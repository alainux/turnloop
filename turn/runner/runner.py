"""The runner: finds runnable nodes, executes them, stores outcomes, dispatches.

Turn owns the workgraph and node state; the runner only reads the graph,
invokes workers through an execution adapter, and writes results back. One node
Run is one execution. Prefect (if used) lives behind the execution adapter and
never leaks into the data model.
"""
from __future__ import annotations

import asyncio
import logging
import time
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
    ReviewMode,
    Resource,
    Run,
    RunStatus,
    VerificationStatus,
    WorkerResult,
)
from turn.graph.logic import build_indexes, evaluate
from turn.runner.events import EventBus
from turn.runner.recovery import backoff_seconds, should_retry
from turn.workers.base import NodeExecutionContext, Worker
from turn.workers import parsing
from turn.workers.harnesses import recover_session_id
from turn.workers.terminal import GenerationStalled, LocalPtyTransport
from turn.workers.registry import WorkerRegistry, build_registry
from turn.workers import worktree

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
        self._verifying: dict[uuid.UUID, asyncio.Task] = {}
        self._retries: dict[uuid.UUID, int] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._merge_lock = asyncio.Lock()
        self._last_launch_at: dict[uuid.UUID, float] = {}
        self.terminal = LocalPtyTransport()

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        for t in [*self._running.values(), *self._verifying.values()]:
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

    async def _project_repo(self, project_id: uuid.UUID) -> str | None:
        """Resolve the project's own git repo path from its root node.

        Every project gets its own repository (recorded on the root node), so
        there is no shared/global repository to fall back to.
        """
        root = await self.store.get_node(project_id)
        if root is None:
            return None
        return root.repo_path

    async def _schedule_project(self, project_id: uuid.UUID) -> None:
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return

        # RUNNING rows survive an abrupt process exit, but tasks do not. Keep
        # run-level usage/history honest by closing every row not owned by a
        # live worker or verifier in this runner process.
        active_node_ids = {
            node_id
            for mapping in (self._running, self._verifying)
            for node_id, task in mapping.items()
            if not task.done()
        }
        await self.store.cancel_orphaned_runs(project_id, active_node_ids)

        by_id = {node.id: node for node in nodes}

        # Cancellation is inherited. A verifier or worker can finish during
        # the cancellation transaction and create a replacement child after
        # supersede_branch() took its descendant snapshot. Reconcile that race
        # at the scheduler boundary: no live work may survive underneath a
        # cancelled/superseded ancestor.
        for node in nodes:
            ancestor = by_id.get(node.parent_id)
            inactive_ancestor = None
            seen: set[uuid.UUID] = set()
            while ancestor is not None and ancestor.id not in seen:
                seen.add(ancestor.id)
                if ancestor.status == NodeStatus.CANCELLED or ancestor.superseded_by:
                    inactive_ancestor = ancestor
                    break
                ancestor = by_id.get(ancestor.parent_id)
            if inactive_ancestor is None or node.status == NodeStatus.CANCELLED:
                continue
            for task in (self._running.get(node.id), self._verifying.get(node.id)):
                if task is not None and not task.done():
                    task.cancel()
            node.status = NodeStatus.CANCELLED
            node.superseded_by = inactive_ancestor.id
            node.needs_review = False
            await self.store._save_node(node)
            await self._emit("node.updated", project_id, _dump(node))

        # Historical runs may finish at the same moment that their branch is
        # cancelled or superseded. Such nodes remain useful history, but they
        # must never keep owning an actionable review slot. Reconcile this at
        # the scheduler boundary as well as in supersede_branch() so imported
        # and pre-migration projects cannot leave auto-verification stuck.
        for node in nodes:
            if node.needs_review and (
                node.superseded_by is not None or node.status == NodeStatus.CANCELLED
            ):
                verifier = self._verifying.get(node.id)
                if verifier is not None and not verifier.done():
                    verifier.cancel()
                node.needs_review = False
                await self.store._save_node(node)
                await self._emit("node.updated", project_id, _dump(node))

        ev = evaluate(nodes, edges)
        idx = build_indexes(nodes, edges)
        node_by_id = idx.node_by_id

        # persist effective leaf statuses (RUNNABLE / BLOCKED) first
        for n in nodes:
            eff = ev.status.get(n.id)
            if eff in (NodeStatus.RUNNABLE, NodeStatus.BLOCKED):
                # Evaluation is a snapshot; a worker can finish while this
                # tick is awaiting earlier writes. Re-read immediately before
                # mutation so stale READY/BLOCKED projections never regress a
                # newly terminal or running node.
                fresh = await self.store.get_node(n.id)
                if fresh is None or fresh.status in (
                    NodeStatus.RUNNING,
                    NodeStatus.COMPLETE,
                    NodeStatus.FAILED,
                    NodeStatus.CANCELLED,
                    NodeStatus.EXPANDED,
                ):
                    continue
                if fresh.status != eff:
                    changed = await self.store.set_status_if_current(
                        fresh.id,
                        eff,
                        (NodeStatus.PENDING, NodeStatus.RUNNABLE, NodeStatus.BLOCKED),
                    )
                    if changed is not None:
                        await self._emit("node.updated", project_id, _dump(changed))

        # Propagate completed planner containers up the tree. This MUST run
        # deepest-first: a parent's worktree has to already contain every
        # child's files before it is merged into its own parent, otherwise a
        # shallow-first merge snapshots the branch before the child merges land
        # and silently drops those files (the "missing chapters" bug).
        parent_of = {n.id: n.parent_id for n in nodes}

        def _depth(nid):
            d = 0
            seen = set()
            cur = parent_of.get(nid)
            while cur is not None and cur not in seen:
                seen.add(cur)
                d += 1
                cur = parent_of.get(cur)
            return d

        merge_nodes = [
            n
            for n in nodes
            if ev.status.get(n.id) == NodeStatus.COMPLETE
            and n.status == NodeStatus.EXPANDED
            and n.parent_id is not None
        ]
        merge_nodes.sort(key=lambda n: _depth(n.id), reverse=True)
        for n in merge_nodes:
            if n.status != NodeStatus.COMPLETE:
                await self.store.set_status(n.id, NodeStatus.COMPLETE)
                await self._emit("node.updated", project_id, _dump(node_by_id[n.id]))
            # A planner container that just finished must propagate its
            # accumulated children's files up into its parent's worktree.
            await self._merge_up(n)
            # Its subtree is now redundant on disk; flag it for review so the
            # user can accept (clean) or reject (feedback) it.
            await self._mark_merged(n)

        # --- finalize ---------------------------------------------------
        # When the whole project has settled (the root is a container and every
        # descendant is terminal), ship the accumulated working branch into the
        # project's base branch so the user is left with a real, initialized
        # git repo of their finished work. Idempotent -- a fully-shipped
        # project is a no-op on later ticks.
        root = node_by_id.get(project_id)
        if root is not None and root.status in (NodeStatus.EXPANDED, NodeStatus.COMPLETE):
            settled = all(
                ev.status.get(n.id)
                in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED)
                and not (n.needs_review and not n.merge_accepted)
                for n in nodes
                if n.id != project_id
            )
            if settled and len(nodes) > 1:
                await self._maybe_finalize(root)

        # --- parent-verification drain ----------------------------------
        # Auto-verify is never a blind cleanup switch. Each pending branch is
        # assigned to its parent agent, which inspects evidence and may accept
        # or reject it. Rejections continue the child's existing session and
        # worktree. This remains independent of auto-run so a user may combine
        # manual execution with automatic parent review.
        if self._auto_accept(root):
            pending = [
                n for n in nodes
                if n.parent_id
                and not n.superseded_by
                and not n.merge_accepted
                and n.needs_review
            ]
            pending.sort(key=lambda n: _depth(n.id), reverse=True)
            for n in pending:
                self._queue_parent_verification(n.id)

        # Reconcile old/concurrent filesystem residue in every review mode.
        # Accepted state is a lifecycle guarantee, not an auto-review feature.
        for n in sorted(
            (item for item in nodes if item.parent_id and item.merge_accepted),
            key=lambda item: _depth(item.id),
            reverse=True,
        ):
            await self._cleanup_accepted(n)

        # --- manual mode -------------------------------------------------
        # When the project root is not auto-run, we still compute and persist
        # effective statuses (so the UI can show what is ready) but we do NOT
        # launch anything. The user drives execution via step()/run_node().
        root = node_by_id.get(project_id)
        if root is not None and not root.auto_run:
            return

        policy = root.run_policy if root and root.run_policy else None
        force_sequential = policy.force_sequential if policy else self.s.force_sequential
        delay_ms = policy.delay_between_jobs_ms if policy else self.s.delay_between_jobs_ms
        project_running = [nid for nid in self._running if node_by_id.get(nid)]
        if force_sequential and project_running:
            return
        if delay_ms and time.monotonic() - self._last_launch_at.get(project_id, 0) < delay_ms / 1000:
            return
        for nid in sorted(ev.runnable, key=str):
            if nid in self._running:
                continue
            snapshot = node_by_id.get(nid)
            if snapshot is None:
                continue
            # Runnable membership is also snapshot-derived. Re-read before
            # reserving the task so a stale tick cannot re-launch a node that
            # completed while this scheduler pass was awaiting I/O.
            node = await self.store.get_node(nid)
            if node is None or node.merge_accepted or node.status in (
                NodeStatus.RUNNING,
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.EXPANDED,
            ):
                continue
            # Respect an explicit pause: a paused node must not be auto-launched.
            if node.paused:
                continue
            self._running[nid] = asyncio.create_task(self._execute_node(node, project_id))
            self._last_launch_at[project_id] = time.monotonic()
            if force_sequential or delay_ms:
                break

    # -- execution -------------------------------------------------------

    async def _execute_node(self, node: Node, project_id: uuid.UUID) -> None:
        async with self._sem:
            try:
                fresh = await self.store.get_node(node.id)
                if fresh is None or fresh.merge_accepted:
                    return
                node = fresh
                if node.executor == PLANNER_EXECUTOR and node.status != NodeStatus.EXPANDED:
                    await self._plan_node(node, project_id)
                else:
                    await self._run_worker(node, project_id)
            except asyncio.CancelledError:
                await self._mark_cancelled(node)
                raise
            except GenerationStalled as e:
                root = await self.store.get_node(project_id)
                policy = root.run_policy if root else None
                max_retries = policy.max_retries if policy else self.s.max_retries
                retry_stalled = policy.retry_choked_models if policy else self.s.retry_choked_models
                if retry_stalled and self._retries.get(node.id, 0) < max_retries:
                    self._retries[node.id] = self._retries.get(node.id, 0) + 1
                    await self.store.set_status(node.id, NodeStatus.RUNNABLE)
                else:
                    await self._mark_failed(node, str(e))
                await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
            except Exception as e:
                logger.exception("node %s failed", node.id)
                await self._mark_failed(node, f"runner error: {e}")
            finally:
                self._running.pop(node.id, None)
                self.wake()

    async def _plan_node(self, node: Node, project_id: uuid.UUID) -> list[Node]:
        ctx = await self._build_context(node)
        # Give the planner (and its future children) a worktree branched from the
        # parent, so children inherit accumulated files and can merge back up.
        self._ensure_worktree(node, ctx.repo_path)
        # Collect the planner's raw Codex transcript so it can be shown in the
        # node-detail terminal pane, exactly like a worker node's output.
        transcript_chunks: list[str] = []
        orig_stream = ctx.stream

        async def _stream(nid, chunk):
            await orig_stream(nid, chunk)
            transcript_chunks.append(chunk)

        ctx.stream = _stream
        run = await self.store.create_run(node, PLANNER_EXECUTOR, self._retries.get(node.id, 0) + 1)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))
        try:
            planner = self.registry.planner
            if planner is None:
                raise RuntimeError("no planner registered")
            plan: PlanResult = await planner.plan(ctx)
            created = await self.store.apply_plan(node, plan)
            transcript = "".join(transcript_chunks)
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
                logs=transcript or f"Planned {len(created)} node(s). {plan.notes or ''}".strip(),
                usage=plan.usage,
                session_id=plan.session_id,
            )
            await self._remember_session(node, plan.session_id)
            await self._emit("plan.applied", project_id, {"parent": _dump(node), "created": len(created)})
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            return created
        except Exception as error:
            transcript = "".join(transcript_chunks)
            await self.store.update_run(
                run.id,
                status=RunStatus.FAILED,
                outcome=Outcome.FAIL,
                summary=str(error),
                logs=transcript,
                error=str(error),
                retry_recommended=isinstance(error, GenerationStalled),
            )
            if transcript:
                await self.store.add_artifacts(
                    node.id,
                    [ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)],
                )
            raise
        finally:
            self.wake()

    async def _run_worker(self, node: Node, project_id: uuid.UUID) -> None:
        ctx = await self._build_context(node)
        worker_key = node.agent.harness.value if node.agent and node.executor != PLANNER_EXECUTOR else node.executor
        worker = self.registry.get(worker_key) or self.registry.get(self.s.default_executor)
        if worker is None:
            await self._mark_failed(node, f"no worker registered for executor '{node.executor}'")
            return
        run = await self.store.create_run(node, worker.name, self._retries.get(node.id, 0) + 1)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))
        try:
            root = await self.store.get_node(project_id)
            timeout = (
                root.run_policy.timeout_seconds
                if root and root.run_policy else self.s.default_run_timeout_seconds
            )
            ctx.timeout_seconds = timeout
            ctx.stall_timeout_seconds = (
                root.run_policy.stall_timeout_seconds
                if root and root.run_policy else self.s.stall_timeout_seconds
            )
            result: WorkerResult = await self.exec_adapter.run(
                worker, ctx, timeout=timeout
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
        fresh = await self.store.get_node(node.id)
        if fresh is not None and fresh.merge_accepted:
            # Acceptance is terminal and may race an already-running worker.
            # Preserve the late run as cancelled history without letting its
            # result revive the accepted node or schedule another retry.
            await self.store.update_run(
                run.id,
                status=RunStatus.CANCELLED,
                outcome=Outcome.FAIL,
                summary="result arrived after branch acceptance",
                error="superseded by accepted state",
            )
            if fresh.status == NodeStatus.RUNNING:
                await self.store.set_status(fresh.id, NodeStatus.COMPLETE)
            await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
            return
        if result.outcome == Outcome.COMPLETE:
            arts = await self.store.add_artifacts(node.id, result.artifacts)
            for a in arts:
                await self._emit("artifact.created", project_id, _dump(a))
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.COMPLETE,
                summary=result.summary, logs=result.executor_notes or result.summary or "",
                usage=result.usage, session_id=result.session_id,
            )
            await self._remember_session(node, result.session_id)
            await self.store.set_status(node.id, NodeStatus.COMPLETE)
            await self._mark_merged(node)
        elif result.outcome == Outcome.EXPAND:
            plan = result.children or PlanResult(nodes=[])
            arts = await self.store.add_artifacts(node.id, result.artifacts)
            for a in arts:
                await self._emit("artifact.created", project_id, _dump(a))
            created = await self.store.apply_plan(node, plan)
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.EXPAND,
                summary=result.summary, logs=result.executor_notes or result.summary or "",
                usage=result.usage, session_id=result.session_id,
            )
            await self._remember_session(node, result.session_id)
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            await self._emit("plan.applied", project_id, {"parent": _dump(node), "created": len(created)})
            if not created:
                await self._mark_merged(node)

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
                summary=result.summary, logs=result.executor_notes or result.summary or "",
                usage=result.usage, session_id=result.session_id,
            )
            await self.store.set_status(node.id, NodeStatus.BLOCKED)

        elif result.outcome == Outcome.FAIL:
            await self.store.update_run(
                run.id, status=RunStatus.FAILED, outcome=Outcome.FAIL,
                summary=result.summary, logs=result.executor_notes or result.summary or result.error or "",
                error=result.error,
                retry_recommended=result.retry_recommended,
            )
            root = await self.store.get_node(project_id)
            policy = root.run_policy if root else None
            max_retries = policy.max_retries if policy else self.s.max_retries
            retry_choked = policy.retry_choked_models if policy else self.s.retry_choked_models
            recommended = should_retry(result.error or result.summary, result.retry_recommended, retry_choked)
            if recommended and self._retries.get(node.id, 0) < max_retries:
                self._retries[node.id] = self._retries.get(node.id, 0) + 1
                base_ms = policy.retry_backoff_ms if policy else self.s.retry_backoff_ms
                delay = backoff_seconds(self._retries[node.id], base_ms)
                if delay:
                    await asyncio.sleep(delay)
                await self.store.set_status(node.id, NodeStatus.RUNNABLE)
            else:
                await self.store.set_status(node.id, NodeStatus.FAILED)

        await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
        self.wake()

    # -- worktree housekeeping ---------------------------------------------

    def _ensure_worktree(self, node: Node, repo_path: str | None = None) -> None:
        """Best-effort: create the node's worktree (branched from its parent).

        No-op when no repo is configured. Failures are logged, never fatal. The
        root node's worktree IS the project repo root, so this also ensures the
        project's working branch is checked out there.
        """
        try:
            if repo_path:
                worktree.get_or_create_worktree(
                    node.id, node.parent_id, force=True, repo_path=repo_path
                )
        except Exception as e:  # pragma: no cover
            logger.warning("worktree ensure failed for %s: %s", node.id, e)

    async def _merge_up(self, node: Node) -> None:
        """Merge a completed container's worktree up into its parent's."""
        repo = await self._project_repo(node.project_id)
        if not repo or node.merge_accepted:
            return
        async with self._merge_lock:
            try:
                await asyncio.to_thread(
                    worktree.merge_into_parent, node.id, node.parent_id, repo
                )
            except Exception as e:  # pragma: no cover
                logger.warning("worktree merge-up failed for %s: %s", node.id, e)

    # -- merge review ----------------------------------------------------

    async def _mark_merged(self, node: Node) -> None:
        """After a node's worktree has been merged up into its parent, flag it
        for review. The root (no parent) is the final accumulation point and is
        never reviewed. Idempotent: once reviewed/accepted it is left alone.

        With project auto-verify ON, the parent agent receives the review. It
        may accept or reject; Turn does not delete evidence before that real
        decision exists.
        """
        if node.parent_id is None:
            return
        repo = await self._project_repo(node.project_id)
        if not repo:
            return
        fresh = await self.store.get_node(node.id)
        if fresh is None or fresh.needs_review or fresh.merge_accepted:
            return
        # Only nodes that actually produced a worktree can be cleaned.
        if not worktree.worktree_path(node.id, repo).exists():
            return
        fresh.needs_review = True
        fresh.verification_status = VerificationStatus.PENDING
        fresh.verification_summary = "Awaiting parent verification"
        await self.store._save_node(fresh)
        await self._emit("node.updated", fresh.project_id, _dump(fresh))
        root = await self.store.get_node(node.project_id)
        if self._auto_accept(root):
            self._queue_parent_verification(node.id)

    def _auto_accept(self, root: Node | None) -> bool:
        """Compatibility name for the parent-owned auto-verification policy.

        The global value is only a compatibility default for projects created
        before per-project policies existed.
        """
        if root and root.run_policy:
            return root.run_policy.review_mode in (ReviewMode.AUTO_ACCEPT, ReviewMode.PARENT)
        return bool(self.s.auto_accept_merges)

    def _queue_parent_verification(self, node_id: uuid.UUID) -> None:
        existing = self._verifying.get(node_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._verify_with_parent(node_id))
        self._verifying[node_id] = task

        def finished(done: asyncio.Task) -> None:
            # A completion callback may run after a replacement task has
            # already been registered for the same node. Never let the older
            # callback remove the newer single-flight reservation.
            if self._verifying.get(node_id) is done:
                self._verifying.pop(node_id, None)
            if not done.cancelled():
                try:
                    done.exception()
                except Exception:
                    pass
            self.wake()

        task.add_done_callback(finished)

    async def _verify_with_parent(self, node_id: uuid.UUID) -> None:
        """Run a real, evidence-bearing review as the node's parent agent.

        COMPLETE accepts. BLOCK is actionable rejection feedback and resumes
        the same child session/worktree. FAIL means the verifier itself could
        not reach a decision; evidence remains available for manual review.
        """
        async with self._sem:
            child = await self.store.get_node(node_id)
            if (
                child is None
                or child.parent_id is None
                or not child.needs_review
                or child.merge_accepted
            ):
                return
            parent = await self.store.get_node(child.parent_id)
            root = await self.store.get_node(child.project_id)
            if parent is None:
                return
            policy = root.run_policy if root and root.run_policy else None
            max_rounds = max(2, (policy.max_retries if policy else self.s.max_retries) + 1)
            if child.verification_round >= max_rounds:
                child.verification_status = VerificationStatus.ERROR
                child.verification_summary = (
                    f"Automatic verification stopped after {max_rounds} rounds; manual review required"
                )
                await self.store._save_node(child)
                await self._emit("node.updated", child.project_id, _dump(child))
                return

            parent_agent = (parent.agent or (root.agent if root else None))
            if parent_agent is None:
                child.verification_status = VerificationStatus.ERROR
                child.verification_summary = "Parent has no configured agent; manual review required"
                await self.store._save_node(child)
                return
            worker = self.registry.get(parent_agent.harness.value)
            if worker is None:
                child.verification_status = VerificationStatus.ERROR
                child.verification_summary = (
                    f"Parent harness {parent_agent.harness.value} is unavailable; manual review required"
                )
                await self.store._save_node(child)
                return

            child.verification_round += 1
            child.verification_status = VerificationStatus.RUNNING
            child.verification_summary = (
                f"{parent.objective} is verifying revision {child.revision}"
            )
            child = await self.store._save_node(child)
            await self._emit("node.updated", child.project_id, _dump(child))

            runs = await self.store.get_runs(child.id)
            execution_runs = [r for r in runs if not r.worker.startswith("parent-verifier:")]
            latest = execution_runs[-1] if execution_runs else None
            artifacts = await self.store.get_artifacts(child.id)
            evidence = "\n".join(
                f"- {artifact.name}: {artifact.ref or artifact.kind.value}"
                for artifact in artifacts[-30:]
                if artifact.name != "transcript"
            ) or "- No declared artifacts; inspect the merged worktree and git history directly."
            verifier_node = child.model_copy(deep=True)
            verifier_node.agent = parent_agent.model_copy(deep=True)
            verifier_node.agent.session_id = child.verification_session_id
            verifier_node.agent.type_id = "validator"
            verifier_node.executor = verifier_node.agent.harness.value
            verifier_node.objective = f"Verify {child.objective}"
            verifier_node.generated_prompt = f"""PARENT NODE:
{parent.objective}

CHILD OBJECTIVE:
{child.objective}

CHILD INSTRUCTIONS:
{child.generated_prompt or "No additional instructions."}

LATEST CHILD RESULT:
{latest.summary if latest and latest.summary else "No summary recorded."}

DECLARED EVIDENCE:
{evidence}

Inspect the merged implementation and relevant graph context. Run focused,
non-destructive checks where possible. Accept only when the child satisfies its
scope and does not conflict with sibling work. If rejecting, give concise,
actionable corrections that the same child session can apply next.
"""
            context = await self._build_context(verifier_node)
            context.purpose = "verify"
            review_run = await self.store.create_run(
                child, f"parent-verifier:{worker.name}", child.verification_round
            )
            try:
                timeout = policy.timeout_seconds if policy else self.s.default_run_timeout_seconds
                result = await self.exec_adapter.run(worker, context, timeout=timeout)
            except asyncio.CancelledError:
                await self.store.update_run(review_run.id, status=RunStatus.CANCELLED, outcome=Outcome.FAIL)
                raise
            except Exception as error:
                result = WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="Parent verification could not run",
                    error=str(error),
                )

            decision = (
                "accepted" if result.outcome == Outcome.COMPLETE
                else "rejected" if result.outcome == Outcome.BLOCK
                else "error"
            )
            decision_artifact = ArtifactSpec(
                kind=ArtifactKind.JSON,
                name=f"parent-verification-{child.verification_round}",
                content={
                    "decision": decision,
                    "parent_id": str(parent.id),
                    "parent_objective": parent.objective,
                    "child_revision": child.revision,
                    "summary": result.summary,
                },
            )
            saved_artifacts = await self.store.add_artifacts(
                child.id, [*result.artifacts, decision_artifact]
            )
            for artifact in saved_artifacts:
                await self._emit("artifact.created", child.project_id, _dump(artifact))
            await self.store.update_run(
                review_run.id,
                status=(RunStatus.FAILED if result.outcome == Outcome.FAIL else RunStatus.COMPLETE),
                outcome=result.outcome,
                summary=result.summary,
                logs=result.executor_notes or result.summary or result.error or "",
                error=result.error,
                usage=result.usage,
                session_id=result.session_id,
            )
            fresh = await self.store.get_node(child.id)
            if fresh is None or not fresh.needs_review or fresh.merge_accepted:
                return
            fresh.verification_summary = result.summary or result.error or "No verification summary"
            if result.session_id:
                fresh.verification_session_id = result.session_id
            if result.outcome == Outcome.COMPLETE:
                fresh.verification_status = VerificationStatus.ACCEPTED
                await self.store._save_node(fresh)
                await self.accept_merge(child.id)
            elif result.outcome == Outcome.BLOCK:
                fresh.verification_status = VerificationStatus.REJECTED
                await self.store._save_node(fresh)
                await self.reject_merge(
                    child.id,
                    f"[Parent verification round {fresh.verification_round}]\n{fresh.verification_summary}",
                )
            else:
                fresh.verification_status = VerificationStatus.ERROR
                await self.store._save_node(fresh)
                await self._emit("node.updated", fresh.project_id, _dump(fresh))

    async def _maybe_finalize(self, root: Node) -> None:
        """Ship a settled project: merge its working branch into the project's
        base branch so the user keeps a real, initialized git repo, then mark
        the root container COMPLETE."""
        repo = await self._project_repo(root.project_id)
        if repo:
            try:
                await asyncio.to_thread(worktree.ship_project, root.id, repo)
            except Exception as e:  # pragma: no cover
                logger.warning("project ship failed for %s: %s", root.id, e)
        if root.status != NodeStatus.COMPLETE:
            await self.store.set_status(root.id, NodeStatus.COMPLETE)
            await self._emit(
                "node.updated", root.project_id, _dump(await self.store.get_node(root.id))
            )

    async def accept_merge(self, node_id: uuid.UUID) -> None:
        """Accept a merged node: keep the merged result (already in the parent)
        and delete this node's now-redundant subtree worktree to reclaim space.

        Cleaning the whole subtree (node + all descendants) means accepting a
        high-level container also resolves every review beneath it.
        """
        verification = self._verifying.get(node_id)
        if verification is not None and verification is not asyncio.current_task():
            verification.cancel()
        node = await self.store.get_node(node_id)
        if node is None or node.parent_id is None:
            return
        repo = await self._project_repo(node.project_id)
        desc = await self.store.descendants(node_id)
        ids = [node_id] + [d.id for d in desc]
        current = asyncio.current_task()
        active_tasks: list[asyncio.Task] = []
        cancelled_descendants: set[uuid.UUID] = set()
        for nid in ids:
            for task in (self._running.get(nid), self._verifying.get(nid)):
                if task is not None and task is not current and not task.done():
                    task.cancel()
                    active_tasks.append(task)
                    if nid != node_id:
                        cancelled_descendants.add(nid)
        if active_tasks:
            await asyncio.gather(*dict.fromkeys(active_tasks), return_exceptions=True)
        async with self._merge_lock:
            cleaned = await self._remove_merged_resources(ids, repo)
            if not cleaned:
                logger.warning("acceptance cleanup incomplete for %s; retaining review state", node_id)
                return
            for nid in ids:
                n = await self.store.get_node(nid)
                if n is None:
                    continue
                n.needs_review = False
                if nid in cancelled_descendants and not n.merge_accepted:
                    n.status = NodeStatus.CANCELLED
                if nid != node_id and n.status == NodeStatus.CANCELLED and not n.merge_accepted:
                    # A cancelled historical attempt is not evidence accepted
                    # by the container decision. Keep it cancelled/unaccepted
                    # instead of visually reviving it as completed work.
                    await self.store._save_node(n)
                    await self._emit("node.updated", n.project_id, _dump(n))
                    continue
                n.merge_accepted = True
                # Acceptance is a terminal positive projection. A concurrent
                # container cleanup may cancel a task, but cannot relabel work
                # already accepted by a verifier as cancelled.
                n.status = NodeStatus.COMPLETE
                if nid == node_id:
                    # Acceptance is terminal. A verifier that reached its
                    # retry ceiling while cleanup awaited the filesystem may
                    # have projected ERROR in the interim; canonicalize the
                    # accepted decision as part of the same locked commit.
                    n.verification_status = VerificationStatus.ACCEPTED
                await self.store._save_node(n)
                await self._emit("node.updated", n.project_id, _dump(n))
        self.wake()

    async def _remove_merged_resources(self, ids: list[uuid.UUID], repo: str | None) -> bool:
        if not repo:
            return True
        for nid in ids:
            try:
                await asyncio.to_thread(worktree.remove_worktree, nid, repo)
            except Exception as error:  # pragma: no cover
                logger.warning("worktree removal failed for %s: %s", nid, error)
        try:
            await asyncio.to_thread(worktree.remove_branches, ids, repo)
        except Exception as error:  # pragma: no cover
            logger.warning("branch removal failed: %s", error)
        return all(
            not worktree.worktree_path(nid, repo).exists()
            and not worktree._branch_exists(worktree.branch_name(nid), repo)
            for nid in ids
        )

    async def _cleanup_accepted(self, node: Node) -> None:
        """Repair filesystem residue for a node already marked accepted."""
        repo = await self._project_repo(node.project_id)
        if not repo:
            return
        async with self._merge_lock:
            # The scheduler snapshot may predate a user rejection. Never let
            # reconciliation delete a worktree that has just become active.
            fresh = await self.store.get_node(node.id)
            if (
                fresh is None
                or not fresh.merge_accepted
                or node.id in self._running
            ):
                return
            if fresh.status != NodeStatus.COMPLETE:
                # Acceptance is the durable positive terminal state. Process
                # interruption or a concurrent container cancellation may
                # leave RUNNING/CANCELLED behind, but with no in-memory owner
                # that projection safely collapses back to COMPLETE.
                await self.store.set_status(fresh.id, NodeStatus.COMPLETE)
                fresh = await self.store.get_node(fresh.id)
                if fresh is None:
                    return
            stale_acceptance_projection = (
                fresh.verification_status != VerificationStatus.ACCEPTED
                or (fresh.verification_summary or "").startswith(
                    "Automatic verification stopped"
                )
            )
            if stale_acceptance_projection:
                fresh.verification_status = VerificationStatus.ACCEPTED
                runs = await self.store.get_runs(fresh.id)
                accepted_review = next(
                    (
                        run for run in reversed(runs)
                        if run.worker.startswith("parent-verifier:")
                        and run.status == RunStatus.COMPLETE
                        and run.outcome == Outcome.COMPLETE
                    ),
                    None,
                )
                if accepted_review and accepted_review.summary:
                    fresh.verification_summary = accepted_review.summary
                await self.store._save_node(fresh)
                await self._emit("node.updated", fresh.project_id, _dump(fresh))
            ids = [node.id]
            if not any(
                worktree.worktree_path(nid, repo).exists()
                or worktree._branch_exists(worktree.branch_name(nid), repo)
                for nid in ids
            ):
                return
            await self._remove_merged_resources(ids, repo)

    async def reject_merge(self, node_id: uuid.UUID, feedback: str) -> None:
        """Reject a merged node: send feedback into the SAME node (no new node)
        and re-run it in place so it can correct its output.

        For a planner/container this supersedes its descendants and re-plans the
        same node; for a leaf it re-runs the same node with the feedback appended
        to its prompt. The node's (already merged) subtree worktree is preserved
        so the re-run starts from the current state.
        """
        verification = self._verifying.get(node_id)
        if verification is not None and verification is not asyncio.current_task():
            verification.cancel()
        node = await self.store.get_node(node_id)
        if node is None or node.parent_id is None:
            return
        if node.agent is not None and not node.agent.session_id:
            # Compatibility recovery for transcripts written before a harness
            # exposed its session id through the adapter. This keeps review
            # feedback in the existing conversation instead of restarting it.
            for artifact in reversed(await self.store.get_artifacts(node_id)):
                if artifact.name != "transcript" or not isinstance(artifact.content, str):
                    continue
                session_id = recover_session_id(artifact.content)
                if session_id:
                    node.agent.session_id = session_id
                    await self.store._save_node(node)
                    break
        if feedback and feedback.strip():
            base = node.generated_prompt or ""
            await self.store.edit_node(
                node_id, generated_prompt=base + f"\n\n[Reviewer feedback]\n{feedback.strip()}"
            )
        # Serialize the accepted -> active transition with accepted-resource
        # cleanup. If cleanup is already in flight, rejection waits and then
        # safely recreates its worktree; if rejection wins, cleanup's refetch
        # sees an active node and exits.
        async with self._merge_lock:
            n = await self.store.get_node(node_id)
            if n is None:
                return
            n.needs_review = False
            n.merge_accepted = False
            is_planner = n.executor == PLANNER_EXECUTOR or n.status == NodeStatus.EXPANDED
            if is_planner:
                n.status = NodeStatus.PENDING
            elif n.status != NodeStatus.RUNNING:
                n.status = NodeStatus.RUNNABLE
            await self.store._save_node(n)
        if is_planner:
            await self.regenerate_descendants(node_id)
        elif n.status != NodeStatus.RUNNING:
            await self.run_node(node_id)
        await self._emit(
            "node.updated", n.project_id, _dump(await self.store.get_node(node_id))
        )
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
        cascade_agent = bool(kwargs.pop("cascade_agent", False))
        node = await self.store.edit_node(node_id, **kwargs)
        if node is not None:
            await self._emit("node.updated", node.project_id, _dump(node))
            if cascade_agent and node.agent is not None:
                for child in await self.store.descendants(node_id):
                    if child.status == NodeStatus.CANCELLED or child.superseded_by:
                        continue
                    inherited = node.agent.model_copy(deep=True)
                    inherited.type_id = "planner" if child.executor == PLANNER_EXECUTOR else "general"
                    changed = await self.store.edit_node(child.id, agent=inherited)
                    if changed is not None:
                        await self._emit("node.updated", changed.project_id, _dump(changed))
        self.wake()

    async def regenerate_descendants(self, node_id: uuid.UUID) -> dict:
        node = await self.store.get_node(node_id)
        if node is None:
            return {"created": [], "superseded": []}
        descendants = await self.store.descendants(node_id)
        cancelling: list[asyncio.Task] = []
        for descendant in descendants:
            for task in (self._running.get(descendant.id), self._verifying.get(descendant.id)):
                if task is not None and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                    cancelling.append(task)
        if cancelling:
            await asyncio.gather(*cancelling, return_exceptions=True)
        cancelled = await self.store.supersede_branch(node_id)
        # Re-plan through the same execution path as an initial planner run so
        # transcript, usage, and especially provider session continuity are
        # preserved across parent-verifier feedback.
        node = await self.store.get_node(node_id)
        if node is None:
            return
        try:
            created = await self._plan_node(node, node.project_id)
        except Exception:
            await self.store.set_status(node.id, NodeStatus.FAILED)
            await self._emit("node.updated", node.project_id, _dump(await self.store.get_node(node.id)))
            raise
        await self._emit(
            "graph.replaced",
            node.project_id,
            {"node": _dump(node), "superseded": [str(c) for c in cancelled],
             "created": len(created)},
        )
        for c in created:
            await self._emit("node.created", node.project_id, _dump(c))
        self.wake()
        return {"created": [str(c.id) for c in created], "superseded": [str(c) for c in cancelled]}

    async def fork(
        self, node_id: uuid.UUID, *, objective: str | None = None,
        generated_prompt: str | None = None,
    ) -> Optional[Node]:
        orig = await self.store.get_node(node_id)
        if orig is None:
            return None
        # A fork remains visible inside the project that owns the source node.
        # Non-root nodes get a sibling alternative. The project root has no
        # sibling container, so its alternative becomes a top-level child.
        # Creating a fresh unrelated project_id here produces an unreachable
        # graph that neither project explorer nor project stream can address.
        parent_id = orig.parent_id or orig.id
        fork = await self.store.create_node(
            project_id=orig.project_id,
            parent_id=parent_id,
            objective=objective or orig.objective,
            generated_prompt=generated_prompt if generated_prompt is not None else orig.generated_prompt,
            executor=PLANNER_EXECUTOR,
            agent=orig.agent.model_copy(deep=True) if orig.agent else None,
            required_inputs=orig.required_inputs,
            resource_refs=orig.resource_refs,
            forked_from=orig.id,
            status=NodeStatus.PENDING,
        )
        # A sibling fork gets an independent planner conversation. Editing and
        # regenerating that fork will then resume its own session.
        fork.agent = fork.agent or AgentConfig(type_id="planner")
        fork.agent.session_id = None
        fork.agent.type_id = "planner"
        fork = await self.store._save_node(fork)
        created = await self._plan_node(fork, fork.project_id)
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

    async def branch_action(self, node_id: uuid.UUID, action: str) -> None:
        """Apply a pause/resume/cancel action to a node and its descendants."""
        node = await self.store.get_node(node_id)
        if node is None:
            return
        descendants = await self.store.descendants(node_id)
        targets = [node, *descendants]
        if action == "pause":
            for target in targets:
                await self.store.set_paused(target.id, True)
        elif action == "resume":
            for target in targets:
                await self.store.set_paused(target.id, False)
        elif action == "cancel":
            for target in targets:
                task = self._running.get(target.id)
                if task:
                    task.cancel()
                elif target.status not in (NodeStatus.COMPLETE, NodeStatus.CANCELLED):
                    await self.store.set_status(target.id, NodeStatus.CANCELLED)
        else:
            raise ValueError(f"unsupported branch action: {action}")
        await self._emit("graph.branch_updated", node.project_id, {"root": str(node_id), "action": action})
        self.wake()

    async def cancel_project_runs(self, project_id: uuid.UUID) -> None:
        """Stop every in-flight task before a project is removed."""
        nodes, _, _ = await self.store.get_workgraph(project_id)
        tasks = [self._running[node.id] for node in nodes if node.id in self._running]
        tasks.extend(
            self._verifying[node.id] for node in nodes if node.id in self._verifying
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            NodeStatus.RUNNING,
        ):
            return None
        # Revive a cancelled or paused node so the user can run it again.
        if node.status == NodeStatus.CANCELLED:
            await self.store.set_status(node_id, NodeStatus.RUNNABLE)
        if node.paused:
            await self.store.set_paused(node_id, False)
        self._running[node.id] = asyncio.create_task(
            self._execute_node(node, node.project_id)
        )
        return node.id

    async def set_mode(self, project_id: uuid.UUID, auto_run: bool) -> None:
        node = await self.store.set_auto_run(project_id, auto_run)
        if node is not None:
            if node.run_policy:
                node.run_policy.auto_run = auto_run
                node = await self.store._save_node(node)
            await self._emit("node.updated", project_id, _dump(node))
        self.wake()

    # -- helpers ---------------------------------------------------------

    async def _build_context(self, node: Node) -> NodeExecutionContext:
        ancestry = await self.store.ancestry(node.id)
        resource_refs = []
        for a in ancestry + [node]:
            resource_refs.extend(a.resource_refs)
        resources = await self._resolve_resources(resource_refs)

        # The project's own git repo (root node's repo_path, else fallback).
        project_repo = await self._project_repo(node.project_id)
        root = await self.store.get_node(node.project_id)
        policy = root.run_policy if root else None

        # Wire a live terminal stream: the worker emits raw output chunks and we
        # fan them out over the project SSE bus as `node.terminal` events.
        pid = node.project_id

        async def _stream(nid, chunk):
            await self._emit("node.terminal", pid, {"node_id": str(nid), "chunk": chunk})

        return NodeExecutionContext(
            node=node,
            ancestry=ancestry,
            resources=resources,
            repo_path=project_repo,
            stream=_stream,
            terminal=self.terminal,
            timeout_seconds=policy.timeout_seconds if policy else self.s.default_run_timeout_seconds,
            stall_timeout_seconds=policy.stall_timeout_seconds if policy else self.s.stall_timeout_seconds,
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

    async def _remember_session(self, node: Node, session_id: str | None) -> None:
        if not session_id:
            return
        fresh = await self.store.get_node(node.id)
        if fresh is None:
            return
        if fresh.agent is None:
            from turn.domain.schemas import AgentConfig, HarnessKind

            harness = fresh.executor if fresh.executor in {h.value for h in HarnessKind} else "codex"
            fresh.agent = AgentConfig(harness=harness)
        fresh.agent.session_id = session_id
        await self.store._save_node(fresh)

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
