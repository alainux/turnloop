"""Repository: the authoritative workgraph + state store.

Turn owns the workgraph and node state. This layer is a thin mapping over
SQLAlchemy; it knows nothing about workers or orchestration.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from turn.db.base import make_engine, make_session_factory
from turn.db.models import (
    ArtifactModel,
    Base,
    EdgeModel,
    NodeModel,
    RunModel,
)
from turn.domain.schemas import (
    Artifact,
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
    RunStatus,
)

# Planner is the only executor the runner treats as a planning operation.
PLANNER_EXECUTOR = "planner"


# --------------------------------------------------------------------------
# Mapping
# --------------------------------------------------------------------------


def _node_from_model(m: NodeModel) -> Node:
    return Node(
        id=m.id,
        project_id=m.project_id,
        parent_id=m.parent_id,
        objective=m.objective,
        generated_prompt=m.generated_prompt,
        executor=m.executor,
        status=NodeStatus(m.status),
        paused=m.paused,
        required_inputs=[InputSpec(**d) for d in (m.required_inputs or [])],
        resource_refs=list(m.resource_refs or []),
        artifact_refs=[uuid.UUID(x) for x in (m.artifact_refs or [])],
        revision=m.revision,
        superseded_by=m.superseded_by,
        forked_from=m.forked_from,
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

    async def dispose(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.Session() as s:
            yield s

    # -- projects ---------------------------------------------------------

    async def create_project(self, prompt: str, name: Optional[str] = None) -> Node:
        root_id = uuid.uuid4()
        node = NodeModel(
            id=root_id,
            project_id=root_id,  # a project IS its root node
            parent_id=None,
            objective=name or prompt,
            generated_prompt=prompt,
            executor=PLANNER_EXECUTOR,
            status=NodeStatus.PENDING.value,
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
            m.generated_prompt = node.generated_prompt
            m.executor = node.executor
            m.status = node.status.value
            m.paused = node.paused
            m.required_inputs = [d.model_dump(mode="json") for d in node.required_inputs]
            m.resource_refs = list(node.resource_refs)
            m.artifact_refs = [str(x) for x in node.artifact_refs]
            m.revision = node.revision
            m.superseded_by = node.superseded_by
            m.forked_from = node.forked_from
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

    async def set_paused(self, node_id: uuid.UUID, paused: bool) -> Optional[Node]:
        n = await self.get_node(node_id)
        if n is None:
            return None
        n.paused = paused
        return await self._save_node(n)

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
            status=status.value,
            required_inputs=[d.model_dump(mode="json") for d in (required_inputs or [])],
            resource_refs=list(resource_refs or []),
            forked_from=forked_from,
        )
        async with self.session() as s:
            s.add(node)
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
            await self._save_node(d)
            cancelled.append(d.id)
        return cancelled

    # -- plan application ------------------------------------------------

    async def apply_plan(self, parent: Node, plan: PlanResult) -> list[Node]:
        """Create child nodes + edges described by a plan under `parent`."""
        if not plan.nodes:
            # nothing to do -> the (sub)tree is complete as-is
            parent.status = NodeStatus.COMPLETE
            await self._save_node(parent)
            return []

        keys_to_ids: dict[str, uuid.UUID] = {}
        new_models: list[NodeModel] = []
        new_edges: list[EdgeModel] = []

        project_id = parent.project_id
        for spec in plan.nodes:
            nid = uuid.uuid4()
            keys_to_ids[spec.key] = nid
            parent_id = (
                keys_to_ids[spec.parent_key] if spec.parent_key else parent.id
            )
            new_models.append(
                NodeModel(
                    id=nid,
                    project_id=project_id,
                    parent_id=parent_id,
                    objective=spec.objective,
                    generated_prompt=spec.generated_prompt,
                    executor=spec.executor,
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
        for e in plan.edges:
            new_edges.append(
                EdgeModel(
                    id=uuid.uuid4(),
                    src=keys_to_ids[e.src],
                    dst=keys_to_ids[e.dst],
                    type=e.type.value,
                )
            )

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

    async def create_run(self, node: Node, worker: str) -> Run:
        run = RunModel(
            id=uuid.uuid4(),
            node_id=node.id,
            worker=worker,
            status=RunStatus.RUNNING.value,
            node_revision=node.revision,
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
