"""Repository: the authoritative workgraph + state store.

Turn owns the workgraph and node state. This layer is a thin mapping over
SQLAlchemy; it knows nothing about workers or orchestration.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from turn.db.base import make_engine, make_session_factory
from turn.db.models import (
    ArtifactModel,
    Base,
    EdgeModel,
    GraphInspectionModel,
    NodeModel,
    RunModel,
    SchemaVersionModel,
    SettingModel,
)
from turn.domain.schemas import (
    Artifact,
    AgentConfig,
    ArtifactKind,
    ArtifactSpec,
    Edge,
    EdgeType,
    InputSpec,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    Run,
    RunPolicy,
    RunStatus,
    Usage,
    VerificationStatus,
)

# Planner is the only executor the runner treats as a planning operation.
PLANNER_EXECUTOR = "planner"


def _concise_title(prompt: str, limit: int = 72) -> str:
    """Derive navigation copy while preserving the full authored prompt."""
    clean = " ".join(prompt.split())
    if len(clean) <= limit:
        return clean
    shortened = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}…"


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


def _node_from_model(m: NodeModel) -> Node:
    return Node(
        id=m.id,
        project_id=m.project_id,
        parent_id=m.parent_id,
        objective=m.objective,
        project_name=getattr(m, "project_name", None),
        generated_prompt=m.generated_prompt,
        executor=m.executor,
        agent=AgentConfig(**m.agent_config) if m.agent_config else None,
        repo_path=m.repo_path,
        status=NodeStatus(m.status),
        paused=m.paused,
        auto_run=m.auto_run,
        run_policy=RunPolicy(**m.run_policy) if m.run_policy else None,
        required_inputs=[InputSpec(**d) for d in (m.required_inputs or [])],
        resource_refs=list(m.resource_refs or []),
        artifact_refs=[uuid.UUID(x) for x in (m.artifact_refs or [])],
        revision=m.revision,
        superseded_by=m.superseded_by,
        forked_from=m.forked_from,
        needs_review=bool(m.needs_review),
        merge_accepted=bool(m.merge_accepted),
        verification_status=(
            VerificationStatus(m.verification_status)
            if getattr(m, "verification_status", None) else None
        ),
        verification_summary=getattr(m, "verification_summary", None),
        verification_round=int(getattr(m, "verification_round", 0) or 0),
        verification_session_id=getattr(m, "verification_session_id", None),
        created_at=m.created_at,
        updated_at=m.updated_at,
    )


def _edge_from_model(m: EdgeModel) -> Edge:
    return Edge(id=m.id, src=m.src, dst=m.dst, type=EdgeType(m.type), created_at=m.created_at)


def _run_from_model(m: RunModel) -> Run:
    return Run(
        id=m.id,
        node_id=m.node_id,
        worker=m.worker,
        started_at=m.started_at,
        ended_at=m.ended_at,
        status=RunStatus(m.status),
        outcome=Outcome(m.outcome) if m.outcome else None,
        summary=m.summary,
        logs=m.logs or "",
        error=m.error,
        retry_recommended=m.retry_recommended,
        node_revision=m.node_revision,
        attempt=getattr(m, "attempt", 1) or 1,
        usage=Usage(**(getattr(m, "usage", None) or {})),
        session_id=getattr(m, "session_id", None),
    )


def _artifact_from_model(m: ArtifactModel) -> Artifact:
    return Artifact(
        id=m.id,
        node_id=m.node_id,
        kind=ArtifactKind(m.kind),
        name=m.name,
        content=m.content,
        ref=m.ref,
        created_at=m.created_at,
    )


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class Store:
    def __init__(self, database_url: str):
        self.url = database_url
        self.engine: AsyncEngine = make_engine(database_url)
        self.Session: async_sessionmaker[AsyncSession] = make_session_factory(self.engine)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate()
        await self._canonicalize_planner_agents()

    async def _canonicalize_planner_agents(self) -> None:
        """Keep the node → agent → type invariant true for old databases."""
        async with self.session() as s:
            rows = (
                await s.execute(select(NodeModel).where(NodeModel.executor == PLANNER_EXECUTOR))
            ).scalars().all()
            changed = False
            for row in rows:
                config = dict(row.agent_config or {})
                if config.get("type_id") == "planner":
                    continue
                config["type_id"] = "planner"
                row.agent_config = config
                changed = True
            if changed:
                await s.commit()

    async def _migrate(self) -> None:
        """Apply small, ordered, idempotent schema migrations.

        Turn intentionally avoids a heavyweight migration dependency for its
        single-user SQLite MVP, but still records versions and inspects the
        schema instead of using exception handling as control flow.
        """
        node_columns = {
            "auto_run": "BOOLEAN NOT NULL DEFAULT 1",
            "needs_review": "BOOLEAN NOT NULL DEFAULT 0",
            "merge_accepted": "BOOLEAN NOT NULL DEFAULT 0",
            "repo_path": "TEXT",
            "agent_config": "JSON",
            "run_policy": "JSON",
            "project_name": "VARCHAR(72)",
            "verification_status": "VARCHAR(16)",
            "verification_summary": "TEXT",
            "verification_round": "INTEGER NOT NULL DEFAULT 0",
            "verification_session_id": "VARCHAR(255)",
        }
        run_columns = {
            "attempt": "INTEGER NOT NULL DEFAULT 1",
            "usage": "JSON NOT NULL DEFAULT '{}'",
            "session_id": "VARCHAR(255)",
        }
        async with self.engine.begin() as conn:
            existing = await conn.run_sync(
                lambda sync: {
                    table: {c["name"] for c in inspect(sync).get_columns(table)}
                    for table in ("nodes", "runs")
                }
            )
            for name, ddl in node_columns.items():
                if name not in existing["nodes"]:
                    await conn.execute(text(f"ALTER TABLE nodes ADD COLUMN {name} {ddl}"))
            for name, ddl in run_columns.items():
                if name not in existing["runs"]:
                    await conn.execute(text(f"ALTER TABLE runs ADD COLUMN {name} {ddl}"))
            version = await conn.scalar(select(SchemaVersionModel.version).order_by(SchemaVersionModel.version.desc()).limit(1))
            if not version or version < 2:
                await conn.execute(SchemaVersionModel.__table__.insert().values(version=2))

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.Session() as s:
            yield s

    # -- projects ---------------------------------------------------------

    async def create_project(
        self, prompt: str, name: Optional[str] = None, repo_path: Optional[str] = None,
        id=None, agent: Optional[AgentConfig] = None, run_policy: Optional[RunPolicy] = None,
    ) -> Node:
        # New projects inherit the user's last auto-run preference (persisted
        # across projects) so manual-stepping mode survives a page reload.
        auto_run_default = True
        try:
            raw = await self.get_setting("default_auto_run", "1")
            auto_run_default = str(raw) not in ("0", "false", "False", "")
        except Exception:
            auto_run_default = True
        root_id = id or uuid.uuid4()
        policy = run_policy or RunPolicy(auto_run=auto_run_default)
        root_agent = agent.model_copy(deep=True) if agent else AgentConfig()
        root_agent.type_id = "planner"
        root_agent.session_id = None
        display_name = name or _concise_title(prompt)
        node = NodeModel(
            id=root_id,
            project_id=root_id,  # a project IS its root node
            parent_id=None,
            objective=display_name,
            project_name=display_name,
            generated_prompt=prompt,
            executor=PLANNER_EXECUTOR,
            status=NodeStatus.PENDING.value,
            auto_run=policy.auto_run,
            agent_config=root_agent.model_dump(mode="json"),
            run_policy=policy.model_dump(mode="json"),
            repo_path=repo_path,
        )
        async with self.session() as s:
            s.add(node)
            await s.commit()
            await s.refresh(node)
            return _node_from_model(node)

    async def list_projects(self) -> list[Node]:
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(NodeModel).where(NodeModel.parent_id.is_(None))
                )
            ).scalars().all()
            return [_node_from_model(m) for m in rows]

    async def delete_project(self, project_id: uuid.UUID) -> None:
        """Delete a single project and all of its nodes/edges/runs/artifacts."""
        ids = select(NodeModel.id).where(NodeModel.project_id == project_id)
        async with self.session() as s:
            await s.execute(
                delete(GraphInspectionModel).where(
                    GraphInspectionModel.project_id == project_id
                )
            )
            await s.execute(delete(ArtifactModel).where(ArtifactModel.node_id.in_(ids)))
            await s.execute(delete(RunModel).where(RunModel.node_id.in_(ids)))
            await s.execute(
                delete(EdgeModel).where(
                    (EdgeModel.src.in_(ids)) | (EdgeModel.dst.in_(ids))
                )
            )
            await s.execute(delete(NodeModel).where(NodeModel.project_id == project_id))
            await s.commit()

    async def get_graph_inspections(self, project_id: uuid.UUID) -> list[dict]:
        """Return graph-tool evidence in chronological order."""
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(GraphInspectionModel)
                    .where(GraphInspectionModel.project_id == project_id)
                    .order_by(GraphInspectionModel.created_at)
                )
            ).scalars().all()
            return [
                {
                    "id": str(row.id),
                    "project_id": str(row.project_id),
                    "requester_node_id": str(row.requester_node_id),
                    "query": row.query,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def clear_projects(self) -> None:
        """Remove every project and all associated data (keeps settings)."""
        async with self.session() as s:
            await s.execute(delete(GraphInspectionModel))
            await s.execute(delete(ArtifactModel))
            await s.execute(delete(RunModel))
            await s.execute(delete(EdgeModel))
            await s.execute(delete(NodeModel))
            await s.commit()

    # -- node reads ------------------------------------------------------

    async def get_node(self, node_id: uuid.UUID) -> Optional[Node]:
        async with self.session() as s:
            m = (
                await s.execute(select(NodeModel).where(NodeModel.id == node_id))
            ).scalar_one_or_none()
            return _node_from_model(m) if m else None

    async def children_of(self, node_id: uuid.UUID) -> list[Node]:
        async with self.session() as s:
            rows = (
                await s.execute(select(NodeModel).where(NodeModel.parent_id == node_id))
            ).scalars().all()
            return [_node_from_model(m) for m in rows]

    async def get_workgraph(self, project_id: uuid.UUID):
        """Return (nodes, edges, artifacts) for everything under a root."""
        async with self.session() as s:
            node_rows = (
                await s.execute(
                    select(NodeModel).where(NodeModel.project_id == project_id)
                )
            ).scalars().all()
            node_ids = [n.id for n in node_rows]
            edge_rows = []
            art_rows = []
            if node_ids:
                edge_rows = (
                    await s.execute(
                        select(EdgeModel).where(
                            EdgeModel.src.in_(node_ids) | EdgeModel.dst.in_(node_ids)
                        )
                    )
                ).scalars().all()
                art_rows = (
                    await s.execute(
                        select(ArtifactModel).where(
                            ArtifactModel.node_id.in_(node_ids)
                        )
                    )
                ).scalars().all()
            return (
                [_node_from_model(m) for m in node_rows],
                [_edge_from_model(m) for m in edge_rows],
                [_artifact_from_model(m) for m in art_rows],
            )

    async def _load_graph(self, project_id: uuid.UUID):
        """Load all project nodes/edges once for in-memory graph reasoning."""
        async with self.session() as s:
            nodes = (
                await s.execute(
                    select(NodeModel).where(NodeModel.project_id == project_id)
                )
            ).scalars().all()
            edges = (
                (await s.execute(select(EdgeModel))).scalars().all()
                if nodes
                else []
            )
            node_map = {m.id: _node_from_model(m) for m in nodes}
            children: dict[uuid.UUID, list[uuid.UUID]] = {}
            parents: dict[uuid.UUID, Optional[uuid.UUID]] = {}
            deps: dict[uuid.UUID, list[uuid.UUID]] = {}
            for m in nodes:
                children.setdefault(m.parent_id, []).append(m.id)
                parents[m.id] = m.parent_id
            for e in edges:
                if e.type == EdgeType.DEPENDS_ON.value:
                    deps.setdefault(e.dst, []).append(e.src)
            return node_map, children, parents, deps

    async def ancestry(self, node_id: uuid.UUID) -> list[Node]:
        node_map, _, parents, _ = await self._load_graph(
            (await self.get_node(node_id)).project_id
        )
        out: list[Node] = []
        cur = parents.get(node_id)
        while cur is not None and cur in node_map:
            out.append(node_map[cur])
            cur = parents.get(cur)
        out.reverse()
        return out

    async def descendants(self, node_id: uuid.UUID) -> list[Node]:
        node_map, children, _, _ = await self._load_graph(
            (await self.get_node(node_id)).project_id
        )
        out: list[Node] = []
        stack = list(children.get(node_id, []))
        while stack:
            nid = stack.pop()
            if nid in node_map:
                out.append(node_map[nid])
                stack.extend(children.get(nid, []))
        return out

    async def prerequisites(self, node_id: uuid.UUID) -> list[Node]:
        node_map, _, _, deps = await self._load_graph(
            (await self.get_node(node_id)).project_id
        )
        return [node_map[d] for d in deps.get(node_id, []) if d in node_map]

    # -- node writes -----------------------------------------------------

    async def _save_node(self, node: Node) -> Node:
        async with self.session() as s:
            m = await s.get(NodeModel, node.id)
            if m is None:
                return node
            m.objective = node.objective
            m.project_name = node.project_name
            m.generated_prompt = node.generated_prompt
            m.executor = node.executor
            m.agent_config = node.agent.model_dump(mode="json") if node.agent else None
            m.repo_path = node.repo_path
            m.status = node.status.value
            m.paused = node.paused
            m.auto_run = node.auto_run
            m.run_policy = node.run_policy.model_dump(mode="json") if node.run_policy else None
            m.required_inputs = [d.model_dump(mode="json") for d in node.required_inputs]
            m.resource_refs = list(node.resource_refs)
            m.artifact_refs = [str(x) for x in node.artifact_refs]
            m.revision = node.revision
            m.superseded_by = node.superseded_by
            m.forked_from = node.forked_from
            m.needs_review = bool(node.needs_review)
            m.merge_accepted = bool(node.merge_accepted)
            m.verification_status = (
                node.verification_status.value if node.verification_status else None
            )
            m.verification_summary = node.verification_summary
            m.verification_round = node.verification_round
            m.verification_session_id = node.verification_session_id
            m.updated_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(m)
            return _node_from_model(m)

    async def set_status(self, node_id: uuid.UUID, status: NodeStatus) -> Optional[Node]:
        n = await self.get_node(node_id)
        if n is None:
            return None
        n.status = status
        return await self._save_node(n)

    async def set_status_if_current(
        self,
        node_id: uuid.UUID,
        status: NodeStatus,
        expected: tuple[NodeStatus, ...],
    ) -> Optional[Node]:
        """Atomically update status only while it remains in ``expected``."""
        async with self.session() as s:
            result = await s.execute(
                update(NodeModel)
                .where(
                    NodeModel.id == node_id,
                    NodeModel.status.in_([value.value for value in expected]),
                )
                .values(status=status.value, updated_at=datetime.now(timezone.utc))
            )
            await s.commit()
            if not result.rowcount:
                return None
        return await self.get_node(node_id)

    async def set_paused(self, node_id: uuid.UUID, paused: bool) -> Optional[Node]:
        n = await self.get_node(node_id)
        if n is None:
            return None
        n.paused = paused
        return await self._save_node(n)

    async def set_auto_run(self, project_id: uuid.UUID, auto_run: bool) -> Optional[Node]:
        n = await self.get_node(project_id)
        if n is None:
            return None
        n.auto_run = auto_run
        return await self._save_node(n)

    # -- settings (cross-project preferences) -----------------------------

    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self.session() as s:
            m = (
                await s.execute(select(SettingModel).where(SettingModel.key == key))
            ).scalar_one_or_none()
            return m.value if m else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.session() as s:
            m = (
                await s.execute(select(SettingModel).where(SettingModel.key == key))
            ).scalar_one_or_none()
            if m is None:
                m = SettingModel(key=key, value=value)
                s.add(m)
            else:
                m.value = value
            await s.commit()

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
        forked_from: Optional[uuid.UUID] = None,
        status: NodeStatus = NodeStatus.RUNNABLE,
    ) -> Node:
        node = NodeModel(
            id=uuid.uuid4(),
            project_id=project_id,
            parent_id=parent_id,
            objective=objective,
            generated_prompt=generated_prompt,
            executor=executor,
            agent_config=agent.model_dump(mode="json") if agent else None,
            status=status.value,
            required_inputs=[d.model_dump(mode="json") for d in (required_inputs or [])],
            resource_refs=list(resource_refs or []),
            forked_from=forked_from,
        )
        async with self.session() as s:
            s.add(node)
            if parent_id is not None:
                s.add(
                    EdgeModel(
                        id=uuid.uuid4(),
                        src=parent_id,
                        dst=node.id,
                        type=EdgeType.CONTAINS.value,
                    )
                )
            await s.commit()
            await s.refresh(node)
            return _node_from_model(node)

    async def edit_node(
        self,
        node_id: uuid.UUID,
        objective: Optional[str] = None,
        generated_prompt: Optional[str] = None,
        required_inputs: Optional[list[InputSpec]] = None,
        resource_refs: Optional[list[str]] = None,
        agent: Optional[AgentConfig] = None,
    ) -> Optional[Node]:
        """Create a new revision (does not destructively rewrite history)."""
        n = await self.get_node(node_id)
        if n is None:
            return None
        # snapshot prior state as an artifact so history is preserved
        prior = {
            "objective": n.objective,
            "generated_prompt": n.generated_prompt,
            "required_inputs": [d.model_dump(mode="json") for d in n.required_inputs],
            "agent": n.agent.model_dump(mode="json") if n.agent else None,
            "revision": n.revision,
        }
        async with self.session() as s:
            art = ArtifactModel(
                id=uuid.uuid4(),
                node_id=node_id,
                kind=ArtifactKind.JSON.value,
                name=f"revision-{n.revision}-snapshot",
                content=prior,
            )
            s.add(art)
            await s.commit()

        if objective is not None:
            n.objective = objective
        if generated_prompt is not None:
            n.generated_prompt = generated_prompt
        if required_inputs is not None:
            n.required_inputs = required_inputs
        if resource_refs is not None:
            n.resource_refs = resource_refs
        if agent is not None:
            updated_agent = agent.model_copy(deep=True)
            if n.agent is not None and n.agent.harness != updated_agent.harness:
                # Provider session identifiers are not portable. Enforce this
                # below every UI/API/CLI caller, not merely in the browser.
                updated_agent.session_id = None
            n.agent = updated_agent
            if n.executor == PLANNER_EXECUTOR:
                n.agent.type_id = "planner"
            elif n.executor in {"codex", "claude", "opencode", "pi"}:
                n.executor = n.agent.harness.value
        n.revision += 1
        return await self._save_node(n)

    async def satisfy_input(
        self, node_id: uuid.UUID, input_id: str, value: str
    ) -> Optional[Node]:
        n = await self.get_node(node_id)
        if n is None:
            return None
        art_id = uuid.uuid4()
        async with self.session() as s:
            s.add(
                ArtifactModel(
                    id=art_id,
                    node_id=node_id,
                    kind=ArtifactKind.USER_INPUT.value,
                    name=f"input:{input_id}",
                    content=value,
                )
            )
            await s.commit()
        changed = False
        for inp in n.required_inputs:
            if inp.id == input_id and inp.satisfied_by is None:
                inp.satisfied_by = art_id
                changed = True
        if changed:
            n.artifact_refs = list(n.artifact_refs) + [art_id]
            n = await self._save_node(n)
        return n

    async def supersede_branch(self, node_id: uuid.UUID) -> list[uuid.UUID]:
        """Mark all active descendants of `node_id` as cancelled (superseded)."""
        desc = await self.descendants(node_id)
        cancelled: list[uuid.UUID] = []
        for d in desc:
            if d.status == NodeStatus.CANCELLED:
                continue
            d.status = NodeStatus.CANCELLED
            d.superseded_by = node_id
            # A superseded result can no longer gate the replacement branch.
            # Its runs and verification artifacts remain as history, while
            # active review ownership is released.
            d.needs_review = False
            await self._save_node(d)
            cancelled.append(d.id)
        return cancelled

    # -- plan application ------------------------------------------------

    async def apply_plan(self, parent: Node, plan: PlanResult) -> list[Node]:
        """Persist a structurally valid plan without changing its semantics."""
        if not plan.nodes:
            # nothing to do -> the (sub)tree is complete as-is
            parent.status = NodeStatus.COMPLETE
            await self._save_node(parent)
            return []

        keys_to_ids = {spec.key: uuid.uuid4() for spec in plan.nodes}
        new_models: list[NodeModel] = []
        new_edges: list[EdgeModel] = []

        project_id = parent.project_id
        for spec in plan.nodes:
            nid = keys_to_ids[spec.key]
            parent_id = (
                keys_to_ids[spec.parent_key] if spec.parent_key else parent.id
            )
            # A node flagged plan:true (or explicitly a planner) is itself a
            # sub-planner: the runner decomposes it again on its next turn
            # rather than executing it as a leaf worker.
            executor = (
                PLANNER_EXECUTOR
                if (spec.plan or spec.executor == PLANNER_EXECUTOR)
                else (spec.executor or "codex")
            )
            inherited_agent = parent.agent.model_copy(deep=True) if parent.agent else None
            generic_leaf = executor != PLANNER_EXECUTOR and executor == "codex"
            if generic_leaf and inherited_agent:
                # "codex" in a generated plan means generic coding work. The
                # project-selected harness resolves it at this adapter boundary.
                executor = inherited_agent.harness.value
            if spec.agent:
                agent = spec.agent.model_copy(deep=True)
            elif executor == PLANNER_EXECUTOR:
                agent = inherited_agent or AgentConfig(type_id="planner")
            elif generic_leaf and inherited_agent:
                # Preserve the complete selected configuration, not only its
                # harness: custom model, reasoning and permission all inherit.
                agent = inherited_agent
            elif inherited_agent and executor == inherited_agent.harness.value:
                # A planner may spell out the already-selected harness. That
                # is not an instruction to discard its model, effort,
                # permission or resources; inherit the complete assignment.
                agent = inherited_agent
            elif executor in {"codex", "claude", "opencode", "pi", "echo", "shell"}:
                # An explicit executor is an adapter choice. Inherit project
                # defaults only for generic `codex` work resolved above.
                agent = AgentConfig(harness=executor)
            else:
                agent = inherited_agent or AgentConfig()
            # Children inherit execution configuration, never conversation
            # identity. A provider session belongs to exactly one graph node;
            # sharing the planner's session with a new implementer cross-wires
            # prompts and makes review recovery resume the wrong agent.
            agent.session_id = None
            if not spec.agent:
                agent.type_id = "planner" if executor == PLANNER_EXECUTOR else "general"
            new_models.append(
                NodeModel(
                    id=nid,
                    project_id=project_id,
                    parent_id=parent_id,
                    objective=spec.objective,
                    generated_prompt=spec.generated_prompt,
                    executor=executor,
                    agent_config=agent.model_dump(mode="json"),
                    status=NodeStatus.PENDING.value,
                    required_inputs=[d.model_dump(mode="json") for d in spec.required_inputs],
                    resource_refs=list(spec.resource_refs),
                )
            )
            # implicit CONTAINS from planning parent to top-level children
            if not spec.parent_key:
                new_edges.append(
                    EdgeModel(
                        id=uuid.uuid4(),
                        src=parent.id,
                        dst=nid,
                        type=EdgeType.CONTAINS.value,
                    )
                )

        # explicit CONTAINS edges for nested specs
        for spec in plan.nodes:
            if spec.parent_key:
                new_edges.append(
                    EdgeModel(
                        id=uuid.uuid4(),
                        src=keys_to_ids[spec.parent_key],
                        dst=keys_to_ids[spec.key],
                        type=EdgeType.CONTAINS.value,
                    )
                )
        # DEPENDS_ON edges (both from specs and explicit plan edges)
        for spec in plan.nodes:
            for dep in spec.depends_on:
                new_edges.append(
                    EdgeModel(
                        id=uuid.uuid4(),
                        src=keys_to_ids[dep],
                        dst=keys_to_ids[spec.key],
                        type=EdgeType.DEPENDS_ON.value,
                    )
                )
        # A planner may express the same dependency in both NodeSpec and the
        # explicit edge list. Persist the relationship once.
        edge_keys = {(edge.src, edge.dst, edge.type) for edge in new_edges}
        for e in plan.edges:
            key = (keys_to_ids[e.src], keys_to_ids[e.dst], e.type.value)
            if key not in edge_keys:
                new_edges.append(EdgeModel(id=uuid.uuid4(), src=key[0], dst=key[1], type=key[2]))
                edge_keys.add(key)

        async with self.session() as s:
            s.add_all(new_models)
            s.add_all(new_edges)
            await s.commit()

        # the planning node becomes a container
        parent.status = NodeStatus.EXPANDED
        parent = await self._save_node(parent)

        async with self.session() as s:
            created = (
                await s.execute(
                    select(NodeModel).where(NodeModel.id.in_(keys_to_ids.values()))
                )
            ).scalars().all()
            return [_node_from_model(m) for m in created]

    # -- runs ------------------------------------------------------------

    async def create_run(self, node: Node, worker: str, attempt: int = 1) -> Run:
        run = RunModel(
            id=uuid.uuid4(),
            node_id=node.id,
            worker=worker,
            status=RunStatus.RUNNING.value,
            node_revision=node.revision,
            attempt=attempt,
        )
        async with self.session() as s:
            s.add(run)
            await s.commit()
            await s.refresh(run)
            return _run_from_model(run)

    async def update_run(
        self,
        run_id: uuid.UUID,
        *,
        status: Optional[RunStatus] = None,
        outcome=None,
        summary: Optional[str] = None,
        logs: Optional[str] = None,
        error: Optional[str] = None,
        retry_recommended: Optional[bool] = None,
        usage: Optional[Usage] = None,
        session_id: Optional[str] = None,
    ) -> Run:
        async with self.session() as s:
            m = await s.get(RunModel, run_id)
            if m is None:
                raise KeyError(run_id)
            if status is not None:
                m.status = status.value
            if outcome is not None:
                m.outcome = outcome.value
            if summary is not None:
                m.summary = summary
            if logs is not None:
                m.logs = logs
            if error is not None:
                m.error = error
            if retry_recommended is not None:
                m.retry_recommended = retry_recommended
            if usage is not None:
                m.usage = usage.model_dump(mode="json")
            if session_id is not None:
                m.session_id = session_id
            m.ended_at = datetime.now(timezone.utc)
            await s.commit()
            await s.refresh(m)
            return _run_from_model(m)

    async def get_runs(self, node_id: uuid.UUID) -> list[Run]:
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(RunModel).where(RunModel.node_id == node_id)
                )
            ).scalars().all()
            return [_run_from_model(m) for m in rows]

    async def get_project_runs(self, project_id: uuid.UUID) -> list[Run]:
        ids = select(NodeModel.id).where(NodeModel.project_id == project_id)
        async with self.session() as s:
            rows = (
                await s.execute(select(RunModel).where(RunModel.node_id.in_(ids)))
            ).scalars().all()
            return [_run_from_model(m) for m in rows]

    async def cancel_orphaned_runs(
        self, project_id: uuid.UUID, active_node_ids: set[uuid.UUID]
    ) -> int:
        """Terminalize persisted RUNNING rows with no task in this process.

        Process termination cannot update an in-flight row. On the next
        scheduler pass, the in-memory task maps are authoritative; any other
        RUNNING record is durable history, not live work.
        """
        node_ids = select(NodeModel.id).where(NodeModel.project_id == project_id)
        conditions = [RunModel.node_id.in_(node_ids), RunModel.status == RunStatus.RUNNING.value]
        if active_node_ids:
            conditions.append(RunModel.node_id.not_in(active_node_ids))
        async with self.session() as s:
            result = await s.execute(
                update(RunModel)
                .where(*conditions)
                .values(
                    status=RunStatus.CANCELLED.value,
                    outcome=Outcome.FAIL.value,
                    ended_at=datetime.now(timezone.utc),
                    error="Run interrupted before this runner process started",
                    retry_recommended=True,
                )
            )
            await s.commit()
            return int(result.rowcount or 0)

    # -- artifacts -------------------------------------------------------

    async def add_artifacts(
        self, node_id: uuid.UUID, specs: list[ArtifactSpec]
    ) -> list[Artifact]:
        arts: list[ArtifactModel] = []
        async with self.session() as s:
            for spec in specs:
                a = ArtifactModel(
                    id=uuid.uuid4(),
                    node_id=node_id,
                    kind=spec.kind.value,
                    name=spec.name,
                    content=spec.content,
                    ref=spec.ref,
                )
                arts.append(a)
                s.add(a)
            await s.commit()
            created = [_artifact_from_model(a) for a in arts]
        # link artifacts onto the node
        n = await self.get_node(node_id)
        if n is not None:
            n.artifact_refs = list(n.artifact_refs) + [a.id for a in created]
            await self._save_node(n)
        return created

    async def get_artifacts(self, node_id: uuid.UUID) -> list[Artifact]:
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(ArtifactModel).where(ArtifactModel.node_id == node_id)
                )
            ).scalars().all()
            return [_artifact_from_model(m) for m in rows]

    async def get_artifact(self, artifact_id: uuid.UUID) -> Optional[Artifact]:
        async with self.session() as s:
            m = (
                await s.execute(
                    select(ArtifactModel).where(ArtifactModel.id == artifact_id)
                )
            ).scalar_one_or_none()
            return _artifact_from_model(m) if m else None
