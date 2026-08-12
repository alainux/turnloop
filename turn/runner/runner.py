"""The runner: finds runnable nodes, executes them, stores outcomes, dispatches.

Turn owns the workgraph and node state; the runner only reads the graph,
invokes workers through an execution adapter, and writes results back. One node
Run is one execution. Prefect (if used) lives behind the execution adapter and
never leaks into the data model.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger("turn.runner")

from turn.db.store import PLANNER_EXECUTOR, Store
from turn.domain.schemas import (
    Artifact,
    ArtifactKind,
    ArtifactSpec,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    Resource,
    Run,
    RunStatus,
    WorkerResult,
)
from turn.graph.logic import build_indexes, evaluate
from turn.runner.events import EventBus
from turn.workers.base import NodeExecutionContext, Worker
from turn.workers import parsing
from turn.workers.registry import WorkerRegistry, build_registry

from turn.config import settings as default_settings


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _depth(node_id, idx) -> int:
    """Depth of a node in the CONTAINS hierarchy (root = 0)."""
    d = 0
    cur = idx.parents.get(node_id)
    while cur is not None and cur in idx.node_by_id:
        d += 1
        cur = idx.parents.get(cur)
    return d


class Runner:
    def __init__(
        self,
        store: Store,
        registry: Optional[WorkerRegistry] = None,
        events: Optional[EventBus] = None,
        settings=default_settings,
        execution_adapter=None,
    ):
        self.store = store
        self.registry = registry or build_registry(settings)
        self.events = events or EventBus()
        self.s = settings
        self.exec_adapter = execution_adapter or DirectExecutionAdapter(settings)
        self._running: dict[uuid.UUID, asyncio.Task] = {}
        self._retries: dict[uuid.UUID, int] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        for t in list(self._running.values()):
            t.cancel()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop:
            try:
                await self.tick()
            except Exception as e:  # pragma: no cover - keep runner alive
                print(f"[runner] tick error: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.s.runner_tick_seconds)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    # -- scheduling ------------------------------------------------------

    async def tick(self) -> None:
        projects = await self.store.list_projects()
        for p in projects:
            try:
                await self._schedule_project(p.id)
            except Exception as e:  # pragma: no cover
                print(f"[runner] schedule error for {p.id}: {e}")

    async def _schedule_project(self, project_id: uuid.UUID) -> None:
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return
        ev = evaluate(nodes, edges)
        idx = build_indexes(nodes, edges)
        node_by_id = idx.node_by_id

        # persist effective leaf statuses (RUNNABLE / BLOCKED) and derived
        # container completion, without touching RUNNING or terminal states
        for n in nodes:
            eff = ev.status.get(n.id)
            persist = False
            if eff in (NodeStatus.RUNNABLE, NodeStatus.BLOCKED) and n.status not in (
                NodeStatus.RUNNING,
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.EXPANDED,
            ):
                persist = True
            elif eff == NodeStatus.COMPLETE and n.status == NodeStatus.EXPANDED:
                persist = True  # all leaf descendants finished
            if persist and n.status != eff:
                await self.store.set_status(n.id, eff)
                await self._emit(
                    "node.updated", project_id, _dump(node_by_id[n.id])
                )

        # --- manual mode -------------------------------------------------
        # When the project root is not auto-run, we still compute and persist
        # effective statuses (so the UI can show what is ready) but we do NOT
        # launch anything. The user drives execution via step()/run_node().
        root = node_by_id.get(project_id)
        if root is not None and not root.auto_run:
            await self._emit(
                "project.manual",
                project_id,
                {"runnable": [str(x) for x in ev.runnable]},
            )
            return

        for nid in ev.runnable:
            if nid in self._running:
                continue
            node = node_by_id.get(nid)
            if node is None:
                continue
            self._running[nid] = asyncio.create_task(self._execute_node(node, project_id))

    # -- execution -------------------------------------------------------

    async def _execute_node(self, node: Node, project_id: uuid.UUID) -> None:
        async with self._sem:
            try:
                if node.executor == PLANNER_EXECUTOR and node.status != NodeStatus.EXPANDED:
                    await self._plan_node(node, project_id)
                else:
                    await self._run_worker(node, project_id)
            except asyncio.CancelledError:
                await self._mark_cancelled(node)
                raise
            except Exception as e:
                logger.exception("node %s failed", node.id)
                await self._mark_failed(node, f"runner error: {e}")
            finally:
                self._running.pop(node.id, None)
                self.wake()

    async def _plan_node(self, node: Node, project_id: uuid.UUID) -> None:
        ctx = await self._build_context(node)
        # Collect the planner's raw Codex transcript so it can be shown in the
        # node-detail terminal pane, exactly like a worker node's output.
        transcript_chunks: list[str] = []
        orig_stream = ctx.stream

        async def _stream(nid, chunk):
            await orig_stream(nid, chunk)
            transcript_chunks.append(chunk)

        ctx.stream = _stream
        run = await self.store.create_run(node, PLANNER_EXECUTOR)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))
        try:
            planner = self.registry.planner
            if planner is None:
                raise RuntimeError("no planner registered")
            plan: PlanResult = await planner.plan(ctx)
            created = await self.store.apply_plan(node, plan)
            transcript = parsing.strip_ansi("".join(transcript_chunks))
            if transcript.strip():
                arts = await self.store.add_artifacts(
                    node.id,
                    [ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)],
                )
                for a in arts:
                    await self._emit("artifact.created", project_id, _dump(a))
            await self.store.update_run(
                run.id,
                status=RunStatus.COMPLETE,
                outcome=Outcome.COMPLETE,
                summary=f"planned {len(created)} node(s)",
            )
            await self._emit("plan.applied", project_id, {"parent": _dump(node), "created": len(created)})
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
        finally:
            self.wake()

    async def _run_worker(self, node: Node, project_id: uuid.UUID) -> None:
        ctx = await self._build_context(node)
        worker = self.registry.get(node.executor) or self.registry.get(self.s.default_executor)
        if worker is None:
            await self._mark_failed(node, f"no worker registered for executor '{node.executor}'")
            return
        run = await self.store.create_run(node, worker.name)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))
        try:
            result: WorkerResult = await self.exec_adapter.run(
                worker, ctx, timeout=self.s.default_run_timeout_seconds
            )
        except asyncio.TimeoutError:
            await self._handle_outcome(
                node, run, project_id,
                WorkerResult(outcome=Outcome.FAIL, summary="timed out", error="timeout",
                             retry_recommended=False),
            )
            return
        except asyncio.CancelledError:
            await self._mark_cancelled(node)
            await self.store.update_run(run.id, status=RunStatus.CANCELLED, outcome=Outcome.FAIL)
            raise
        except Exception as e:
            logger.exception("worker failed for node %s", node.id)
            await self.store.update_run(
                run.id, status=RunStatus.FAILED, outcome=Outcome.FAIL, error=str(e)
            )
            await self._mark_failed(node, f"worker error: {e}")
            return
        await self._handle_outcome(node, run, project_id, result)

    async def _handle_outcome(
        self, node: Node, run: Run, project_id: uuid.UUID, result: WorkerResult
    ) -> None:
        if result.outcome == Outcome.COMPLETE:
            arts = await self.store.add_artifacts(node.id, result.artifacts)
            for a in arts:
                await self._emit("artifact.created", project_id, _dump(a))
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.COMPLETE,
                summary=result.summary, logs=result.executor_notes or "",
            )
            await self.store.set_status(node.id, NodeStatus.COMPLETE)

        elif result.outcome == Outcome.EXPAND:
            plan = result.children or PlanResult(nodes=[])
            created = await self.store.apply_plan(node, plan)
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.EXPAND,
                summary=result.summary,
            )
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            await self._emit("plan.applied", project_id, {"parent": _dump(node), "created": len(created)})

        elif result.outcome == Outcome.BLOCK:
            node = await self.store.get_node(node.id)
            if node is not None:
                existing = {i.id for i in node.required_inputs}
                for mi in result.missing_inputs:
                    if mi.id not in existing:
                        node.required_inputs.append(mi)
                        existing.add(mi.id)
                await self._save_node_state(node)
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.BLOCK,
                summary=result.summary,
            )
            await self.store.set_status(node.id, NodeStatus.BLOCKED)

        elif result.outcome == Outcome.FAIL:
            await self.store.update_run(
                run.id, status=RunStatus.FAILED, outcome=Outcome.FAIL,
                summary=result.summary, error=result.error,
                retry_recommended=result.retry_recommended,
            )
            if result.retry_recommended and self._retries.get(node.id, 0) < self.s.max_retries:
                self._retries[node.id] = self._retries.get(node.id, 0) + 1
                await self.store.set_status(node.id, NodeStatus.RUNNABLE)
            else:
                await self.store.set_status(node.id, NodeStatus.FAILED)

        await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
        self.wake()

    # -- user actions ----------------------------------------------------

    async def provide_input(self, node_id: uuid.UUID, input_id: str, value: str) -> None:
        node = await self.store.satisfy_input(node_id, input_id, value)
        if node is not None:
            # re-evaluate: if all inputs satisfied, it becomes runnable
            still_missing = [i for i in node.required_inputs if i.satisfied_by is None]
            if not still_missing:
                await self.store.set_status(node_id, NodeStatus.RUNNABLE)
            await self._emit("node.updated", node.project_id, _dump(node))
        self.wake()

    async def edit_node(self, node_id: uuid.UUID, **kwargs) -> None:
        node = await self.store.edit_node(node_id, **kwargs)
        if node is not None:
            await self._emit("node.updated", node.project_id, _dump(node))
        self.wake()

    async def regenerate_descendants(self, node_id: uuid.UUID) -> None:
        node = await self.store.get_node(node_id)
        if node is None:
            return
        cancelled = await self.store.supersede_branch(node_id)
        # re-plan from the (possibly edited) node and build a fresh branch
        ctx = await self._build_context(node)
        planner = self.registry.planner
        plan = await planner.plan(ctx) if planner else PlanResult(nodes=[])
        created = await self.store.apply_plan(node, plan)
        # ensure the node is once again a live container
        if created:
            await self.store.set_status(node_id, NodeStatus.EXPANDED)
        else:
            await self.store.set_status(node_id, NodeStatus.COMPLETE)
        await self._emit(
            "graph.replaced",
            node.project_id,
            {"node": _dump(node), "superseded": [str(c) for c in cancelled],
             "created": len(created)},
        )
        for c in created:
            await self._emit("node.created", node.project_id, _dump(c))
        self.wake()

    async def fork(self, node_id: uuid.UUID) -> Optional[Node]:
        orig = await self.store.get_node(node_id)
        if orig is None:
            return None
        parent_id = orig.parent_id  # sibling alternative; None => new root project
        fork = await self.store.create_node(
            project_id=orig.project_id if parent_id else uuid.uuid4(),
            parent_id=parent_id,
            objective=orig.objective,
            generated_prompt=orig.generated_prompt,
            executor=PLANNER_EXECUTOR,
            required_inputs=orig.required_inputs,
            resource_refs=orig.resource_refs,
            forked_from=orig.id,
            status=NodeStatus.PENDING,
        )
        # planner builds an independent alternative branch from the fork
        ctx = await self._build_context(fork)
        planner = self.registry.planner
        plan = await planner.plan(ctx) if planner else PlanResult(nodes=[])
        created = await self.store.apply_plan(fork, plan)
        if created:
            await self.store.set_status(fork.id, NodeStatus.EXPANDED)
        else:
            await self.store.set_status(fork.id, NodeStatus.COMPLETE)
        await self._emit(
            "graph.forked",
            fork.project_id,
            {"fork": _dump(fork), "from": str(orig.id), "created": len(created)},
        )
        for c in created:
            await self._emit("node.created", fork.project_id, _dump(c))
        self.wake()
        return fork

    async def retry(self, node_id: uuid.UUID) -> None:
        node = await self.store.get_node(node_id)
        if node is None:
            return
        if node.status == NodeStatus.FAILED:
            self._retries[node.id] = 0
            await self.store.set_status(node_id, NodeStatus.RUNNABLE)
            await self._emit("node.updated", node.project_id, _dump(node))
            self.wake()

    async def pause(self, node_id: uuid.UUID) -> None:
        node = await self.store.set_paused(node_id, True)
        if node:
            await self._emit("node.updated", node.project_id, _dump(node))
        self.wake()

    async def resume(self, node_id: uuid.UUID) -> None:
        node = await self.store.set_paused(node_id, False)
        if node:
            await self._emit("node.updated", node.project_id, _dump(node))
        self.wake()

    async def cancel(self, node_id: uuid.UUID) -> None:
        node = await self.store.get_node(node_id)
        if node is None:
            return
        task = self._running.get(node_id)
        if task is not None:
            task.cancel()
        else:
            await self.store.set_status(node_id, NodeStatus.CANCELLED)
            await self._emit("node.updated", node.project_id, _dump(node))
        self.wake()

    # -- manual stepping --------------------------------------------------

    async def step(self, project_id: uuid.UUID) -> Optional[uuid.UUID]:
        """Manual mode: run exactly one runnable node (shallowest first).

        Returns the executed node id, or None if nothing is runnable.
        """
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return None
        ev = evaluate(nodes, edges)
        idx = build_indexes(nodes, edges)
        candidates = [nid for nid in ev.runnable if nid not in self._running]
        if not candidates:
            return None
        # shallowest-first so planners/containers run before their leaves
        candidates.sort(key=lambda nid: (_depth(nid, idx), str(nid)))
        node = idx.node_by_id.get(candidates[0])
        if node is None:
            return None
        self._running[node.id] = asyncio.create_task(
            self._execute_node(node, project_id)
        )
        return node.id

    async def run_node(self, node_id: uuid.UUID) -> Optional[uuid.UUID]:
        """Manually execute a specific node regardless of auto-run mode."""
        node = await self.store.get_node(node_id)
        if node is None:
            return None
        if node.id in self._running:
            return None
        if node.status in (
            NodeStatus.COMPLETE,
            NodeStatus.FAILED,
            NodeStatus.CANCELLED,
            NodeStatus.RUNNING,
        ):
            return None
        self._running[node.id] = asyncio.create_task(
            self._execute_node(node, node.project_id)
        )
        return node.id

    async def set_mode(self, project_id: uuid.UUID, auto_run: bool) -> None:
        node = await self.store.set_auto_run(project_id, auto_run)
        if node is not None:
            await self._emit("node.updated", project_id, _dump(node))
        self.wake()

    # -- helpers ---------------------------------------------------------

    async def _build_context(self, node: Node) -> NodeExecutionContext:
        ancestry = await self.store.ancestry(node.id)
        resource_refs = []
        for a in ancestry + [node]:
            resource_refs.extend(a.resource_refs)
        resources = await self._resolve_resources(resource_refs)

        # Wire a live terminal stream: the worker emits raw output chunks and we
        # fan them out over the project SSE bus as `node.terminal` events.
        pid = node.project_id

        async def _stream(nid, chunk):
            await self._emit("node.terminal", pid, {"node_id": str(nid), "chunk": chunk})

        return NodeExecutionContext(
            node=node,
            ancestry=ancestry,
            resources=resources,
            repo_path=self.s.repo_path,
            stream=_stream,
        )

    async def _resolve_resources(self, refs: list[str]) -> list[Resource]:
        out: list[Resource] = []
        seen: set[str] = set()
        for ref in refs:
            if ref in seen:
                continue
            seen.add(ref)
            content = None
            try:
                from pathlib import Path

                p = Path(ref)
                if p.is_file():
                    content = p.read_text(errors="replace")[:20000]
            except OSError:
                content = None
            out.append(Resource(ref=ref, content=content))
        return out

    async def _save_node_state(self, node: Node) -> None:
        n = await self.store.get_node(node.id)
        if n is None:
            return
        n.required_inputs = node.required_inputs
        await self.store._save_node(n)  # type: ignore[attr-defined]

    async def _mark_cancelled(self, node: Node) -> None:
        n = await self.store.get_node(node.id)
        if n is None:
            return
        await self.store.set_status(node.id, NodeStatus.CANCELLED)
        await self._emit("node.updated", n.project_id, _dump(n))

    async def _mark_failed(self, node: Node, error: str) -> None:
        await self.store.set_status(node.id, NodeStatus.FAILED)
        n = await self.store.get_node(node.id)
        if n is not None:
            await self._emit("node.updated", n.project_id, _dump(n))

    async def _emit(self, etype: str, project_id: uuid.UUID, data) -> None:
        await self.events.publish(
            {"type": etype, "project_id": str(project_id), "data": data}
        )


class DirectExecutionAdapter:
    """Runs a worker coroutine in-process with timeout + cancellation support.

    This is the default backend. Prefect is an optional alternative behind the
    same interface (see turn.runner.prefect_adapter)."""

    def __init__(self, settings=default_settings):
        self.s = settings

    async def run(self, worker: Worker, ctx: NodeExecutionContext, timeout: float) -> WorkerResult:
        return await asyncio.wait_for(worker.execute(ctx), timeout=timeout)
