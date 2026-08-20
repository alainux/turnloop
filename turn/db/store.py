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
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    VerificationResult,
    Run,
    RunPolicy,
    RunStatus,
    SubgraphRef,
    Trigger,
    TriggerContext,
    TriggerKind,
    Usage,
    NODE_OBJECTIVE_MAX_LENGTH,
    concise_node_title,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.domain.capability_contracts import SETUP_CAPABILITY_ID, capability_ids_for_agent_type
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

    async def _save_node(self, node: Node) -> Node:
        if node.project_id not in self._states:
            return node
        previous = self._states[node.project_id].nodes.get(node.id)
        saved = node.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        self._states[node.project_id].nodes[node.id] = saved
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

    async def set_agent_status(
        self, node_id: uuid.UUID, *, state: str | None, message: str | None
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.agent_state = state
        node.agent_message = message
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
        self, node_id: uuid.UUID, status: NodeStatus
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.status = status
        node.agent_state = None
        node.agent_message = None
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

    async def apply_plan(self, parent: Node, plan: PlanResult) -> list[Node]:
        """Persist a validated graph mutation without interpreting its policy."""
        project_root = self.project_path(parent.project_id)
        if plan.nodes and project_root is None:
            raise RuntimeError(f"project directory is not known: {parent.project_id}")
        created = apply_graph_plan(self._state(parent.project_id), parent, plan)
        by_key = {spec.key: node for spec, node in zip(plan.nodes, created, strict=True)}
        created_trigger_ids: list[uuid.UUID] = []
        for spec in plan.triggers:
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
        await self._persist_project(parent.project_id)
        edge_count = sum(len(spec.follows) + (1 if spec.parent_key else 0) for spec in plan.nodes) + len(plan.edges)
        await self._log(parent.project_id, kind="graph.transition", action="plan.applied", message=f"plan applied; created {len(created)} node(s)", data={"parent_id": str(parent.id), "node_id": str(parent.id), "created_node_ids": [str(item.id) for item in created], "created_roles": [item.agent.type_id.value if item.agent else None for item in created], "edge_count": edge_count})
        if plan.triggers:
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

    async def mark_trigger_fired(self, trigger_id: uuid.UUID, when: datetime) -> Optional[Trigger]:
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

    async def create_run(self, node: Node, worker: str, attempt: int = 1) -> Run:
        # A new attempt inherits the current harness conversation; an explicit
        # fresh re-run clears node.agent.session_id before this method is called.
        run = Run(
            id=uuid.uuid4(),
            node_id=node.id,
            worker=worker,
            status=RunStatus.RUNNING,
            attempt=attempt,
            session_id=node.agent.session_id if node.agent else None,
        )
        self._state(node.project_id).runs[run.id] = run
        await self._persist_project(node.project_id)
        await self._log(node.project_id, kind="harness.run", action="run.created", message="harness run created", data={"run_id": str(run.id), "node_id": str(node.id), "worker": worker, "attempt": attempt, "session_id": run.session_id})
        return run.model_copy(deep=True)

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
        project_id, run = found
        if status is not None:
            run.status = status
        if outcome is not None:
            run.outcome = outcome
        if summary is not None:
            run.summary = summary
        if logs is not None:
            run.logs = logs
        if error is not None:
            run.error = error
        elif status == RunStatus.COMPLETE:
            # A run may have been marked orphaned while its owner was being
            # registered. Completing that same run must remove the transient
            # interruption marker instead of presenting a contradictory
            # COMPLETE + error record.
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
                or ("harness session updated" if session_id is not None else f"run {run.status.value} updated")
            ),
            status="error" if run.status is RunStatus.FAILED else "ok" if run.status is RunStatus.COMPLETE else "info",
            data={"run_id": str(run.id), "node_id": str(run.node_id), "status": run.status.value, "outcome": _jsonable(run.outcome), "error": run.error, "summary": run.summary, "session_id": run.session_id, "usage": _jsonable(run.usage)},
        )
        return run.model_copy(deep=True)

    async def get_runs(self, node_id: uuid.UUID) -> list[Run]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        return [run.model_copy(deep=True) for run in self._states[project_id].runs.values() if run.node_id == node_id]

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
        await self._persist_project(project_id)
        await self._log(project_id, kind="state.changed", action="artifacts.cleared", message=f"cleared {len(removed)} generated artifact(s)", data={"node_id": str(node_id), "artifact_ids": [str(item) for item in removed]})
        return removed

    async def get_artifact(self, artifact_id: uuid.UUID) -> Optional[Artifact]:
        for state in self._states.values():
            artifact = state.artifacts.get(artifact_id)
            if artifact is not None:
                return artifact.model_copy(deep=True)
        return None
