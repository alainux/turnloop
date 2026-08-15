"""Local project-file persistence for Turn.

Turn deliberately keeps the public ``Store`` interface small and async so the
runner and HTTP API do not need to know how state is persisted.  The storage
format is intentionally boring:

* ``<project>/.turn/state.json`` contains that project's nodes, edges, runs,
  and artifacts.
* ``./turn/config.json`` contains cross-project preferences and the project
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
    Edge,
    EdgeType,
    Graph,
    InputSpec,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    Run,
    RunPolicy,
    RunStatus,
    Usage,
)
from turn.graph.logic import GraphWalker

PLANNER_EXECUTOR = "planner"
STATE_VERSION = 2


def _concise_title(prompt: str, limit: int = 72) -> str:
    """Derive navigation copy while preserving the full authored prompt."""
    clean = " ".join(prompt.split())
    if len(clean) <= limit:
        return clean
    shortened = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}…"


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
    ):
        raw = str(data_dir or location or (Path.cwd() / "turn"))
        self.data_dir = self._resolve_data_dir(raw)
        self.config_path = Path(config_path).expanduser().resolve() if config_path else self.data_dir / "config.json"
        self._states: dict[uuid.UUID, dict[str, dict[uuid.UUID, Any]]] = {}
        self._project_paths: dict[uuid.UUID, Path] = {}
        self._settings: dict[str, str] = {}
        self._loaded = False
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _resolve_data_dir(raw: str) -> Path:
        return Path(raw).expanduser().resolve()

    @staticmethod
    def _state_path(project_path: Path) -> Path:
        return project_path / ".turn" / "state.json"

    @staticmethod
    def _empty_state() -> dict[str, dict[uuid.UUID, Any]]:
        return {"nodes": {}, "edges": {}, "runs": {}, "artifacts": {}}

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
                    self._project_paths[uuid.UUID(str(project_id))] = Path(path).expanduser().resolve()
                except (ValueError, TypeError):
                    continue

        # Also discover local projects if the config was copied or rebuilt.
        projects_root = self.data_dir / "projects"
        if projects_root.exists():
            for state_path in projects_root.glob("*/.turn/state.json"):
                try:
                    data = json.loads(state_path.read_text(encoding="utf-8"))
                    project_id = uuid.UUID(str(data["project_id"]))
                    self._project_paths.setdefault(project_id, state_path.parent.parent)
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    continue

        for project_id, project_path in list(self._project_paths.items()):
            state_path = self._state_path(project_path)
            if not state_path.exists():
                continue
            try:
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                self._states[project_id] = self._decode_state(raw)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                raise RuntimeError(f"could not read project state {state_path}: {error}") from error
        self._loaded = True

        await self._persist_config()

    def _decode_state(self, raw: dict[str, Any]) -> dict[str, dict[uuid.UUID, Any]]:
        state = self._empty_state()
        for item in raw.get("nodes", []):
            node = Node.model_validate(item)
            state["nodes"][node.id] = node
        for item in raw.get("edges", []):
            edge = Edge.model_validate(item)
            state["edges"][edge.id] = edge
        for item in raw.get("runs", []):
            run = Run.model_validate(item)
            state["runs"][run.id] = run
        for item in raw.get("artifacts", []):
            artifact = Artifact.model_validate(item)
            state["artifacts"][artifact.id] = artifact
        return state

    def _encode_state(self, project_id: uuid.UUID) -> dict[str, Any]:
        state = self._states[project_id]
        return {
            "version": STATE_VERSION,
            "project_id": str(project_id),
            "nodes": [self._model_dump(value) for value in state["nodes"].values()],
            "edges": [self._model_dump(value) for value in state["edges"].values()],
            "runs": [self._model_dump(value) for value in state["runs"].values()],
            "artifacts": [self._model_dump(value) for value in state["artifacts"].values()],
        }

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

    def _state(self, project_id: uuid.UUID) -> dict[str, dict[uuid.UUID, Any]]:
        return self._states.setdefault(project_id, self._empty_state())

    def _project_for_node(self, node_id: uuid.UUID) -> tuple[uuid.UUID, Node] | None:
        for project_id, state in self._states.items():
            node = state["nodes"].get(node_id)
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
        root_agent = (agent.model_copy(deep=True) if agent else AgentConfig()).as_type(
            AgentType.PLANNER
        )
        root_agent.session_id = None
        display_name = name or _concise_title(prompt)
        node = Node(
            id=root_id,
            project_id=root_id,
            objective=display_name,
            project_name=display_name,
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
        self._project_paths[root_id] = project_path
        state = self._empty_state()
        state["nodes"][node.id] = node
        self._states[root_id] = state
        await self._persist_project(root_id)
        await self._persist_config()
        return node.model_copy(deep=True)

    async def list_projects(self) -> list[Node]:
        return [
            state["nodes"][project_id].model_copy(deep=True)
            for project_id, state in self._states.items()
            if project_id in state["nodes"] and state["nodes"][project_id].parent_id is None
        ]

    async def delete_project(self, project_id: uuid.UUID) -> None:
        self._states.pop(project_id, None)
        self._project_paths.pop(project_id, None)
        await self._persist_config()

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
            for node in self._states[project_id]["nodes"].values()
            if node.parent_id == node_id
        ]

    async def get_workgraph(self, project_id: uuid.UUID):
        graph = await self.get_graph(project_id)
        return graph.nodes, graph.edges, graph.artifacts

    async def get_graph(self, project_id: uuid.UUID) -> Graph:
        state = self._states.get(project_id, self._empty_state())
        return Graph(
            project_id=project_id,
            nodes=[node.model_copy(deep=True) for node in state["nodes"].values()],
            edges=[edge.model_copy(deep=True) for edge in state["edges"].values()],
            artifacts=[artifact.model_copy(deep=True) for artifact in state["artifacts"].values()],
        )

    async def _load_graph(self, project_id: uuid.UUID):
        nodes, edges, _ = await self.get_workgraph(project_id)
        node_map = {node.id: node for node in nodes}
        children: dict[uuid.UUID | None, list[uuid.UUID]] = {}
        parents: dict[uuid.UUID, Optional[uuid.UUID]] = {}
        deps: dict[uuid.UUID, list[uuid.UUID]] = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node.id)
            parents[node.id] = node.parent_id
        for edge in edges:
            if edge.type == EdgeType.DEPENDS_ON:
                deps.setdefault(edge.dst, []).append(edge.src)
        return node_map, children, parents, deps

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

    async def prerequisites(self, node_id: uuid.UUID) -> list[Node]:
        current = await self.get_node(node_id)
        if current is None:
            return []
        nodes, edges, _ = await self.get_workgraph(current.project_id)
        return GraphWalker(nodes, edges).prerequisites(node_id)

    # -- node writes ------------------------------------------------------

    async def _save_node(self, node: Node) -> Node:
        if node.project_id not in self._states:
            return node
        saved = node.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        self._states[node.project_id]["nodes"][node.id] = saved
        await self._persist_project(node.project_id)
        return saved.model_copy(deep=True)

    async def set_status(self, node_id: uuid.UUID, status: NodeStatus) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None:
            return None
        node.status = status
        return await self._save_node(node)

    async def set_status_if_current(
        self, node_id: uuid.UUID, status: NodeStatus, expected: tuple[NodeStatus, ...]
    ) -> Optional[Node]:
        node = await self.get_node(node_id)
        if node is None or node.status not in expected:
            return None
        node.status = status
        return await self._save_node(node)

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

    # -- settings ---------------------------------------------------------

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        return self._settings.get(key, default)

    async def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = value
        await self._persist_config()

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
        state["nodes"][node.id] = node
        if parent_id is not None:
            edge = Edge(src=parent_id, dst=node.id, type=EdgeType.CONTAINS)
            state["edges"][edge.id] = edge
        await self._persist_project(project_id)
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
                # permissions, skills, or tools.
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

    async def replace_descendants(self, node_id: uuid.UUID) -> list[uuid.UUID]:
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
        ids = {item.id for item in descendants}
        state = self._state(node.project_id)
        for key in list(state["nodes"]):
            if key in ids:
                del state["nodes"][key]
        for key, edge in list(state["edges"].items()):
            if edge.src in ids or edge.dst in ids:
                del state["edges"][key]
        for key, run in list(state["runs"].items()):
            if run.node_id in ids:
                del state["runs"][key]
        for key, artifact in list(state["artifacts"].items()):
            if artifact.node_id in ids:
                del state["artifacts"][key]
        await self._persist_project(node.project_id)
        return [item.id for item in descendants]

    async def apply_plan(self, parent: Node, plan: PlanResult) -> list[Node]:
        if not plan.nodes:
            parent.status = NodeStatus.COMPLETE
            await self._save_node(parent)
            return []

        keys_to_ids = {spec.key: uuid.uuid4() for spec in plan.nodes}
        state = self._state(parent.project_id)
        created: list[Node] = []
        for spec in plan.nodes:
            node_id = keys_to_ids[spec.key]
            parent_id = keys_to_ids[spec.parent_key] if spec.parent_key else parent.id
            executor = PLANNER_EXECUTOR if (spec.plan or spec.executor == PLANNER_EXECUTOR) else (spec.executor or "codex")
            inherited_agent = parent.agent.model_copy(deep=True) if parent.agent else None
            generic_leaf = executor != PLANNER_EXECUTOR and executor == "codex"
            if generic_leaf and inherited_agent:
                executor = inherited_agent.harness.value
            if spec.agent:
                node_agent = spec.agent.model_copy(deep=True)
            elif executor == PLANNER_EXECUTOR:
                node_agent = inherited_agent or AgentConfig(type_id="planner")
            elif generic_leaf and inherited_agent:
                node_agent = inherited_agent
            elif inherited_agent and executor == inherited_agent.harness.value:
                node_agent = inherited_agent
            elif executor in {"codex", "claude", "opencode", "pi", "echo", "shell"}:
                node_agent = AgentConfig(harness=executor)
            else:
                node_agent = inherited_agent or AgentConfig()
            node_agent.session_id = None
            if not spec.agent:
                node_agent = node_agent.as_type(
                    AgentType.PLANNER if executor == PLANNER_EXECUTOR else AgentType.EXECUTOR
                )
            node = Node(
                id=node_id, project_id=parent.project_id, parent_id=parent_id,
                objective=spec.objective, generated_prompt=spec.generated_prompt,
                executor=executor, agent=node_agent, status=NodeStatus.PENDING,
                required_inputs=spec.required_inputs, resource_refs=spec.resource_refs,
            )
            state["nodes"][node.id] = node
            created.append(node)
            if not spec.parent_key:
                edge = Edge(src=parent.id, dst=node.id, type=EdgeType.CONTAINS)
                state["edges"][edge.id] = edge
        for spec in plan.nodes:
            if spec.parent_key:
                edge = Edge(src=keys_to_ids[spec.parent_key], dst=keys_to_ids[spec.key], type=EdgeType.CONTAINS)
                state["edges"][edge.id] = edge
            for dep in spec.depends_on:
                edge = Edge(src=keys_to_ids[dep], dst=keys_to_ids[spec.key], type=EdgeType.DEPENDS_ON)
                state["edges"][edge.id] = edge
        edge_keys = {(edge.src, edge.dst, edge.type) for edge in state["edges"].values()}
        for item in plan.edges:
            key = (keys_to_ids[item.src], keys_to_ids[item.dst], item.type)
            if key not in edge_keys:
                edge = Edge(src=key[0], dst=key[1], type=key[2])
                state["edges"][edge.id] = edge
                edge_keys.add(key)
        parent.status = NodeStatus.EXPANDED
        state["nodes"][parent.id] = parent.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        await self._persist_project(parent.project_id)
        return [node.model_copy(deep=True) for node in created]

    # -- runs -------------------------------------------------------------

    async def create_run(self, node: Node, worker: str, attempt: int = 1) -> Run:
        # A new attempt inherits the current harness conversation. Review
        # feedback therefore resumes the same session; an explicit fresh
        # re-run clears node.agent.session_id before this method is called.
        run = Run(
            id=uuid.uuid4(),
            node_id=node.id,
            worker=worker,
            status=RunStatus.RUNNING,
            attempt=attempt,
            session_id=node.agent.session_id if node.agent else None,
        )
        self._state(node.project_id)["runs"][run.id] = run
        await self._persist_project(node.project_id)
        return run.model_copy(deep=True)

    def _project_for_run(self, run_id: uuid.UUID) -> tuple[uuid.UUID, Run] | None:
        for project_id, state in self._states.items():
            run = state["runs"].get(run_id)
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
        if retry_recommended is not None:
            run.retry_recommended = retry_recommended
        if usage is not None:
            run.usage = usage
        if session_id is not None:
            run.session_id = session_id
        if status in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED):
            run.ended_at = datetime.now(timezone.utc)
        await self._persist_project(project_id)
        return run.model_copy(deep=True)

    async def get_runs(self, node_id: uuid.UUID) -> list[Run]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        return [run.model_copy(deep=True) for run in self._states[project_id]["runs"].values() if run.node_id == node_id]

    async def get_project_runs(self, project_id: uuid.UUID) -> list[Run]:
        state = self._states.get(project_id, self._empty_state())
        ids = set(state["nodes"])
        return [run.model_copy(deep=True) for run in state["runs"].values() if run.node_id in ids]

    async def cancel_orphaned_runs(self, project_id: uuid.UUID, active_node_ids: set[uuid.UUID]) -> int:
        state = self._states.get(project_id)
        if state is None:
            return 0
        node_ids = set(state["nodes"])
        changed = 0
        for run in state["runs"].values():
            if run.node_id in node_ids and run.status == RunStatus.RUNNING and run.node_id not in active_node_ids:
                run.status = RunStatus.CANCELLED
                run.outcome = Outcome.FAIL
                run.ended_at = datetime.now(timezone.utc)
                run.error = "Run interrupted before this runner process started"
                run.retry_recommended = True
                node = state["nodes"].get(run.node_id)
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
        artifacts: list[Artifact] = []
        for spec in specs:
            artifact = Artifact(id=uuid.uuid4(), node_id=node_id, kind=spec.kind, name=spec.name, content=spec.content, ref=spec.ref)
            state["artifacts"][artifact.id] = artifact
            artifacts.append(artifact)
        await self._persist_project(project_id)
        node = await self.get_node(node_id)
        if node is not None:
            node.artifact_refs = list(node.artifact_refs) + [artifact.id for artifact in artifacts]
            await self._save_node(node)
        return [artifact.model_copy(deep=True) for artifact in artifacts]

    async def get_artifacts(self, node_id: uuid.UUID) -> list[Artifact]:
        found = self._project_for_node(node_id)
        if not found:
            return []
        project_id, _ = found
        return [artifact.model_copy(deep=True) for artifact in self._states[project_id]["artifacts"].values() if artifact.node_id == node_id]

    async def get_artifact(self, artifact_id: uuid.UUID) -> Optional[Artifact]:
        for state in self._states.values():
            artifact = state["artifacts"].get(artifact_id)
            if artifact is not None:
                return artifact.model_copy(deep=True)
        return None
