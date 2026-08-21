"""Local project-file persistence for Turn.

Turn deliberately keeps the public ``Store`` interface small and async so the
runner and HTTP API do not need to know how state is persisted.  The storage
format is intentionally boring:

* ``<project>/.turn/state.json`` contains that project's nodes, edges, runs,
  and artifacts.
* ``./.turn/config.json`` contains cross-project preferences and the project
  path index.

Writes use a temporary file followed by ``os.replace`` so a process stop never
leaves a half-written graph behind.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    Artifact,
    ArtifactKind,
    ArtifactSpec,
    DocumentRef,
    Edge,
    EdgeType,
    Graph,
    InputSpec,
    InboundMessage,
    InboundMessageStatus,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    ProcessState,
    VerificationResult,
    Run,
    RunPolicy,
    RunStatus,
    RuntimeGuard,
    SubgraphRef,
    Trigger,
    TriggerContext,
    TriggerKind,
    Usage,
    NODE_OBJECTIVE_MAX_LENGTH,
    concise_node_title,
)
from turn.domain.organization import (
    BudgetRequest,
    BudgetRequestStatus,
    Handoff,
    HandoffContract,
    HandoffStatus,
    ManagerPhase,
    OrganizationContract,
    OrganizationPhase,
    OrganizationReview,
    OrganizationScale,
    WorkItem,
    WorkItemStatus,
    WorkspaceRef,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.domain.capability_contracts import SETUP_CAPABILITY_ID, capability_ids_for_agent_type
from turn.domain.lead import (
    BootstrapStatus,
    LeadStatus,
    ProjectLead,
    ReviewDecision,
    ReviewKind,
    ReviewRequest,
    ReviewStatus,
)
from turn.contracts.organization import audit_plan
from turn.contracts.text import sanitize_control_text
from turn.db.state import ProjectState
from turn.graph.logic import GraphWalker
from turn.graph.mutations import (
    append_artifacts,
    apply_plan as apply_graph_plan,
    merge_document_refs,
    merge_subgraph_refs,
)
from turn.logging import EventLog

PLANNER_EXECUTOR = "planner"
# The new organization collections are additive and readers already tolerate
# unknown/missing fields, so keep the existing state version stable for local
# projects and external tooling that pins the compact format.
STATE_VERSION = 3


def _jsonable(value: Any) -> Any:
    """Convert a Pydantic model or a nested value to JSON-compatible data."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class Store:
    """Durable local-file store rooted at a filesystem directory."""

    def __init__(
        self,
        location: str | Path | None = None,
        *,
        data_dir: str | Path | None = None,
        config_path: str | Path | None = None,
        projects_dir: str | Path | None = None,
        logs: EventLog | None = None,
    ):
        raw = str(data_dir or location or (Path.cwd() / ".turn"))
        self.data_dir = self._resolve_data_dir(raw)
        self.config_path = Path(config_path).expanduser().resolve() if config_path else self.data_dir / "config.json"
        self.projects_dir = (
            Path(projects_dir).expanduser().resolve()
            if projects_dir
            else self.data_dir / "projects"
        )
        self._states: dict[uuid.UUID, ProjectState] = {}
        self._project_paths: dict[uuid.UUID, Path] = {}
        self._settings: dict[str, str] = {}
        self._loaded = False
        self._write_lock = asyncio.Lock()
        self._project_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self.logs = logs
        self._event_sink = None

    def set_event_sink(self, sink) -> None:
        """Attach the process-wide trigger dispatcher without coupling storage to it."""
        self._event_sink = sink

    async def _log(self, project_id: uuid.UUID | str | None, **kwargs: Any) -> None:
        if self.logs is not None:
            await self.logs.emit(project_id, **kwargs)

    async def _emit_event(
        self,
        event_name: str,
        *,
        project_id: uuid.UUID | None = None,
        node_id: uuid.UUID | None = None,
        data: dict[str, Any] | None = None,
        source: str = "transition",
    ) -> None:
        if self._event_sink is not None:
            await self._event_sink(
                event_name,
                source=source,
                project_id=project_id,
                node_id=node_id,
                data=data or {},
            )

    @staticmethod
    def _resolve_data_dir(raw: str) -> Path:
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _state_path(project_path: Path) -> Path:
        return project_path / ".turn" / "state.json"

    @staticmethod
    def _project_id_from_state(raw: dict[str, Any]) -> uuid.UUID:
        """Read the project identity from either current or project-local state.

        Current state stores the id at the document root. Older project-local
        state already stores the same identity on its root node, so discovery
        can reconstruct the index without inspecting or rewriting graph data.
        """
        if raw.get("project_id"):
            return uuid.UUID(str(raw["project_id"]))
        roots = [
            item
            for item in raw.get("nodes", [])
            if isinstance(item, dict) and item.get("parent_id") is None
        ]
        if len(roots) != 1:
            raise KeyError("project_id")
        root = roots[0]
        return uuid.UUID(str(root.get("project_id") or root["id"]))

    @staticmethod
    def _empty_state() -> ProjectState:
        return ProjectState.empty()

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _model_dump(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    async def init(self) -> None:
        if self._loaded:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                config = {}
            self._settings = {
                str(key): str(value)
                for key, value in (config.get("settings") or {}).items()
                if value is not None
            }
            projects = config.get("projects") or {}
            for project_id, path in projects.items():
                try:
                    pid = uuid.UUID(str(project_id))
                    project_path = Path(path).expanduser().resolve()
                    self._project_paths[pid] = project_path
                    if self.logs is not None:
                        self.logs.bind_project(pid, project_path)
                except (ValueError, TypeError):
                    continue

        # Also discover local projects if the config was copied or rebuilt.
        projects_root = self.projects_dir
        if projects_root.exists():
            for state_path in projects_root.glob("*/.turn/state.json"):
                try:
                    data = json.loads(state_path.read_text(encoding="utf-8"))
                    project_id = self._project_id_from_state(data)
                    project_path = state_path.parent.parent
                    self._project_paths.setdefault(project_id, project_path)
                    if self.logs is not None:
                        self.logs.bind_project(project_id, project_path)
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue

        for project_id, project_path in list(self._project_paths.items()):
            state_path = self._state_path(project_path)
            if not state_path.exists():
                continue
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                state, normalized = self._decode_state(raw)
                self._states[project_id] = state
                if normalized:
                    await self._persist_project(project_id)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                raise RuntimeError(f"could not read project state {state_path}: {error}") from error
        self._loaded = True

        await self._persist_config()

    def _decode_state(
        self,
        raw: dict[str, Any],
    ) -> tuple[ProjectState, bool]:
        state = self._empty_state()
        normalized = False
        edge_keys: set[tuple[uuid.UUID, uuid.UUID, EdgeType]] = set()
        for item in raw.get("nodes", []):
            node_payload = dict(item)
            agent_payload = node_payload.get("agent")
            if isinstance(agent_payload, dict) and "telemetry_mode" in agent_payload:
                # Telemetry is now an implementation detail of the one normal
                # interactive run path. Remove the retired user preference
                # while normalizing persisted project state once.
                node_payload["agent"] = {
                    key: value for key, value in agent_payload.items()
                    if key != "telemetry_mode"
                }
                normalized = True
            if (
                node_payload.get("executor") == PLANNER_EXECUTOR
                and isinstance(agent_payload, dict)
                and "type_id" not in agent_payload
            ):
                # Very old planner nodes omitted the specialization entirely.
                # Migrate only that missing field; an explicit persisted role
                # is user state and must remain intact.
                node_payload["agent"] = {
                    **agent_payload,
                    "type_id": AgentType.PLANNER.value,
                }
                normalized = True
            # Older planners sometimes put the full assignment in the graph
            # label. Preserve that text as the worker prompt and migrate the
            # persisted label to the same concise form used for new nodes.
            objective = node_payload.get("objective")
            if isinstance(objective, str) and len(objective) > NODE_OBJECTIVE_MAX_LENGTH:
                node_payload["objective"] = concise_node_title(objective)
                if not node_payload.get("generated_prompt"):
                    node_payload["generated_prompt"] = objective
                normalized = True
            node = Node.model_validate(node_payload)
            state.nodes[node.id] = node
        for item in raw.get("edges", []):
            edge = Edge.model_validate(item)
            key = (edge.src, edge.dst, edge.type)
            if key in edge_keys:
                continue
            state.edges[edge.id] = edge
            edge_keys.add(key)
        for item in raw.get("runs", []):
            run = Run.model_validate(item)
            state.runs[run.id] = run
        artifact_keys: set[tuple[Any, ...]] = set()
        for item in raw.get("artifacts", []):
            artifact = Artifact.model_validate(item)
            content_key = json.dumps(artifact.content, sort_keys=True, default=str)
            key = (
                artifact.node_id,
                "ref",
                artifact.ref,
            ) if artifact.ref else (
                artifact.node_id,
                "value",
                artifact.kind,
                artifact.name,
                content_key,
            )
            if key in artifact_keys:
                normalized = True
                continue
            artifact_keys.add(key)
            state.artifacts[artifact.id] = artifact
        for item in raw.get("triggers", []):
            trigger_payload = dict(item)
            if trigger_payload.pop("filters", None) is not None:
                normalized = True
            trigger = Trigger.model_validate(trigger_payload)
            state.triggers[trigger.id] = trigger
        for item in raw.get("work_items", []):
            work_item = WorkItem.model_validate(item)
            state.work_items[work_item.id] = work_item
        for item in raw.get("handoffs", []):
            handoff = Handoff.model_validate(item)
            state.handoffs[handoff.id] = handoff
        for item in raw.get("budget_requests", []):
            request = BudgetRequest.model_validate(item)
            state.budget_requests[request.id] = request
        for item in raw.get("review_requests", []):
            review = ReviewRequest.model_validate(item)
            state.review_requests[review.id] = review
        for item in raw.get("inbound_messages", []):
            inbox_item = InboundMessage.model_validate(item)
            state.inbound_messages[inbox_item.id] = inbox_item
        if raw.get("lead") is not None:
            state.lead = ProjectLead.model_validate(raw["lead"])
        state.bootstrap_status = raw.get("bootstrap_status", state.bootstrap_status)
        # Give pre-organization projects a durable charter when they are first
        # reopened.  This is an additive projection migration: it does not
        # invent tickets or change the existing graph, but it lets the manager
        # loop and organization dashboard observe the real saved boundary.
        for node in state.nodes.values():
            if node.executor != PLANNER_EXECUTOR:
                continue
            if node.organization_contract is None:
                node.organization_contract = OrganizationContract.from_objective(
                    node.generated_prompt or node.objective
                )
                normalized = True
            if node.organization_review is None:
                node.organization_review = OrganizationReview()
                normalized = True
            if node.manager_phase is None:
                node.manager_phase = (
                    ManagerPhase.EXECUTING
                    if node.status in {NodeStatus.EXPANDED, NodeStatus.COMPLETE}
                    else ManagerPhase.PLANNING
                )
                normalized = True
            if not node.acceptance_criteria and node.organization_contract.acceptance_criteria:
                node.acceptance_criteria = list(
                    node.organization_contract.acceptance_criteria
                )
                normalized = True
        for node in state.nodes.values():
            filtered = [artifact_id for artifact_id in node.artifact_refs if artifact_id in state.artifacts]
            if filtered != node.artifact_refs:
                node.artifact_refs = filtered
                normalized = True
        # A planner handoff is the source of truth for its composition link.
        # Older daemon instances persisted the submitted graph but omitted the
        # link from the parent node. Recover that invariant from the durable
        # accepted CLI event so restarting the server repairs the projection
        # without importing or rewriting any referenced graph.
        if self._recover_handoff_source_links(state):
            normalized = True
        return state, normalized

    def _recover_handoff_source_links(self, state: ProjectState) -> bool:
        if self.logs is None:
            return False
        project_ids = {node.project_id for node in state.nodes.values()}
        if len(project_ids) != 1:
            return False
        project_id = next(iter(project_ids))
        changed = False
        pending: dict[uuid.UUID, str] = {}
        accepted: dict[uuid.UUID, list[str]] = {}
        for record in self.logs.read(project_id):
            if (
                record.get("kind") != "agent.action"
                or record.get("action") != "agent.submit"
            ):
                continue
            data = record.get("data")
            if not isinstance(data, dict) or data.get("kind") != "plan":
                continue
            raw_node_id = data.get("node_id")
            if not raw_node_id:
                continue
            try:
                node_id = uuid.UUID(str(raw_node_id))
            except (ValueError, TypeError):
                continue
            status = record.get("status")
            raw_ref = data.get("graph_file")
            if status == "started" and isinstance(raw_ref, str):
                pending[node_id] = raw_ref
                continue
            if status == "error":
                pending.pop(node_id, None)
                continue
            if status != "ok":
                continue
            ref = pending.pop(node_id, None)
            if ref is not None:
                accepted.setdefault(node_id, []).append(ref)

        for node_id, refs in accepted.items():
            node = state.nodes.get(node_id)
            if node is None:
                continue
            sources: list[SubgraphRef] = []
            for raw_ref in refs:
                try:
                    normalized = Path(raw_ref).as_posix()
                    sources.append(
                        SubgraphRef(
                            ref=normalized,
                            title=Path(normalized).name,
                            managed=False,
                        )
                    )
                except (ValueError, TypeError):
                    continue
            if not sources:
                continue
            before = {item.ref for item in node.subgraph_refs}
            node.subgraph_refs = merge_subgraph_refs(node.subgraph_refs, sources)
            changed = changed or before != {item.ref for item in node.subgraph_refs}
        return changed

    def _encode_state(self, project_id: uuid.UUID) -> dict[str, Any]:
        state = self._states[project_id]
        payload = {
            "version": STATE_VERSION,
            "project_id": str(project_id),
            "nodes": [self._model_dump(value) for value in state.nodes.values()],
            "edges": [self._model_dump(value) for value in state.edges.values()],
            "runs": [self._model_dump(value) for value in state.runs.values()],
            "artifacts": [self._model_dump(value) for value in state.artifacts.values()],
        }
        # Keep the original compact state shape for projects that have never
        # opted into triggers; readers treat the field as an empty collection.
        if state.triggers:
            payload["triggers"] = [self._model_dump(value) for value in state.triggers.values()]
        if state.work_items:
            payload["work_items"] = [self._model_dump(value) for value in state.work_items.values()]
        if state.handoffs:
            payload["handoffs"] = [self._model_dump(value) for value in state.handoffs.values()]
        if state.budget_requests:
            payload["budget_requests"] = [
                self._model_dump(value) for value in state.budget_requests.values()
            ]
        if state.review_requests:
            payload["review_requests"] = [
                self._model_dump(value) for value in state.review_requests.values()
            ]
        if state.lead is not None:
            payload["lead"] = self._model_dump(state.lead)
        if state.inbound_messages:
            payload["inbound_messages"] = [
                self._model_dump(value) for value in state.inbound_messages.values()
            ]
        payload["bootstrap_status"] = state.bootstrap_status
        return payload

    async def _persist_project(self, project_id: uuid.UUID) -> None:
        project_path = self._project_paths[project_id]
        async with self._write_lock:
            await asyncio.to_thread(self._write_json, self._state_path(project_path), self._encode_state(project_id))

    async def _persist_config(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "settings": self._settings,
            "projects": {str(project_id): str(path) for project_id, path in self._project_paths.items()},
        }
        async with self._write_lock:
            await asyncio.to_thread(self._write_json, self.config_path, payload)

    async def dispose(self) -> None:
        return None

    def _state(self, project_id: uuid.UUID) -> ProjectState:
        return self._states.setdefault(project_id, self._empty_state())

    def _project_lock(self, project_id: uuid.UUID) -> asyncio.Lock:
        return self._project_locks.setdefault(project_id, asyncio.Lock())

    def _project_for_node(self, node_id: uuid.UUID) -> tuple[uuid.UUID, Node] | None:
        for project_id, state in self._states.items():
            node = state.nodes.get(node_id)
            if node is not None:
                return project_id, node
        return None

    # -- projects ---------------------------------------------------------

    async def create_project(
        self,
        prompt: str,
        name: Optional[str] = None,
        repo_path: Optional[str] = None,
        id=None,
        agent: Optional[AgentConfig] = None,
        run_policy: Optional[RunPolicy] = None,
    ) -> Node:
        # Step mode is the safe default: creating a graph must never launch
        # agents until the user explicitly requests a run.
        auto_run_default = False
        raw = await self.get_setting("default_auto_run", "0")
        auto_run_default = str(raw) not in ("0", "false", "False", "")
        root_id = id or uuid.uuid4()
        policy = run_policy or RunPolicy(auto_run=auto_run_default)
        root_config = agent.model_copy(deep=True) if agent else AgentConfig()
        # Initial setup is a root-project concern. Keep it as an explicit
        # selection so nested planner nodes receive only their normal planner
        # contract plus the capabilities chosen for their subtree.
        root_config.capabilities = list(dict.fromkeys([
            *root_config.capabilities,
            SETUP_CAPABILITY_ID,
        ]))
        root_agent = root_config.as_type(AgentType.PLANNER)
        # ``turn-setup`` belongs to the project root only. ``as_type`` keeps
        # role contracts exact, so restore this root-only capability after
        # specialization rather than allowing it to cascade to descendants.
        root_agent.capabilities = list(dict.fromkeys([
            *root_agent.capabilities,
            SETUP_CAPABILITY_ID,
        ]))
        root_agent.session_id = None
        explicit_name = name.strip() if name and name.strip() else None
        display_name = explicit_name or concise_node_title(prompt)
        contract = OrganizationContract.from_objective(prompt)
        node = Node(
            id=root_id,
            project_id=root_id,
            objective=display_name,
            # A derived title is navigation copy, not a user override. Keep
            # the field empty until the user explicitly names the project so
            # a later planner-authored document title can become the tile
            # title without losing that distinction.
            project_name=explicit_name,
            generated_prompt=prompt,
            executor=PLANNER_EXECUTOR,
            status=NodeStatus.PENDING,
            auto_run=policy.auto_run,
            agent=root_agent,
            run_policy=policy,
            repo_path=repo_path,
            organization_contract=contract,
            organization_review=OrganizationReview(),
            manager_phase=ManagerPhase.PLANNING,
            acceptance_criteria=list(contract.acceptance_criteria),
        )
        project_path = Path(repo_path).expanduser().resolve() if repo_path else self.data_dir / "projects" / f"proj-{root_id.hex[:8]}"
        project_path.mkdir(parents=True, exist_ok=True)
        catalog = CapabilityCatalog(self.data_dir / "capabilities")
        for capability_id in node.agent.capabilities:
            catalog.load_into_project(capability_id, project_path)
        self._project_paths[root_id] = project_path
        if self.logs is not None:
            self.logs.bind_project(root_id, project_path)
        state = self._empty_state()
        state.nodes[node.id] = node
        self._states[root_id] = state
        await self._persist_project(root_id)
        await self._persist_config()
        await self._log(root_id, kind="project.created", action="create_project", message="project created", data={"objective": prompt, "repo_path": repo_path})
        return node.model_copy(deep=True)

    async def list_projects(self) -> list[Node]:
        return [
            state.nodes[project_id].model_copy(deep=True)
            for project_id, state in self._states.items()
            if project_id in state.nodes and state.nodes[project_id].parent_id is None
        ]

    async def delete_project(self, project_id: uuid.UUID) -> None:
        async with self._project_lock(project_id):
            self._states.pop(project_id, None)
            self._project_paths.pop(project_id, None)
            await self._persist_config()
        await self._log(project_id, kind="project.deleted", action="delete_project", message="project deleted")
        if self.logs is not None:
            self.logs.unbind_project(project_id)

    async def clear_projects(self) -> None:
        self._states.clear()
        self._project_paths.clear()
        await self._persist_config()

    # -- node reads -------------------------------------------------------

    async def get_node(self, node_id: uuid.UUID) -> Optional[Node]:
        found = self._project_for_node(node_id)
        return found[1].model_copy(deep=True) if found else None

    async def children_of(self, node_id: uuid.UUID) -> list[Node]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        return [
            node.model_copy(deep=True)
            for node in self._states[project_id].nodes.values()
            if node.parent_id == node_id
        ]

    async def list_work_items(
        self,
        project_id: uuid.UUID,
        *,
        organization_id: uuid.UUID | None = None,
        status: WorkItemStatus | None = None,
    ) -> list[WorkItem]:
        state = self._states.get(project_id)
        if state is None:
            return []
        values = [
            item
            for item in state.work_items.values()
            if (organization_id is None or item.organization_id == organization_id)
            and (status is None or item.status is status)
        ]
        return [
            item.model_copy(deep=True)
            for item in sorted(values, key=lambda value: (-value.priority, value.created_at))
        ]

    async def get_work_item(self, item_id: uuid.UUID) -> WorkItem | None:
        for state in self._states.values():
            item = state.work_items.get(item_id)
            if item is not None:
                return item.model_copy(deep=True)
        return None

    async def create_work_item(
        self,
        *,
        project_id: uuid.UUID,
        organization_id: uuid.UUID,
        key: str | None = None,
        title: str,
        objective: str,
        acceptance_criteria: list[Any] | None = None,
        priority: int = 0,
        depends_on: list[uuid.UUID] | None = None,
        node_id: uuid.UUID | None = None,
        agent_type: str = "executor",
        organization_contract: OrganizationContract | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WorkItem:
        if project_id not in self._states:
            raise ValueError(f"project not found: {project_id}")
        item = WorkItem(
            project_id=project_id,
            organization_id=organization_id,
            node_id=node_id,
            key=key or title,
            agent_type=agent_type,
            organization_contract=organization_contract,
            title=title,
            objective=objective,
            acceptance_criteria=list(acceptance_criteria or []),
            priority=priority,
            depends_on=list(depends_on or []),
            metadata=dict(metadata or {}),
        )
        self._states[project_id].work_items[item.id] = item
        await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="work_item.changed",
            action="work_item.created",
            message=f"work item {item.id} created",
            data={"work_item_id": str(item.id), "organization_id": str(organization_id)},
        )
        return item.model_copy(deep=True)

    async def update_work_item(
        self,
        item_id: uuid.UUID,
        *,
        status: WorkItemStatus | None = None,
        priority: int | None = None,
        claimed_by: uuid.UUID | None = None,
        rejection_reason: str | None = None,
        artifact_refs: list[uuid.UUID] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> WorkItem | None:
        found = next(
            (
                (project_id, item)
                for project_id, state in self._states.items()
                if (item := state.work_items.get(item_id)) is not None
            ),
            None,
        )
        if found is None:
            return None
        project_id, _ = found
        # The read-modify-write cycle must hold the project lock: unlocked
        # field-by-field mutation interleaves with locked graph writers and
        # can persist a half-updated work item.
        async with self._project_lock(project_id):
            item = self._states[project_id].work_items.get(item_id)
            if item is None:
                return None
            if status is not None:
                item.status = status
            if priority is not None:
                item.priority = priority
            if claimed_by is not None:
                item.claimed_by = claimed_by
            if rejection_reason is not None:
                item.rejection_reason = sanitize_control_text(rejection_reason)
            if artifact_refs is not None:
                item.artifact_refs = list(artifact_refs)
            if evidence_refs is not None:
                item.evidence_refs = list(evidence_refs)
            item.updated_at = datetime.now(timezone.utc)
            await self._persist_project(project_id)
            await self._log(
                project_id,
                kind="work_item.changed",
                action="work_item.update",
                message=f"work item {item.id} updated",
                data={"work_item_id": str(item.id), "status": item.status.value},
            )
            return item.model_copy(deep=True)

    async def claim_work_item(
        self, item_id: uuid.UUID, node_id: uuid.UUID | None = None
    ) -> WorkItem | None:
        found = next(
            (
                (project_id, item_id)
                for project_id, state in self._states.items()
                if item_id in state.work_items
            ),
            None,
        )
        if found is None:
            return None
        project_id, _ = found
        # Claiming is the assignment boundary of the whole orchestrator. The
        # claimability check and the status flip must be one atomic step under
        # the project lock; otherwise two concurrent claims (scheduler tick +
        # CLI agent) both pass the BACKLOG/READY check and one node's work is
        # silently stolen.
        async with self._project_lock(project_id):
            state = self._states[project_id]
            item = state.work_items.get(item_id)
            if item is None:
                return None
            if item.status not in {WorkItemStatus.BACKLOG, WorkItemStatus.READY}:
                raise ValueError(f"work item is not claimable: {item.status.value}")
            incomplete = [
                dependency
                for dependency in item.depends_on
                if dependency not in state.work_items
                or state.work_items[dependency].status is not WorkItemStatus.COMPLETE
            ]
            if incomplete:
                raise ValueError(
                    "work item dependencies are incomplete: "
                    + ", ".join(str(dependency) for dependency in incomplete)
                )
            item.status = WorkItemStatus.CLAIMED
            if node_id is not None:
                item.claimed_by = node_id
            item.updated_at = datetime.now(timezone.utc)
            await self._persist_project(project_id)
            await self._log(
                project_id,
                kind="work_item.changed",
                action="work_item.update",
                message=f"work item {item.id} updated",
                data={"work_item_id": str(item.id), "status": item.status.value},
            )
            return item.model_copy(deep=True)

    async def materialize_ready_work_items(
        self, project_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Node]:
        """Turn dependency-ready backlog entries into ordinary graph nodes."""
        state = self._states.get(project_id)
        if state is None:
            return []
        # Selection, node creation, and ticket mutation are one atomic
        # assignment step: two concurrent scheduler ticks must never both
        # materialize the same backlog entry.
        async with self._project_lock(project_id):
            return await self._materialize_ready_work_items_locked(project_id, state, limit=limit)

    async def _materialize_ready_work_items_locked(
        self, project_id: uuid.UUID, state, *, limit: int | None
    ) -> list[Node]:
        candidates = sorted(
            (
                item for item in state.work_items.values()
                if item.node_id is None
                and item.status in {WorkItemStatus.BACKLOG, WorkItemStatus.READY}
                and all(
                    dependency in state.work_items
                    and state.work_items[dependency].status is WorkItemStatus.COMPLETE
                    for dependency in item.depends_on
                )
            ),
            key=lambda item: (-item.priority, item.created_at),
        )
        if limit is not None:
            candidates = candidates[: max(0, limit)]
        materialized: list[Node] = []
        for item in candidates:
            parent = state.nodes.get(item.organization_id)
            if parent is None:
                item.status = WorkItemStatus.BLOCKED
                continue
            try:
                role = AgentType(item.agent_type)
            except ValueError:
                role = None
            spec = NodeSpec(
                key=f"ticket-{item.id.hex}",
                objective=item.title,
                generated_prompt=item.objective,
                executor=None if role is not None else item.agent_type,
                agent_type=role,
                organization_contract=item.organization_contract,
                acceptance_criteria=list(item.acceptance_criteria),
                priority=item.priority,
            )
            plan = PlanResult(nodes=[spec])
            created = apply_graph_plan(state, parent, plan)
            node = created[0]
            node.work_item_id = item.id
            state.nodes[node.id] = node.model_copy(deep=True)
            item.node_id = node.id
            # Materialization is the scheduler's assignment boundary. Keep
            # the durable ticket truthful: it is no longer merely queued and
            # the created graph node is its concrete owner.
            item.claimed_by = node.id
            item.status = WorkItemStatus.ACTIVE
            item.updated_at = datetime.now(timezone.utc)
            for dependency_id in item.depends_on:
                dependency = state.work_items.get(dependency_id)
                if dependency is None or dependency.node_id is None:
                    continue
                state.edges[uuid.uuid4()] = Edge(
                    src=dependency.node_id,
                    dst=node.id,
                    type=EdgeType.FOLLOWS,
                )
            materialized.append(node.model_copy(deep=True))
        if materialized or any(item.status is WorkItemStatus.BLOCKED for item in candidates):
            await self._persist_project(project_id)
        return materialized

    async def create_budget_request(
        self,
        *,
        project_id: uuid.UUID,
        organization_id: uuid.UUID,
        requested_budget,
        reason: str,
    ) -> BudgetRequest:
        """Persist a budget change request without changing the live budget."""
        organization = await self.get_node(organization_id)
        if (
            organization is None
            or organization.project_id != project_id
            or organization.organization_contract is None
        ):
            raise ValueError("organization_id must identify a planner boundary in this project")
        request = BudgetRequest(
            project_id=project_id,
            organization_id=organization_id,
            requested_budget=requested_budget,
            reason=reason,
        )
        self._state(project_id).budget_requests[request.id] = request
        await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="organization.budget",
            action="budget.requested",
            message=f"budget request {request.id} submitted",
            data={
                "budget_request_id": str(request.id),
                "organization_id": str(organization_id),
                "reason": reason,
            },
        )
        return request.model_copy(deep=True)

    async def list_budget_requests(
        self,
        project_id: uuid.UUID,
        *,
        organization_id: uuid.UUID | None = None,
        status: BudgetRequestStatus | None = None,
    ) -> list[BudgetRequest]:
        state = self._states.get(project_id)
        if state is None:
            return []
        values = [
            request
            for request in state.budget_requests.values()
            if (organization_id is None or request.organization_id == organization_id)
            and (status is None or request.status is status)
        ]
        return [
            request.model_copy(deep=True)
            for request in sorted(values, key=lambda value: value.requested_at)
        ]

    async def decide_budget_request(
        self,
        request_id: uuid.UUID,
        *,
        status: BudgetRequestStatus,
        decision_reason: str | None = None,
    ) -> BudgetRequest | None:
        """Apply an explicit budget decision at a manager/API boundary."""
        for project_id, state in self._states.items():
            request = state.budget_requests.get(request_id)
            if request is None:
                continue
            if request.status is not BudgetRequestStatus.PENDING:
                raise ValueError("budget request has already been decided")
            request.status = BudgetRequestStatus(status)
            request.decision_reason = decision_reason
            request.reviewed_at = datetime.now(timezone.utc)
            if request.status is BudgetRequestStatus.APPROVED:
                organization = state.nodes.get(request.organization_id)
                if organization is None or organization.organization_contract is None:
                    raise ValueError("budget request organization no longer exists")
                organization.organization_contract.budget = (
                    request.requested_budget.model_copy(deep=True)
                )
                organization.updated_at = datetime.now(timezone.utc)
                state.nodes[organization.id] = organization
            await self._persist_project(project_id)
            await self._log(
                project_id,
                kind="organization.budget",
                action="budget.decided",
                message=f"budget request {request.id} {request.status.value.lower()}",
                data={
                    "budget_request_id": str(request.id),
                    "organization_id": str(request.organization_id),
                    "status": request.status.value,
                },
            )
            return request.model_copy(deep=True)
        return None

    # ------------------------------------------------------------------
    # Project lead and review requests

    async def project_lead(self, project_id: uuid.UUID) -> ProjectLead | None:
        """Return the project's single lead, creating nothing implicitly."""
        state = self._states.get(project_id)
        if state is None or state.lead is None:
            return None
        return state.lead.model_copy(deep=True)

    async def lead_by_terminal_owner(self, owner_id: uuid.UUID) -> ProjectLead | None:
        """Resolve a lead by its stable terminal owner identity."""
        for state in self._states.values():
            if state.lead is not None and state.lead.terminal_owner_id == owner_id:
                return state.lead.model_copy(deep=True)
        return None

    async def ensure_project_lead(
        self,
        project_id: uuid.UUID,
        *,
        agent=None,
    ) -> ProjectLead:
        """Return the project lead, creating the single instance on first call.

        The lead is idempotent per project: repeated bootstrap or restart
        paths must never spawn a second oversight agent.
        """
        async with self._project_lock(project_id):
            state = self._state(project_id)
            changed = False
            if state.lead is None:
                state.lead = ProjectLead(project_id=project_id, agent=agent)
                changed = True
                await self._log(
                    project_id,
                    kind="lead.created",
                    action="lead.created",
                    message=f"project lead {state.lead.id} created",
                    data={"lead_id": str(state.lead.id)},
                )
            elif agent is not None and state.lead.agent is None:
                state.lead.agent = agent
                changed = True
            if changed:
                await self._persist_project(project_id)
            return state.lead.model_copy(deep=True)

    async def update_lead(
        self,
        project_id: uuid.UUID,
        *,
        session_id: str | None = None,
        status=None,
        agent=None,
        wait_events: list[str] | None = None,
    ) -> ProjectLead | None:
        """Persist lead lifecycle/session fields (all updates optional)."""
        async with self._project_lock(project_id):
            state = self._state(project_id)
            if state.lead is None:
                return None
            if session_id is not None:
                state.lead.session_id = session_id
            if status is not None:
                from turn.domain.lead import LeadStatus
                state.lead.status = LeadStatus(status)
            if agent is not None:
                state.lead.agent = agent
            if wait_events is not None:
                state.lead.wait_events = list(dict.fromkeys(wait_events))
            await self._persist_project(project_id)
            return state.lead.model_copy(deep=True)

    async def queue_inbound_message(
        self,
        project_id: uuid.UUID,
        recipient_node_id: uuid.UUID,
        content: str,
        *,
        source: str = "user",
    ) -> InboundMessage:
        """Queue information for the recipient's next safe Run boundary."""
        node = await self.get_node(recipient_node_id)
        if node is None or node.project_id != project_id:
            raise ValueError("recipient_node_id must identify a node in this project")
        item = InboundMessage(
            project_id=project_id,
            recipient_node_id=recipient_node_id,
            content=content.strip(),
            source=source,
        )
        async with self._project_lock(project_id):
            self._state(project_id).inbound_messages[item.id] = item
            await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="agent.inbox",
            action="message.queued",
            message=f"message queued for node {recipient_node_id}",
            data={"message_id": str(item.id), "node_id": str(recipient_node_id)},
        )
        return item.model_copy(deep=True)

    async def pending_inbound_messages(
        self,
        project_id: uuid.UUID,
        recipient_node_id: uuid.UUID,
    ) -> list[InboundMessage]:
        state = self._states.get(project_id)
        if state is None:
            return []
        return [
            item.model_copy(deep=True)
            for item in sorted(state.inbound_messages.values(), key=lambda value: value.created_at)
            if item.recipient_node_id == recipient_node_id
            and item.status is InboundMessageStatus.QUEUED
        ]

    async def mark_inbound_messages_consumed(
        self,
        project_id: uuid.UUID,
        message_ids: list[uuid.UUID],
        run_id: uuid.UUID,
    ) -> None:
        wanted = set(message_ids)
        if not wanted:
            return
        async with self._project_lock(project_id):
            state = self._state(project_id)
            changed = False
            for item_id in wanted:
                item = state.inbound_messages.get(item_id)
                if item is not None and item.status is InboundMessageStatus.QUEUED:
                    item.status = InboundMessageStatus.CONSUMED
                    item.run_id = run_id
                    changed = True
            if changed:
                await self._persist_project(project_id)

    async def wake_lead_for_event(
        self,
        project_id: uuid.UUID,
        event_name: str,
    ) -> ProjectLead | None:
        """Wake a dormant lead only for an explicitly requested event."""
        async with self._project_lock(project_id):
            state = self._state(project_id)
            lead = state.lead
            if (
                lead is None
                or lead.status.value != "DORMANT"
                or (lead.wait_events and event_name not in lead.wait_events)
            ):
                return lead.model_copy(deep=True) if lead is not None else None
            lead.status = LeadStatus.IDLE
            lead.wait_events = []
            await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="lead.wake",
            action="lead.wake.event",
            message=f"lead woke for {event_name}",
            data={"event_name": event_name},
        )
        return lead.model_copy(deep=True)

    async def create_review_request(
        self,
        *,
        project_id: uuid.UUID,
        sender_id: uuid.UUID,
        receiver_id: uuid.UUID,
        receiver_is_lead: bool,
        kind: ReviewKind,
        reason: str | None = None,
        artifact_refs: list[str] | None = None,
        required_changes: list[str] | None = None,
    ) -> ReviewRequest:
        """Durably record one pending review/escalation turn."""
        request = ReviewRequest(
            project_id=project_id,
            sender_id=sender_id,
            receiver_id=receiver_id,
            receiver_is_lead=receiver_is_lead,
            kind=kind,
            reason=reason,
            artifact_refs=list(artifact_refs or []),
            required_changes=list(required_changes or []),
        )
        async with self._project_lock(project_id):
            self._state(project_id).review_requests[request.id] = request
            await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="review.requested",
            action="review.requested",
            message=f"{kind.value} review requested ({request.status.value})",
            data={
                "review_request_id": str(request.id),
                "sender_id": str(sender_id),
                "receiver_id": str(receiver_id),
                "receiver_is_lead": receiver_is_lead,
                "kind": kind.value,
                "reason": reason,
            },
        )
        return request.model_copy(deep=True)

    async def update_review_request(
        self,
        project_id: uuid.UUID,
        request_id: uuid.UUID,
        *,
        status: ReviewStatus | None = None,
        decision: ReviewDecision | None = None,
        summary: str | None = None,
        required_changes: list[str] | None = None,
    ) -> ReviewRequest | None:
        """Advance a review request; SETTLED stamps ``settled_at`` once."""
        async with self._project_lock(project_id):
            state = self._state(project_id)
            request = state.review_requests.get(request_id)
            if request is None:
                return None
            changed = False
            if status is not None and status is not request.status:
                request.status = status
                changed = True
            if decision is not None:
                request.decision = decision
                changed = True
            if summary is not None:
                request.summary = summary
                changed = True
            if required_changes is not None:
                request.required_changes = list(required_changes)
                changed = True
            if request.status is ReviewStatus.SETTLED and request.settled_at is None:
                from datetime import datetime, timezone as _timezone
                request.settled_at = datetime.now(_timezone.utc)
                changed = True
            if changed:
                await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="review.updated",
            action="review.updated",
            message=f"review {request_id} -> {request.status.value}"
            + (f" ({request.decision.value})" if request.decision else ""),
            data={
                "review_request_id": str(request_id),
                "status": request.status.value,
                "decision": request.decision.value if request.decision else None,
                "summary": request.summary,
            },
        )
        return request.model_copy(deep=True)

    async def review_requests(
        self,
        project_id: uuid.UUID,
        *,
        status: ReviewStatus | None = None,
        sender_id: uuid.UUID | None = None,
        receiver_id: uuid.UUID | None = None,
    ) -> list[ReviewRequest]:
        """List durable review requests, newest first, optionally filtered."""
        state = self._states.get(project_id)
        if state is None:
            return []
        values = [
            item
            for item in state.review_requests.values()
            if (status is None or item.status is status)
            and (sender_id is None or item.sender_id == sender_id)
            and (receiver_id is None or item.receiver_id == receiver_id)
        ]
        values.sort(key=lambda item: item.created_at, reverse=True)
        return [item.model_copy(deep=True) for item in values]

    def bootstrap_status_sync(self, project_id: uuid.UUID) -> str:
        """Synchronous bootstrap-status read for scheduler hot paths."""
        state = self._states.get(project_id)
        return state.bootstrap_status if state else "READY"

    async def bootstrap_status(self, project_id: uuid.UUID) -> str:
        return self.bootstrap_status_sync(project_id)

    async def set_bootstrap_status(
        self, project_id: uuid.UUID, status: str
    ) -> str:
        if status not in ("BOOTSTRAPPING", "READY"):
            raise ValueError(f"invalid bootstrap status: {status}")
        async with self._project_lock(project_id):
            state = self._state(project_id)
            previous = state.bootstrap_status
            state.bootstrap_status = status
            await self._persist_project(project_id)
        if previous != status:
            await self._log(
                project_id,
                kind="project.bootstrap",
                action="bootstrap.status",
                message=f"bootstrap {previous} -> {status}",
                data={"from": previous, "to": status},
            )
        return status

    async def list_handoffs(
        self,
        project_id: uuid.UUID,
        *,
        node_id: uuid.UUID | None = None,
    ) -> list[Handoff]:
        state = self._states.get(project_id)
        if state is None:
            return []
        values = [
            item
            for item in state.handoffs.values()
            if node_id is None
            or item.producer_node_id == node_id
            or item.consumer_node_id == node_id
        ]
        return [item.model_copy(deep=True) for item in values]

    async def update_handoff(
        self,
        handoff_id: uuid.UUID,
        *,
        status: HandoffStatus,
        artifact_id: uuid.UUID | None = None,
        evidence_refs: list[str] | None = None,
        rejection_reason: str | None = None,
    ) -> Handoff | None:
        status = HandoffStatus(status)
        for project_id, state in self._states.items():
            handoff = state.handoffs.get(handoff_id)
            if handoff is None:
                continue
            artifact = None
            if artifact_id is not None:
                artifact = state.artifacts.get(artifact_id)
                if artifact is None or artifact.node_id != handoff.producer_node_id:
                    raise ValueError(
                        "handoff artifact must be produced by its declared producer"
                    )
                if (
                    artifact.schema_name != handoff.contract.schema_name
                    or artifact.schema_version != handoff.contract.version
                ):
                    raise ValueError(
                        "handoff artifact does not satisfy its schema contract"
                    )
            elif handoff.artifact_id is not None:
                artifact = state.artifacts.get(handoff.artifact_id)
            if status in {HandoffStatus.AVAILABLE, HandoffStatus.ACCEPTED}:
                if artifact is None:
                    raise ValueError(
                        "available or accepted handoffs require a matching artifact"
                    )
                if status is HandoffStatus.ACCEPTED and handoff.contract.evidence_required:
                    refs = evidence_refs or artifact.evidence_refs
                    if not refs:
                        raise ValueError(
                            "accepted handoffs require acceptance evidence"
                        )
            handoff.status = status
            if artifact_id is not None:
                handoff.artifact_id = artifact_id
            if evidence_refs is not None:
                handoff.evidence_refs = list(evidence_refs)
            if rejection_reason is not None:
                handoff.rejection_reason = rejection_reason
            handoff.updated_at = datetime.now(timezone.utc)
            await self._persist_project(project_id)
            await self._log(
                project_id,
                kind="handoff.changed",
                action="handoff.update",
                message=f"handoff {handoff.id} updated",
                data={"handoff_id": str(handoff.id), "status": handoff.status.value},
            )
            return handoff.model_copy(deep=True)
        return None

    async def get_workgraph(self, project_id: uuid.UUID):
        graph = await self.get_graph(project_id)
        return graph.nodes, graph.edges, graph.artifacts

    def project_path(self, project_id: uuid.UUID) -> Path | None:
        """Return the project root for server-owned asset serving."""
        path = self._project_paths.get(project_id)
        return path.resolve() if path is not None else None

    async def get_graph(self, project_id: uuid.UUID) -> Graph:
        state = self._states.get(project_id, self._empty_state())
        root = state.nodes.get(project_id)
        return Graph(
            project_id=project_id,
            nodes=[node.model_copy(deep=True) for node in state.nodes.values()],
            edges=[edge.model_copy(deep=True) for edge in state.edges.values()],
            artifacts=[artifact.model_copy(deep=True) for artifact in state.artifacts.values()],
            triggers=[trigger.model_copy(deep=True) for trigger in state.triggers.values()],
            work_items=[item.model_copy(deep=True) for item in state.work_items.values()],
            handoffs=[item.model_copy(deep=True) for item in state.handoffs.values()],
            budget_requests=[
                item.model_copy(deep=True) for item in state.budget_requests.values()
            ],
        )

    async def _load_graph(self, project_id: uuid.UUID):
        nodes, edges, _ = await self.get_workgraph(project_id)
        node_map = {node.id: node for node in nodes}
        children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
        parents: dict[uuid.UUID, Optional[uuid.UUID]] = {}
        predecessors: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node.id)
            parents[node.id] = node.parent_id
        for edge in edges:
            if edge.type == EdgeType.FOLLOWS:
                predecessors.setdefault(edge.dst, []).append(edge.src)
        return node_map, children, parents, predecessors

    async def ancestry(self, node_id: uuid.UUID) -> list[Node]:
        current = await self.get_node(node_id)
        if current is None:
            return []
        nodes, edges, _ = await self.get_workgraph(current.project_id)
        return GraphWalker(nodes, edges).ancestors(node_id)

    async def descendants(self, node_id: uuid.UUID) -> list[Node]:
        current = await self.get_node(node_id)
        if current is None:
            return []
        nodes, edges, _ = await self.get_workgraph(current.project_id)
        return GraphWalker(nodes, edges).descendants(node_id)

    async def predecessors(self, node_id: uuid.UUID) -> list[Node]:
        current = await self.get_node(node_id)
        if current is None:
            return []
        nodes, edges, _ = await self.get_workgraph(current.project_id)
        return GraphWalker(nodes, edges).predecessors(node_id)

    # -- node writes ------------------------------------------------------

    @staticmethod
    def _work_item_status(status: NodeStatus) -> WorkItemStatus:
        return {
            NodeStatus.PENDING: WorkItemStatus.BACKLOG,
            NodeStatus.BLOCKED: WorkItemStatus.BLOCKED,
            NodeStatus.RUNNABLE: WorkItemStatus.READY,
            NodeStatus.RUNNING: WorkItemStatus.RUNNING,
            NodeStatus.EXPANDED: WorkItemStatus.CLAIMED,
            NodeStatus.COMPLETE: WorkItemStatus.COMPLETE,
            NodeStatus.FAILED: WorkItemStatus.REJECTED,
            NodeStatus.CANCELLED: WorkItemStatus.CANCELLED,
        }[status]

    def _sync_work_item(self, node: Node) -> None:
        if node.work_item_id is None:
            return
        item = self._states.get(node.project_id, self._empty_state()).work_items.get(node.work_item_id)
        if item is None:
            return
        state = self._states.get(node.project_id, self._empty_state())
        item.status = self._work_item_status(node.status)
        item.artifact_refs = list(node.artifact_refs)
        item.evidence_refs = list(
            dict.fromkeys(
                ref
                for artifact in state.artifacts.values()
                if artifact.id in node.artifact_refs
                and artifact.kind is ArtifactKind.EVIDENCE
                for ref in artifact.evidence_refs
            )
        )
        item.updated_at = datetime.now(timezone.utc)

    def _sync_handoffs_for_node(self, node_id: uuid.UUID) -> None:
        """Make matching producer artifacts visible to typed handoffs."""
        found = self._project_for_node(node_id)
        if found is None:
            return
        project_id, _ = found
        state = self._states[project_id]
        produced = [
            artifact
            for artifact in state.artifacts.values()
            if artifact.node_id == node_id
        ]
        for handoff in state.handoffs.values():
            if handoff.producer_node_id != node_id:
                continue
            matching = next(
                (
                    artifact
                    for artifact in produced
                    if artifact.schema_name == handoff.contract.schema_name
                    and artifact.schema_version == handoff.contract.version
                ),
                None,
            )
            if matching is None:
                continue
            if (
                handoff.status is HandoffStatus.ACCEPTED
                and handoff.artifact_id == matching.id
            ):
                continue
            handoff.artifact_id = matching.id
            handoff.evidence_refs = list(dict.fromkeys(matching.evidence_refs))
            handoff.status = HandoffStatus.AVAILABLE
            handoff.rejection_reason = None
            handoff.updated_at = datetime.now(timezone.utc)

    async def _save_node(self, node: Node) -> Node | None:
        """Persist one node mutation and return the durable copy.

        Returns ``None`` when the write was intentionally dropped. Callers
        receive an honest failure signal instead of an unpersisted copy that
        merely looks saved.
        """
        if node.project_id not in self._states:
            await self._log(
                None,
                kind="state.changed",
                action="node.save.dropped",
                status="error",
                message=f"node {node.id} update dropped: project {node.project_id} is not loaded",
                data={"node_id": str(node.id), "project_id": str(node.project_id)},
            )
            return None
        previous = self._states[node.project_id].nodes.get(node.id)
        if previous is None:
            # A provider/reconnect callback may hold a snapshot while a plan
            # replacement removes its subtree. Never let that stale callback
            # recreate a deleted node; graph creation is owned by apply_plan
            # and create_node, not by state updates.
            await self._log(
                node.project_id,
                kind="state.changed",
                action="node.save.dropped",
                status="error",
                message=f"node {node.id} update dropped: node no longer exists in project {node.project_id}",
                data={"node_id": str(node.id), "project_id": str(node.project_id)},
            )
            return None
        saved = node.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        self._states[node.project_id].nodes[node.id] = saved
        self._sync_work_item(saved)
        self._sync_handoffs_for_node(saved.id)
        await self._persist_project(node.project_id)
        if previous is not None:
            before = previous.model_dump(mode="json")
            after = saved.model_dump(mode="json")
            changed = {key: {"from": before.get(key), "to": after.get(key)} for key in before if key != "updated_at" and before.get(key) != after.get(key)}
            if changed:
                await self._log(node.project_id, kind="state.changed", action="node.update", message=f"node {node.id} state changed", data={"node_id": str(node.id), "changes": changed})
        return saved.model_copy(deep=True)

    async def set_status(self, node_id: uuid.UUID, status: NodeStatus) -> Optional[Node]:
        found = self._project_for_node(node_id)
        if found is None:
            return None
        project_id, _ = found
        async with self._project_lock(project_id):
            current = self._states[project_id].nodes.get(node_id)
            if current is None:
                return None
            node = current.model_copy(deep=True)
            previous = node.status
            node.status = status
            if previous != status:
                await self._log(node.project_id, kind="graph.transition", action="node.status", message=f"{previous.value} -> {status.value}", data={"node_id": str(node_id), "from": previous.value, "to": status.value})
            saved = await self._save_node(node)
        if saved is None:
            return None
        if previous != status:
            await self._emit_event(
                "node.status.changed",
                project_id=saved.project_id,
                node_id=saved.id,
                data={
                    "node_id": str(saved.id),
                    "project_id": str(saved.project_id),
                    "from": previous.value,
                    "to": status.value,
                },
            )
        return saved

    async def set_status_if_current(
        self, node_id: uuid.UUID, status: NodeStatus, expected: tuple[NodeStatus, ...]
    ) -> Optional[Node]:
        found = self._project_for_node(node_id)
        if found is None:
            return None
        project_id, _ = found
        saved: Node | None = None
        previous: NodeStatus | None = None
        async with self._project_lock(project_id):
            current = self._states[project_id].nodes.get(node_id)
            if current is None or current.status not in expected:
                return None
            node = current.model_copy(deep=True)
            node.status = status
            previous = current.status
            if current.status != status:
                await self._log(project_id, kind="graph.transition", action="node.status", message=f"{current.status.value} -> {status.value}", data={"node_id": str(node_id), "from": current.status.value, "to": status.value})
            saved = await self._save_node(node)
        if saved is not None and previous != status:
            # Dispatch after releasing the project lock: a matching trigger
            # may reset a node in this same project and must be able to enter
            # activate_trigger without deadlocking the Store.
            await self._emit_event(
                "node.status.changed",
                project_id=saved.project_id,
                node_id=saved.id,
                data={
                    "node_id": str(saved.id),
                    "project_id": str(saved.project_id),
                    "from": previous.value if previous else None,
                    "to": status.value,
                },
            )
        return saved

    async def publish_outputs(
        self,
        node_id: uuid.UUID,
        *,
        outputs: dict[str, str] | None = None,
        route: str | None = None,
    ) -> Optional[Node]:
        """Persist one completed node's published variables and chosen route.

        Only names the node explicitly declares in ``provides`` are published;
        undeclared worker output keys are dropped so a node cannot silently
        widen its data contract.
        """
        found = self._project_for_node(node_id)
        if found is None:
            return None
        project_id, _ = found
        async with self._project_lock(project_id):
            current = await self.get_node(node_id)
            if current is None:
                return None
            changed = False
            if outputs:
                declared = set(current.provides)
                published = {key: str(value) for key, value in outputs.items() if key in declared}
                if published:
                    current.outputs = {**current.outputs, **published}
                    changed = True
            if route is not None and route != current.route_taken:
                current.route_taken = route
                changed = True
            if not changed:
                return current
            return await self._save_node(current)

    async def set_agent_status(
        self, node_id: uuid.UUID, *, state: str | None, message: str | None
    ) -> Optional[Node]:
        found = self._project_for_node(node_id)
        if found is None:
            return None
        project_id, _ = found
        # Live agent status arrives from CLI submissions racing the scheduler;
        # the read-modify-write must hold the project lock like every other
        # node mutator or a locked graph writer can persist a torn update.
        async with self._project_lock(project_id):
            current = await self.get_node(node_id)
            if current is None:
                return None
            current.agent_state = sanitize_control_text(state) if state is not None else None
            current.agent_message = sanitize_control_text(message) if message is not None else None
            return await self._save_node(current)

    async def set_runtime_guard(
        self, project_id: uuid.UUID, *, code: str, message: str
    ) -> Optional[Node]:
        """Persist one infrastructure guard on the project root.

        The root is the durable project-wide circuit breaker.  Repeated
        scheduler ticks update neither the timestamp nor the log once the
        same guard is already present, so a boundary failure cannot become a
        self-amplifying retry loop.
        """
        node = await self.get_node(project_id)
        if node is None:
            return None
        clean_code = sanitize_control_text(code)
        clean_message = sanitize_control_text(message)
        current = node.runtime_guard
        if current is not None and current.code == clean_code and current.message == clean_message:
            return node
        async with self._project_lock(project_id):
            fresh = await self.get_node(project_id)
            if fresh is None:
                return None
            existing = fresh.runtime_guard
            if existing is not None and existing.code == clean_code and existing.message == clean_message:
                return fresh
            fresh.runtime_guard = RuntimeGuard(code=clean_code, message=clean_message)
            saved = await self._save_node(fresh)
        await self._log(
            project_id,
            kind="runtime.guard",
            action="runtime.guard.raised",
            message=clean_message,
            status="error",
            data={"code": clean_code, "retry_suppressed": True},
        )
        return saved

    async def clear_runtime_guard(self, project_id: uuid.UUID) -> Optional[Node]:
        """Clear a guard only through an explicit user/operator mutation."""
        node = await self.get_node(project_id)
        if node is None or node.runtime_guard is None:
            return node
        node.runtime_guard = None
        return await self._save_node(node)

    async def set_organization_review(
        self, node_id: uuid.UUID, review: OrganizationReview
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        clean = review.model_copy(deep=True)
        clean.last_reason = sanitize_control_text(clean.last_reason) if clean.last_reason else None
        clean.audit_summary = sanitize_control_text(clean.audit_summary) if clean.audit_summary else None
        clean.audit_findings = [sanitize_control_text(item) for item in clean.audit_findings]
        clean.audit_required_changes = [sanitize_control_text(item) for item in clean.audit_required_changes]
        if clean.audit is not None:
            clean.audit.errors = [sanitize_control_text(item) for item in clean.audit.errors]
            clean.audit.warnings = [sanitize_control_text(item) for item in clean.audit.warnings]
        node.organization_review = clean
        return await self._save_node(node)

    async def set_manager_state(
        self,
        node_id: uuid.UUID,
        *,
        phase: ManagerPhase,
        iteration: int | None = None,
        reasons: list[str] | None = None,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.manager_phase = phase
        if iteration is not None:
            node.manager_iteration = iteration
        if reasons is not None:
            node.manager_review_reasons = list(dict.fromkeys(sanitize_control_text(item) for item in reasons))
        return await self._save_node(node)

    async def set_paused(self, node_id: uuid.UUID, paused: bool) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.paused = paused
        return await self._save_node(node)

    async def set_auto_run(self, project_id: uuid.UUID, auto_run: bool) -> Optional[Node]:
        node = await self.get_node(project_id)
        if node is None:
            return None
        node.auto_run = auto_run
        return await self._save_node(node)

    async def set_resource_refs(self, node_id: uuid.UUID, refs: list[str]) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.resource_refs = list(refs)
        return await self._save_node(node)

    async def set_workspace_path(self, node_id: uuid.UUID, path: str | None) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.workspace_path = path
        return await self._save_node(node)

    async def set_workspace_ref(
        self,
        node_id: uuid.UUID,
        *,
        path: str | None,
        branch: str | None,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.workspace_path = path
        node.workspace = WorkspaceRef(path=path, branch=branch) if path and branch else None
        node.output_branch = branch
        return await self._save_node(node)

    async def set_workspace_commit(self, node_id: uuid.UUID, commit: str | None) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.workspace_commit = commit
        return await self._save_node(node)

    async def rename_project(self, project_id: uuid.UUID, name: str) -> Optional[Node]:
        node = await self.get_node(project_id)
        if node is None or node.parent_id is not None:
            return None
        node.project_name = name.strip()
        return await self._save_node(node)

    async def set_project_policy(self, project_id: uuid.UUID, policy: RunPolicy) -> Optional[Node]:
        node = await self.get_node(project_id)
        if node is None or node.parent_id is not None:
            return None
        node.run_policy = policy
        node.auto_run = policy.auto_run
        return await self._save_node(node)

    async def set_project_mode(self, project_id: uuid.UUID, auto_run: bool) -> Optional[Node]:
        node = await self.get_node(project_id)
        if node is None or node.parent_id is not None:
            return None
        node.auto_run = auto_run
        if node.run_policy is not None:
            node.run_policy.auto_run = auto_run
        return await self._save_node(node)

    async def set_required_inputs(
        self,
        node_id: uuid.UUID,
        required_inputs: list[InputSpec],
        *,
        merge: bool = False,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        if merge:
            by_id = {item.id: item for item in node.required_inputs}
            by_id.update({item.id: item for item in required_inputs})
            node.required_inputs = list(by_id.values())
        else:
            node.required_inputs = list(required_inputs)
        return await self._save_node(node)

    async def set_agent_session(
        self, node_id: uuid.UUID, session_id: str | None, *, agent: AgentConfig | None = None
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        if agent is not None:
            node.agent = agent.model_copy(deep=True)
        if node.agent is None:
            node.agent = AgentConfig()
        previous = node.agent.session_id
        node.agent.session_id = session_id
        if previous != session_id:
            await self._log(node.project_id, kind="decision.session", action="session.cleared" if session_id is None else "session.set", message="agent session cleared" if session_id is None else "agent session saved", data={"node_id": str(node_id), "previous_session_id": previous, "session_id": session_id})
        return await self._save_node(node)

    async def clear_agent_session(self, node_id: uuid.UUID) -> Optional[Node]:
        return await self.set_agent_session(node_id, None)

    async def complete_verification(
        self,
        node_id: uuid.UUID,
        decision: VerificationResult,
        *,
        session_id: str | None = None,
        status: NodeStatus = NodeStatus.COMPLETE,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.verification = decision
        if session_id and node.agent is not None:
            node.agent.session_id = session_id
        # A rejected review must not become a terminal leaf before the runner
        # routes it back to its correction target. Otherwise the scheduler can
        # finalize the containing project and cancel the review task in the
        # small window between persisting the decision and resetting the flow.
        node.status = status
        saved = await self._save_node(node)
        if saved is None:
            return None
        await self._emit_event(
            "verification.completed",
            project_id=saved.project_id,
            node_id=saved.id,
            data={
                "node_id": str(saved.id),
                "project_id": str(saved.project_id),
                "decision": decision.decision.value,
                "summary": decision.summary,
                "target_node_id": str(decision.target_node_id) if decision.target_node_id else None,
            },
            source="agent_action",
        )
        return saved

    async def reset_node_after_rejection(
        self,
        node_id: uuid.UUID,
        status: NodeStatus,
        *,
        agent_state: str | None = None,
        agent_message: str | None = None,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.status = status
        node.agent_state = sanitize_control_text(agent_state) if agent_state is not None else None
        node.agent_message = sanitize_control_text(agent_message) if agent_message is not None else None
        return await self._save_node(node)

    # -- settings ---------------------------------------------------------

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._settings.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        previous = self._settings.get(key)
        self._settings[key] = value
        await self._persist_config()
        if previous != value:
            await self._log(None, kind="configuration.changed", action=f"settings.{key}", message=f"setting {key} changed", data={"key": key, "from": previous, "to": value})

    # -- graph construction ----------------------------------------------

    async def create_node(
        self,
        *,
        project_id: uuid.UUID,
        parent_id: Optional[uuid.UUID],
        objective: str,
        generated_prompt: Optional[str] = None,
        executor: Optional[str] = None,
        required_inputs: Optional[list[InputSpec]] = None,
        resource_refs: Optional[list[str]] = None,
        agent: Optional[AgentConfig] = None,
        status: NodeStatus = NodeStatus.RUNNABLE,
    ) -> Node:
        node = Node(
            id=uuid.uuid4(), project_id=project_id, parent_id=parent_id,
            objective=objective, generated_prompt=generated_prompt,
            executor=executor, agent=agent, status=status,
            required_inputs=required_inputs or [], resource_refs=resource_refs or [],
        )
        state = self._state(project_id)
        state.nodes[node.id] = node
        if parent_id is not None:
            edge = Edge(src=parent_id, dst=node.id, type=EdgeType.CONTAINS)
            state.edges[edge.id] = edge
        await self._persist_project(project_id)
        await self._log(project_id, kind="graph.transition", action="node.created", message="graph node created", data={"node_id": str(node.id), "parent_id": str(parent_id) if parent_id else None, "objective": objective})
        return node.model_copy(deep=True)

    async def edit_node(
        self,
        node_id: uuid.UUID,
        objective: Optional[str] = None,
        generated_prompt: Optional[str] = None,
        required_inputs: Optional[list[InputSpec]] = None,
        resource_refs: Optional[list[str]] = None,
        agent: Optional[AgentConfig] = None,
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        if objective is not None:
            node.objective = objective
        if generated_prompt is not None:
            node.generated_prompt = generated_prompt
        if required_inputs is not None:
            node.required_inputs = required_inputs
        if resource_refs is not None:
            node.resource_refs = resource_refs
        if agent is not None:
            updated_agent = agent.model_copy(deep=True)
            if node.agent is not None and node.agent.harness != updated_agent.harness:
                updated_agent.session_id = None
            elif node.agent is not None and not updated_agent.session_id:
                # Inspector saves usually send the editable configuration
                # without the runtime session id. Do not accidentally erase
                # the resumable conversation while changing model, reasoning,
                # capabilities or tools.
                updated_agent.session_id = node.agent.session_id
            node.agent = updated_agent
            if node.executor == PLANNER_EXECUTOR:
                node.agent.type_id = "planner"
            elif node.executor in {"codex", "claude", "opencode", "pi"}:
                node.executor = node.agent.harness.value
        return await self._save_node(node)

    async def satisfy_input(self, node_id: uuid.UUID, input_id: str, value: str) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        artifact = (await self.add_artifacts(node_id, [ArtifactSpec(kind=ArtifactKind.USER_INPUT, name=f"input:{input_id}", content=value)]))[0]
        for item in node.required_inputs:
            if item.id == input_id and item.satisfied_by is None:
                item.satisfied_by = artifact.id
        node.artifact_refs = list(node.artifact_refs) + [artifact.id]
        return await self._save_node(node)

    async def replace_descendants(
        self,
        node_id: uuid.UUID,
        *,
        force: bool = False,
        preserved_refs: set[str] | None = None,
    ) -> list[uuid.UUID]:
        """Remove an old generated tree before planning it again.

        Regeneration is intentionally destructive in the MVP: there is no
        revision history, fork, or snapshot to maintain. The caller stops
        active workers first; this method removes the persisted subtree and
        its edges, runs, and artifacts in one state-file write.
        """
        node = await self.get_node(node_id)
        if node is None:
            return []
        descendants = await self.descendants(node_id)
        running = [item for item in descendants if item.status is NodeStatus.RUNNING]
        if running:
            ids = ", ".join(str(item.id) for item in running)
            raise RuntimeError(
                "cannot replace a graph containing running nodes: "
                f"{ids}; wait for them to finish or cancel them first"
            )
        existing_refs = {
            reference.ref
            for item in [node, *descendants]
            for reference in item.subgraph_refs
            if not reference.managed
        }
        missing_refs = existing_refs - (preserved_refs or set())
        if missing_refs and not force:
            raise RuntimeError(
                "graph contains composed subgraphs; preserve their links or "
                "resubmit with --force to replace them"
            )
        ids = {item.id for item in descendants}
        state = self._state(node.project_id)
        for key in list(state.nodes):
            if key in ids:
                del state.nodes[key]
        for key, edge in list(state.edges.items()):
            if edge.src in ids or edge.dst in ids:
                del state.edges[key]
        for key, run in list(state.runs.items()):
            if run.node_id in ids:
                del state.runs[key]
        for key, artifact in list(state.artifacts.items()):
            if artifact.node_id in ids:
                del state.artifacts[key]
        removed_work_items = {
            item_id
            for item_id, item in state.work_items.items()
            if item.node_id in ids
        }
        for item_id in removed_work_items:
            del state.work_items[item_id]
        for handoff_id, handoff in list(state.handoffs.items()):
            if (
                handoff.producer_node_id in ids
                or handoff.consumer_node_id in ids
            ):
                del state.handoffs[handoff_id]
        removed_triggers = [
            trigger_id
            for trigger_id, trigger in state.triggers.items()
            if trigger.target_node_id in ids
        ]
        for trigger_id in removed_triggers:
            del state.triggers[trigger_id]
        await self._persist_project(node.project_id)
        await self._log(
            node.project_id,
            kind="graph.transition",
            action="graph.replaced",
            message=f"replaced {len(descendants)} descendant node(s)",
            data={"node_id": str(node_id), "removed_node_ids": [str(item.id) for item in descendants]},
        )
        if removed_triggers:
            await self._log(
                node.project_id,
                kind="trigger.changed",
                action="triggers.removed",
                message=f"removed {len(removed_triggers)} trigger(s) with replaced nodes",
                data={"trigger_ids": [str(item) for item in removed_triggers]},
            )
        return [item.id for item in descendants]

    async def apply_plan(
        self,
        parent: Node,
        plan: PlanResult,
        *,
        enforce_organization_audit: bool = False,
    ) -> list[Node]:
        """Persist a graph mutation and materialize its accountable work.

        The runner enables the independent organization audit before applying a
        provider handoff. Direct storage callers may leave enforcement off for
        graph inspection and migration tools that intentionally preserve older
        plans while exposing their audit result.
        """
        project_root = self.project_path(parent.project_id)
        if plan.nodes and project_root is None:
            raise RuntimeError(f"project directory is not known: {parent.project_id}")
        contract = plan.organization_contract or parent.organization_contract
        if parent.organization_contract is not None:
            scale_rank = {
                OrganizationScale.FOCUSED: 0,
                OrganizationScale.DELIVERY: 1,
                OrganizationScale.ORGANIZATION: 2,
            }
            proposed = plan.organization_contract
            if (
                proposed is not None
                and scale_rank[proposed.scale]
                < scale_rank[parent.organization_contract.scale]
            ):
                raise RuntimeError(
                    "organization plan cannot downgrade its parent contract scale"
                )
            # The existing boundary remains the authority when the planner
            # omits a contract; an explicit same-or-broader proposal may
            # refine it.
            contract = proposed or parent.organization_contract
        audit = audit_plan(contract, plan) if contract is not None else None
        if enforce_organization_audit and audit is not None and not audit.accepted:
            raise RuntimeError("organization plan rejected: " + "; ".join(audit.errors))
        if contract is not None:
            parent.organization_contract = contract
            review = parent.organization_review or OrganizationReview()
            review.audit = audit
            review.phase = (
                OrganizationPhase.EXECUTE_FRONTIER
                if audit is None or audit.accepted
                else OrganizationPhase.REPLAN
            )
            review.replan_requested = bool(audit is not None and not audit.accepted)
            review.last_reason = (
                "; ".join(audit.errors)
                if audit is not None and audit.errors
                else "plan accepted"
            )
            review.reviewed_at = datetime.now(timezone.utc)
            parent.organization_review = review
        applied_plan = (
            plan.model_copy(update={"organization_contract": contract})
            if contract is not None
            else plan
        )
        created = apply_graph_plan(self._state(parent.project_id), parent, applied_plan)
        by_key = {
            spec.key: node
            for spec, node in zip(applied_plan.nodes, created, strict=True)
        }
        state = self._state(parent.project_id)
        work_items_by_key: dict[str, WorkItem] = {}
        ticketing_enabled = (
            contract is not None and contract.scale is not OrganizationScale.FOCUSED
        ) or any(spec.required_handoffs for spec in applied_plan.nodes)
        if ticketing_enabled:
            for spec, node in zip(applied_plan.nodes, created, strict=True):
                item = WorkItem(
                    project_id=parent.project_id,
                    organization_id=parent.id,
                    node_id=node.id,
                    key=spec.key,
                    agent_type=(node.agent.type_id.value if node.agent else "executor"),
                    organization_contract=node.organization_contract,
                    title=node.objective,
                    objective=node.generated_prompt or node.objective,
                    acceptance_criteria=list(node.acceptance_criteria),
                    priority=spec.priority,
                    status=self._work_item_status(node.status),
                )
                state.work_items[item.id] = item
                node.work_item_id = item.id
                state.nodes[node.id] = node.model_copy(deep=True)
                work_items_by_key[spec.key] = item
            for spec in applied_plan.nodes:
                item = work_items_by_key[spec.key]
                dependency_ids = [
                    work_items_by_key[pred].id
                    for pred in spec.follows
                    if pred in work_items_by_key
                ]
                dependency_ids.extend(
                    work_items_by_key[edge.src].id
                    for edge in applied_plan.edges
                    if edge.type is EdgeType.FOLLOWS
                    and edge.dst == spec.key
                    and edge.src in work_items_by_key
                )
                item.depends_on = list(dict.fromkeys(dependency_ids))
            for spec in applied_plan.nodes:
                consumer = by_key[spec.key]
                predecessors = [
                    by_key[pred]
                    for pred in spec.follows
                    if pred in by_key
                ]
                predecessors.extend(
                    by_key[edge.src]
                    for edge in applied_plan.edges
                    if edge.type is EdgeType.FOLLOWS
                    and edge.dst == spec.key
                    and edge.src in by_key
                )
                if not predecessors:
                    continue
                for predecessor in predecessors:
                    for contract_spec in spec.required_handoffs:
                        handoff = Handoff(
                            project_id=parent.project_id,
                            producer_node_id=predecessor.id,
                            consumer_node_id=consumer.id,
                            contract=contract_spec,
                        )
                        state.handoffs[handoff.id] = handoff
        created_trigger_ids: list[uuid.UUID] = []
        for spec in applied_plan.triggers:
            target = by_key[spec.target_key]
            trigger = Trigger(
                project_id=parent.project_id,
                target_node_id=target.id,
                event_name=spec.event_name,
                kind=spec.kind,
                schedule=spec.schedule,
                data=spec.data,
                enabled=spec.enabled,
            )
            self._state(parent.project_id).triggers[trigger.id] = trigger
            created_trigger_ids.append(trigger.id)
        # Role capabilities are part of Turn's agent contract.  They are
        # project-scoped packages, so materialize them as nodes are created;
        # user-selected capabilities remain subject to plan validation.
        catalog = CapabilityCatalog(self.data_dir / "capabilities")
        for node in created:
            for capability_id in capability_ids_for_agent_type(node.agent.type_id):
                catalog.load_into_project(capability_id, project_root)
        for node in created:
            self._sync_handoffs_for_node(node.id)
        await self._persist_project(parent.project_id)
        edge_count = sum(len(spec.follows) + (1 if spec.parent_key else 0) for spec in applied_plan.nodes) + len(applied_plan.edges)
        await self._log(parent.project_id, kind="graph.transition", action="plan.applied", message=f"plan applied; created {len(created)} node(s)", data={"parent_id": str(parent.id), "node_id": str(parent.id), "created_node_ids": [str(item.id) for item in created], "created_roles": [item.agent.type_id.value if item.agent else None for item in created], "edge_count": edge_count})
        if applied_plan.triggers:
            await self._log(
                parent.project_id,
                kind="trigger.changed",
                action="triggers.created",
                message=f"created {len(plan.triggers)} trigger(s)",
                data={"trigger_ids": [str(item) for item in created_trigger_ids]},
            )
        return created

    # -- triggers ---------------------------------------------------------

    async def list_triggers(self, project_id: uuid.UUID | None = None) -> list[Trigger]:
        values = [
            trigger
            for pid, state in self._states.items()
            if project_id is None or pid == project_id
            for trigger in state.triggers.values()
        ]
        return [trigger.model_copy(deep=True) for trigger in values]

    async def get_trigger(self, trigger_id: uuid.UUID) -> Optional[Trigger]:
        for state in self._states.values():
            trigger = state.triggers.get(trigger_id)
            if trigger is not None:
                return trigger.model_copy(deep=True)
        return None

    async def create_trigger(
        self,
        *,
        project_id: uuid.UUID,
        target_node_id: uuid.UUID,
        event_name: str | None = None,
        kind: TriggerKind = TriggerKind.EVENT,
        schedule: str | None = None,
        data: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> Trigger:
        target = await self.get_node(target_node_id)
        if target is None or target.project_id != project_id:
            raise ValueError("trigger target must belong to the project")
        trigger = Trigger(
            project_id=project_id,
            target_node_id=target_node_id,
            event_name=event_name,
            kind=kind,
            schedule=schedule,
            data=data or {},
            enabled=enabled,
        )
        self._state(project_id).triggers[trigger.id] = trigger
        await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="trigger.changed",
            action="trigger.created",
            message="trigger created",
            data={"trigger_id": str(trigger.id), "target_node_id": str(target_node_id), "event_name": event_name, "trigger_data": trigger.data},
        )
        return trigger.model_copy(deep=True)

    async def update_trigger(self, trigger_id: uuid.UUID, **changes: Any) -> Optional[Trigger]:
        current = await self.get_trigger(trigger_id)
        if current is None:
            return None
        if "data" in changes and changes["data"] is None:
            changes["data"] = {}
        updated = Trigger.model_validate({
            **current.model_dump(mode="python"),
            **changes,
            "updated_at": datetime.now(timezone.utc),
        })
        if updated.target_node_id != current.target_node_id:
            target = await self.get_node(updated.target_node_id)
            if target is None or target.project_id != current.project_id:
                raise ValueError("trigger target must belong to the project")
        self._state(current.project_id).triggers[trigger_id] = updated
        await self._persist_project(current.project_id)
        await self._log(current.project_id, kind="trigger.changed", action="trigger.updated", message="trigger updated", data={"trigger_id": str(trigger_id)})
        return updated.model_copy(deep=True)

    async def delete_trigger(self, trigger_id: uuid.UUID) -> bool:
        current = await self.get_trigger(trigger_id)
        if current is None:
            return False
        self._state(current.project_id).triggers.pop(trigger_id, None)
        await self._persist_project(current.project_id)
        await self._log(current.project_id, kind="trigger.changed", action="trigger.deleted", message="trigger deleted", data={"trigger_id": str(trigger_id)})
        return True

    async def mark_trigger_fired(self, trigger_id: uuid.UUID, when: datetime | None) -> Optional[Trigger]:
        current = await self.get_trigger(trigger_id)
        if current is None:
            return None
        current.last_fired_at = when
        self._state(current.project_id).triggers[trigger_id] = current
        await self._persist_project(current.project_id)
        return current.model_copy(deep=True)

    async def activate_trigger(self, trigger: Trigger, context: TriggerContext) -> list[Node]:
        """Reset the target flow and attach the event envelope to its entry node."""
        target = await self.get_node(trigger.target_node_id)
        if target is None:
            return []
        if (
            target.trigger_context is not None
            and target.trigger_context.event_id == context.event_id
        ):
            # Durable replay guard: this exact event was already applied to the
            # target (crash between activation and inbox-cursor save). Never
            # reset the flow twice for one event identity.
            await self._log(
                trigger.project_id,
                kind="trigger.activity",
                action="trigger.replay_skipped",
                message="trigger target already carries this event_id",
                data={"trigger_id": str(trigger.id), "target_node_id": str(target.id), "event_id": str(context.event_id)},
            )
            return []
        nodes, edges, _ = await self.get_workgraph(trigger.project_id)
        walker = GraphWalker(nodes, edges)
        index = walker.indexes
        affected: set[uuid.UUID] = {target.id}
        pending = [target.id]
        while pending:
            current = pending.pop()
            for child in index.children.get(current, []):
                if child not in affected:
                    affected.add(child)
                    pending.append(child)
            for successor in index.successors.get(current, []):
                if successor not in affected:
                    affected.add(successor)
                    pending.append(successor)
        state = self._state(trigger.project_id)
        activated: list[Node] = []
        async with self._project_lock(trigger.project_id):
            for node_id in affected:
                node = state.nodes.get(node_id)
                if node is None or node.status is NodeStatus.RUNNING:
                    continue
                node.trigger_context = context if node_id == target.id else None
                node.verification = None
                node.agent_state = None
                node.agent_message = None
                node.status = NodeStatus.RUNNABLE if node_id == target.id else NodeStatus.PENDING
                node.updated_at = datetime.now(timezone.utc)
                state.nodes[node_id] = node
                activated.append(node.model_copy(deep=True))
            await self._persist_project(trigger.project_id)
        await self._log(
            trigger.project_id,
            kind="trigger.activity",
            action="trigger.activated",
            message="trigger activated node flow",
            data={"trigger_id": str(trigger.id), "target_node_id": str(target.id), "event_id": str(context.event_id), "event_name": context.event_name},
        )
        return activated

    # -- runs -------------------------------------------------------------

    async def create_run(
        self,
        node: Node,
        worker: str,
        attempt: int = 1,
        *,
        process_owner_id: uuid.UUID | None = None,
    ) -> Run:
        # A new attempt inherits the current harness conversation; an explicit
        # fresh re-run clears node.agent.session_id before this method is called.
        run = Run(
            id=uuid.uuid4(),
            node_id=node.id,
            worker=worker,
            status=RunStatus.RUNNING,
            attempt=attempt,
            session_id=node.agent.session_id if node.agent else None,
            process_owner_id=process_owner_id or node.id,
            provider=(node.agent.harness.value if node.agent else worker),
        )
        async with self._project_lock(node.project_id):
            self._state(node.project_id).runs[run.id] = run
            await self._persist_project(node.project_id)
        await self._log(node.project_id, kind="harness.run", action="run.created", message="harness run created", data={"run_id": str(run.id), "node_id": str(node.id), "worker": worker, "attempt": attempt, "session_id": run.session_id})
        return run.model_copy(deep=True)

    async def get_run(self, run_id: uuid.UUID) -> Run | None:
        """Return one attempt without exposing mutable store state."""
        found = self._project_for_run(run_id)
        return found[1].model_copy(deep=True) if found else None

    async def active_run(self, node_id: uuid.UUID) -> Run | None:
        """Return the newest still-semantic-active attempt for a node."""
        runs = await self.get_runs(node_id)
        for run in reversed(runs):
            if run.status is RunStatus.RUNNING:
                return run
        return None

    async def mark_run_process(
        self,
        run_id: uuid.UUID,
        state: ProcessState,
        *,
        pid: int | None = None,
        pane_id: str | None = None,
        exit_code: int | None = None,
    ) -> Run:
        """Record supervision facts without changing semantic Run status."""
        found = self._project_for_run(run_id)
        if not found:
            raise KeyError(run_id)
        project_id, _ = found
        async with self._project_lock(project_id):
            current = self._states[project_id].runs.get(run_id)
            if current is None:
                raise KeyError(run_id)
            current.process_state = state
            if pid is not None:
                current.process_pid = pid
            if pane_id is not None:
                current.pane_id = pane_id
            now = datetime.now(timezone.utc)
            if state is ProcessState.RUNNING and current.process_started_at is None:
                current.process_started_at = now
            if state is ProcessState.EXITED:
                current.process_exited_at = now
                current.process_exit_code = exit_code
            await self._persist_project(project_id)
            saved = current.model_copy(deep=True)
        await self._log(
            project_id,
            kind="harness.process",
            action="process.state",
            message=f"process {state.value.lower()}",
            status="error" if state is ProcessState.EXITED and exit_code not in (None, 0) else "info",
            data={
                "run_id": str(run_id),
                "node_id": str(saved.node_id),
                "process_state": saved.process_state.value,
                "process_pid": saved.process_pid,
                "process_exit_code": saved.process_exit_code,
                "pane_id": saved.pane_id,
            },
        )
        return saved

    async def accept_run_submission(
        self,
        run_id: uuid.UUID,
        *,
        outcome: Outcome,
        node_status: NodeStatus | None = None,
        submission_id: uuid.UUID | None = None,
    ) -> Run | None:
        """Atomically accept the sole semantic submission for an attempt.

        This claims and settles the semantic handoff in one project-locked
        write. The runner may persist result materials and graph effects
        afterward, but competing watchers can no longer reinterpret the
        attempt. ``None`` means the attempt is stale, already settled, or cancelled. Process exit
        watchers must use :meth:`mark_run_process` instead and therefore
        cannot overwrite this decision.
        """
        found = self._project_for_run(run_id)
        if not found:
            return None
        project_id, _ = found
        async with self._project_lock(project_id):
            current = self._states[project_id].runs.get(run_id)
            node = self._states[project_id].nodes.get(current.node_id) if current else None
            if (
                current is None
                or node is None
                or current.status is not RunStatus.RUNNING
                or current.accepted_submission
                or node.status is NodeStatus.CANCELLED
            ):
                return None
            current.accepted_submission = True
            current.submission_id = submission_id or uuid.uuid4()
            current.outcome = outcome
            current.status = (
                RunStatus.FAILED if outcome is Outcome.FAIL else RunStatus.COMPLETE
            )
            current.ended_at = datetime.now(timezone.utc)
            # Graph materialization (artifacts, children, required inputs)
            # must finish before the scheduler sees the node's terminal graph
            # status. The Run claim and semantic outcome are atomic here;
            # callers apply ``node_status`` in the existing graph mutation
            # path immediately afterward so a scheduler tick cannot finalize
            # a half-materialized submission.
            node.agent_state = None
            node.agent_message = None
            await self._persist_project(project_id)
            saved = current.model_copy(deep=True)
        await self._log(
            project_id,
            kind="harness.submission",
            action="submission.accepted",
            message=f"accepted {outcome.value} submission",
            status="error" if outcome is Outcome.FAIL else "ok",
            data={
                "run_id": str(run_id),
                "node_id": str(saved.node_id),
                "outcome": outcome.value,
                "submission_id": str(saved.submission_id),
            },
        )
        return saved

    async def mark_submission_rejected(
        self, node_id: uuid.UUID, *, run_id: uuid.UUID | None, message: str
    ) -> bool:
        """Record correction-required feedback without failing a live Run."""
        found = self._project_for_node(node_id)
        if not found:
            return False
        project_id, _ = found
        async with self._project_lock(project_id):
            node = self._states[project_id].nodes.get(node_id)
            run = self._states[project_id].runs.get(run_id) if run_id else None
            if node is None or node.status is NodeStatus.CANCELLED:
                return False
            if run_id is not None and (
                run is None or run.node_id != node_id or run.status is not RunStatus.RUNNING
            ):
                return False
            node.agent_state = "correction_required"
            node.agent_message = sanitize_control_text(message)
            node.updated_at = datetime.now(timezone.utc)
            await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="harness.submission",
            action="submission.rejected",
            message=sanitize_control_text(message),
            status="error",
            data={"node_id": str(node_id), "run_id": str(run_id) if run_id else None},
        )
        return True

    def _project_for_run(self, run_id: uuid.UUID) -> tuple[uuid.UUID, Run] | None:
        for project_id, state in self._states.items():
            run = state.runs.get(run_id)
            if run is not None:
                return project_id, run
        return None

    async def update_run(
        self, run_id: uuid.UUID, *, status: Optional[RunStatus] = None,
        outcome=None, summary: Optional[str] = None, logs: Optional[str] = None,
        error: Optional[str] = None, retry_recommended: Optional[bool] = None,
        usage: Optional[Usage] = None, session_id: Optional[str] = None,
    ) -> Run:
        found = self._project_for_run(run_id)
        if not found:
            raise KeyError(run_id)
        project_id, _ = found
        async with self._project_lock(project_id):
            run = self._states[project_id].runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            # Semantic settlement is single-assignment. A late process watcher
            # or cancellation path may still report diagnostics, but it cannot
            # turn an accepted COMPLETE/BLOCK/EXPAND/FAIL submission into a
            # different semantic status.
            if (
                run.accepted_submission
                and run.status in {
                    RunStatus.COMPLETE,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
                and status in {RunStatus.FAILED, RunStatus.CANCELLED}
                and status is not run.status
            ):
                status = None
                outcome = None
            if status is not None:
                run.status = status
            if outcome is not None:
                run.outcome = outcome
            if summary is not None:
                run.summary = sanitize_control_text(summary)
            if logs is not None:
                run.logs = sanitize_control_text(logs)
            if error is not None:
                run.error = sanitize_control_text(error)
            elif status == RunStatus.COMPLETE:
                # A run may have been marked orphaned while its owner was
                # being registered. Completing that same run removes the
                # transient interruption marker.
                run.error = None
            if retry_recommended is not None:
                run.retry_recommended = retry_recommended
            elif status == RunStatus.COMPLETE:
                run.retry_recommended = False
            if usage is not None:
                run.usage = usage
            if session_id is not None:
                run.session_id = session_id
            if status in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED):
                run.ended_at = datetime.now(timezone.utc)
            await self._persist_project(project_id)
            saved = run.model_copy(deep=True)
        terminal_update = status in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED)
        await self._log(
            project_id,
            # Session discovery is a live progress update, not a completed
            # provider return. Keeping those facts distinct prevents the
            # Activity view from claiming a RUNNING harness has returned.
            kind="harness.return" if terminal_update else "harness.run",
            action="run.updated",
            message=(
                summary
                or error
                or ("harness session updated" if session_id is not None else f"run {saved.status.value} updated")
            ),
            status="error" if saved.status is RunStatus.FAILED else "ok" if saved.status is RunStatus.COMPLETE else "info",
            data={"run_id": str(saved.id), "node_id": str(saved.node_id), "status": saved.status.value, "outcome": _jsonable(saved.outcome), "error": saved.error, "summary": saved.summary, "session_id": saved.session_id, "usage": _jsonable(saved.usage)},
        )
        return saved

    async def get_runs(self, node_id: uuid.UUID) -> list[Run]:
        found = self._project_for_node(node_id)
        if found:
            project_id, _ = found
            return [run.model_copy(deep=True) for run in self._states[project_id].runs.values() if run.node_id == node_id]
        # Non-graph owners (e.g. the project lead terminal identity) still own
        # observable runs; scan every project for them.
        return [
            run.model_copy(deep=True)
            for state in self._states.values()
            for run in state.runs.values()
            if run.node_id == node_id
        ]

    async def get_project_runs(self, project_id: uuid.UUID) -> list[Run]:
        state = self._states.get(project_id, self._empty_state())
        ids = set(state.nodes)
        return [run.model_copy(deep=True) for run in state.runs.values() if run.node_id in ids]

    async def cancel_orphaned_runs(self, project_id: uuid.UUID, active_node_ids: set[uuid.UUID]) -> int:
        state = self._states.get(project_id)
        if state is None:
            return 0
        node_ids = set(state.nodes)
        changed = 0
        for run in state.runs.values():
            if run.node_id in node_ids and run.status == RunStatus.RUNNING and run.node_id not in active_node_ids:
                run.status = RunStatus.CANCELLED
                run.process_state = ProcessState.CANCELLED
                run.outcome = Outcome.FAIL
                run.ended_at = datetime.now(timezone.utc)
                run.error = "Run interrupted before this runner process started"
                run.retry_recommended = True
                node = state.nodes.get(run.node_id)
                if node is not None and node.status == NodeStatus.RUNNING:
                    # A persisted RUNNING node has no live task after a
                    # restart. Put it back on the scheduler so a direct
                    # filesystem worker can resume instead of leaving the
                    # graph permanently stuck.
                    node.status = NodeStatus.RUNNABLE
                    node.updated_at = datetime.now(timezone.utc)
                changed += 1
        if changed:
            await self._persist_project(project_id)
        return changed

    # -- artifacts --------------------------------------------------------

    async def add_artifacts(self, node_id: uuid.UUID, specs: list[ArtifactSpec]) -> list[Artifact]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        state = self._states[project_id]
        artifacts = append_artifacts(state, node_id, specs)
        node = state.nodes.get(node_id)
        if node is not None:
            self._sync_work_item(node)
        self._sync_handoffs_for_node(node_id)
        await self._persist_project(project_id)
        if artifacts:
            await self._log(project_id, kind="state.changed", action="artifact.created", message=f"created {len(artifacts)} artifact(s)", data={"node_id": str(node_id), "artifact_ids": [str(item.id) for item in artifacts]})
        return [artifact.model_copy(deep=True) for artifact in artifacts]

    async def add_document_refs(self, node_id: uuid.UUID, refs: list[DocumentRef]) -> list[Artifact]:
        """Attach dynamic refs without pretending that the target exists.

        A document reference is coordination metadata. A worker must submit an
        explicit file or link artifact when the referenced output is actually
        available; storing a ref alone must never create a placeholder artifact.
        """
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        state = self._states[project_id]
        node = state.nodes.get(node_id)
        if node is None:
            return []
        node.document_refs = merge_document_refs(node.document_refs, refs)
        await self._persist_project(project_id)
        await self._log(project_id, kind="state.changed", action="document_refs.updated", message="document references updated", data={"node_id": str(node_id), "references": [item.model_dump(mode="json") for item in refs]})
        return []

    async def add_subgraph_refs(self, node_id: uuid.UUID, refs) -> Optional[Node]:
        """Attach composed graph source links without importing their nodes."""
        found = self._project_for_node(node_id)
        if not found:
            return None
        project_id, _ = found
        state = self._states[project_id]
        node = state.nodes.get(node_id)
        if node is None:
            return None
        node.subgraph_refs = merge_subgraph_refs(node.subgraph_refs, refs)
        await self._persist_project(project_id)
        await self._log(
            project_id,
            kind="state.changed",
            action="subgraph_refs.updated",
            message="subgraph references updated",
            data={"node_id": str(node_id), "references": [item.model_dump(mode="json") for item in refs]},
        )
        return node.model_copy(deep=True)

    async def replace_subgraph_refs(self, node_id: uuid.UUID, refs) -> Optional[Node]:
        """Replace the source links owned by one planning boundary."""
        found = self._project_for_node(node_id)
        if not found:
            return None
        node = found[1].model_copy(deep=True)
        node.subgraph_refs = list(refs)
        return await self._save_node(node)

    async def get_artifacts(self, node_id: uuid.UUID) -> list[Artifact]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        return [artifact.model_copy(deep=True) for artifact in self._states[project_id].artifacts.values() if artifact.node_id == node_id]

    async def clear_generated_artifacts(self, node_id: uuid.UUID) -> list[uuid.UUID]:
        """Remove prior run outputs while retaining explicit user inputs.

        A fresh attempt replaces the node's generated result; keeping every
        prior handoff artifact makes the inspector look like the node produced
        multiple current outputs. User-supplied input artifacts remain attached
        because they are part of the node's requirements, not run output.
        """
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, node = found
        state = self._states[project_id]
        removed = [
            artifact_id
            for artifact_id, artifact in state.artifacts.items()
            if artifact.node_id == node_id and artifact.kind is not ArtifactKind.USER_INPUT
        ]
        if not removed:
            return []
        for artifact_id in removed:
            state.artifacts.pop(artifact_id, None)
        node.artifact_refs = [
            artifact_id
            for artifact_id in node.artifact_refs
            if artifact_id in state.artifacts
        ]
        self._sync_work_item(node)
        self._sync_handoffs_for_node(node_id)
        await self._persist_project(project_id)
        await self._log(project_id, kind="state.changed", action="artifacts.cleared", message=f"cleared {len(removed)} generated artifact(s)", data={"node_id": str(node_id), "artifact_ids": [str(item) for item in removed]})
        return removed

    async def get_artifact(self, artifact_id: uuid.UUID) -> Optional[Artifact]:
        for state in self._states.values():
            artifact = state.artifacts.get(artifact_id)
            if artifact is not None:
                return artifact.model_copy(deep=True)
        return None
