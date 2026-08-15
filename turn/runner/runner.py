"""The runner: finds runnable nodes, executes them, stores outcomes, dispatches.

Turn owns the workgraph and node state; the runner only reads the graph,
invokes workers through an execution adapter, and writes results back. One node
Run is one execution. Prefect (if used) lives behind the execution adapter and
never leaks into the data model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("turn.runner")

from turn.db.store import PLANNER_EXECUTOR, Store
from turn.domain.schemas import (
    Artifact,
    ArtifactKind,
    ArtifactSpec,
    HarnessKind,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    Resource,
    Run,
    RunStatus,
    WorkerResult,
)
from turn.graph.logic import GraphWalker
from turn.runner.events import EventBus
from turn.runner.recovery import backoff_seconds, should_retry
from turn.workers.base import NodeExecutionContext, Worker
from turn.workers.herdr import HerdrAdapter
from turn.workers import parsing
from turn.workers.harnesses import recover_session_id
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.terminal import GenerationStalled, HerdrPtyTransport, TerminalTransport
from turn.workers.registry import WorkerRegistry, build_registry

from turn.config import settings as default_settings


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


class Runner:
    def __init__(
        self,
        store: Store,
        registry: Optional[WorkerRegistry] = None,
        events: Optional[EventBus] = None,
        settings=default_settings,
        execution_adapter=None,
        herdr_adapter: HerdrAdapter | None = None,
        terminal_transport: TerminalTransport | None = None,
    ):
        self.store = store
        self.registry = registry or build_registry(settings)
        self.events = events or EventBus()
        self.s = settings
        self.harness_commands = HarnessCommandFactory(
            codex_binary=settings.codex_binary,
            codex_args=settings.codex_args,
        )
        self.exec_adapter = execution_adapter or DirectExecutionAdapter(settings)
        self._running: dict[uuid.UUID, asyncio.Task] = {}
        self._reconnect_tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._retries: dict[uuid.UUID, int] = {}
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        # Serialize review metadata transitions; this no longer protects
        # version-control operations because Turn does not perform any.
        self._merge_lock = asyncio.Lock()
        self._last_launch_at: dict[uuid.UUID, float] = {}
        self._last_workspace_reconcile_at = 0.0
        # Herdr owns one durable project workspace and one pane per node. Turn
        # only opens short-lived control streams into those panes, so Herdr's
        # UI remains the place where project terminals are managed.
        self.terminal = terminal_transport or HerdrPtyTransport(
            settings.data_dir, adapter=herdr_adapter
        )
        # Shell access and harness access use the same per-node Herdr pane; the
        # UI's terminal endpoint still decides whether the node is generating,
        # so shell activity does not make a node appear active.
        self.shell = self.terminal
        self._shell_tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._status_watchers: dict[uuid.UUID, asyncio.Task] = {}

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        for t in self._running.values():
            t.cancel()
        for t in self._reconnect_tasks.values():
            t.cancel()
        for t in self._shell_tasks.values():
            t.cancel()
        for t in self._status_watchers.values():
            t.cancel()
        if self._shell_tasks:
            await asyncio.gather(*self._shell_tasks.values(), return_exceptions=True)
        if self._status_watchers:
            await asyncio.gather(*self._status_watchers.values(), return_exceptions=True)
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self._wake.set()

    def generation_active(self, node_id: uuid.UUID) -> bool:
        """Whether Turn is currently running a provider for this node.

        A browser may open an ordinary shell in the same persistent Herdr
        session as an agent.  That shell is intentionally inspectable, but it
        must not make a completed node look as though it is still generating.
        """
        task = self._running.get(node_id)
        if task is not None and not task.done():
            return True
        reconnect = self._reconnect_tasks.get(node_id)
        return reconnect is not None and not reconnect.done()

    async def ensure_node_terminal(self, node_id: uuid.UUID) -> bool:
        """Allocate a node's idle Herdr pane without opening a control client."""
        node = await self.store.get_node(node_id)
        if node is None:
            return False
        cwd = await self._project_repo(node.project_id)
        if not cwd:
            return False
        return await self.terminal.ensure_persistent_shell(
            node_id,
            cwd=cwd,
            environment={"TURN_PROJECT_ID": str(node.project_id)},
        )

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
        now = time.monotonic()
        if now - self._last_workspace_reconcile_at >= 1.0:
            await self._reconcile_project_workspaces(projects)
            self._last_workspace_reconcile_at = now
            projects = await self.store.list_projects()
        for p in projects:
            try:
                await self._schedule_project(p.id)
            except Exception as e:  # pragma: no cover
                print(f"[runner] schedule error for {p.id}: {e}")

    async def _reconcile_project_workspaces(self, projects: list[Node]) -> None:
        """Reflect externally deleted Turn-owned Herdr spaces in project state."""
        await self.terminal.close_orphaned_project_workspaces(
            {str(project.id) for project in projects}
        )
        for project in projects:
            state = await self.terminal.project_workspace_state(str(project.id))
            if state != "missing":
                continue
            await self.cancel_project_runs(project.id)
            # The Herdr space has already disappeared externally. Forget the
            # mapping as well so the next Turn process cannot retain a stale
            # workspace reference.
            await self.terminal.close_project_workspace(str(project.id))
            await self.store.delete_project(project.id)
            await self._emit(
                "project.deleted",
                project.id,
                {"source": "herdr", "reason": "workspace_deleted"},
            )

    async def close_project_workspace(self, project_id: uuid.UUID) -> bool:
        """Close a project's Herdr space without touching unrelated workspaces."""
        return await self.terminal.close_project_workspace(str(project_id))

    async def _project_repo(self, project_id: uuid.UUID) -> str | None:
        """Resolve the filesystem directory assigned to a project."""
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
            for mapping in (self._running,)
            for node_id, task in mapping.items()
            if not task.done()
        }
        await self.store.cancel_orphaned_runs(project_id, active_node_ids)

        by_id = {node.id: node for node in nodes}

        # Cancellation is inherited. No live work may survive underneath a
        # cancelled ancestor.
        for node in nodes:
            ancestor = by_id.get(node.parent_id)
            inactive_ancestor = None
            seen: set[uuid.UUID] = set()
            while ancestor is not None and ancestor.id not in seen:
                seen.add(ancestor.id)
                if ancestor.status == NodeStatus.CANCELLED:
                    inactive_ancestor = ancestor
                    break
                ancestor = by_id.get(ancestor.parent_id)
            if inactive_ancestor is None or node.status == NodeStatus.CANCELLED:
                continue
            for task in (self._running.get(node.id),):
                if task is not None and not task.done():
                    task.cancel()
            node.status = NodeStatus.CANCELLED
            node.needs_review = False
            await self.store._save_node(node)
            await self._emit("node.updated", project_id, _dump(node))

        # Cancelled nodes must never keep owning an actionable review slot.
        for node in nodes:
            if node.needs_review and (
                node.status == NodeStatus.CANCELLED
            ):
                node.needs_review = False
                await self.store._save_node(node)
                await self._emit("node.updated", project_id, _dump(node))

        walker = GraphWalker(nodes, edges)
        ev = walker.evaluate()
        idx = walker.indexes
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

        # --- finalize ---------------------------------------------------
        # When the whole project has settled, mark the root complete. All
        # workers have already written directly to the assigned directory.
        root = node_by_id.get(project_id)
        if root is not None and root.status in (NodeStatus.EXPANDED, NodeStatus.COMPLETE):
            settled = all(
                ev.status.get(n.id)
                in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED)
                and not n.needs_review
                for n in nodes
                if n.id != project_id
            )
            if settled and len(nodes) > 1:
                await self._maybe_finalize(root)

        # --- manual mode -------------------------------------------------
        # When the project root is not auto-run, we still compute and persist
        # effective statuses (so the UI can show what is ready) but we do NOT
        # launch anything. The user drives execution via step()/run_node().
        root = node_by_id.get(project_id)
        if root is not None and not root.auto_run:
            return

        policy = root.run_policy if root and root.run_policy else None
        delay_ms = policy.delay_between_jobs_ms if policy else self.s.delay_between_jobs_ms
        project_running = [nid for nid in self._running if node_by_id.get(nid)]
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
            if node is None or node.status in (
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
            if delay_ms:
                break

    # -- execution -------------------------------------------------------

    async def _execute_node(self, node: Node, project_id: uuid.UUID) -> None:
        async with self._sem:
            watcher: asyncio.Task | None = None
            try:
                fresh = await self.store.get_node(node.id)
                if fresh is None:
                    return
                node = fresh
                await self.store.set_agent_status(node.id, state=None, message=None)
                status_path = await self._agent_status_path(node)
                if status_path is not None:
                    watcher = asyncio.create_task(
                        self._watch_agent_status(node.id, project_id, status_path)
                    )
                    self._status_watchers[node.id] = watcher
                # Every executed agent gets a durable Herdr pane, including
                # deterministic test agents that do not themselves attach a
                # harness. Planning stays lazy; execution owns the pane.
                await self.ensure_node_terminal(node.id)
                # A pre-run shell is the same durable Herdr pane the worker
                # will use. Detach only Turn's temporary control stream; do
                # not close the pane before the provider takes it over.
                await self.detach_shell(node.id)
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
                if watcher is not None:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                    self._status_watchers.pop(node.id, None)
                cleared = await self.store.set_agent_status(
                    node.id, state=None, message=None
                )
                if cleared is not None:
                    await self._emit("node.updated", project_id, _dump(cleared))
                self._running.pop(node.id, None)
                self.wake()

    async def _agent_status_path(self, node: Node) -> Path | None:
        repo = await self._project_repo(node.project_id)
        if not repo:
            return None
        return Path(repo) / ".turn" / "interactive" / f"{node.id}.status.json"

    async def _watch_agent_status(
        self, node_id: uuid.UUID, project_id: uuid.UUID, path: Path
    ) -> None:
        last: tuple[str | None, str | None] | None = None
        while True:
            state: str | None = None
            message: str | None = None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    raw_state = payload.get("state")
                    raw_message = payload.get("message")
                    state = raw_state if isinstance(raw_state, str) else None
                    message = raw_message if isinstance(raw_message, str) else None
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                pass
            current = (state, message)
            if current != last:
                updated = await self.store.set_agent_status(
                    node_id, state=state, message=message
                )
                if updated is not None:
                    await self._emit("node.updated", project_id, _dump(updated))
                last = current
            await asyncio.sleep(0.2)

    async def _persist_terminal_transcript(
        self, node_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        """Save the raw PTY stream before its completed session is released.

        A worker normally returns a transcript artifact, but cancellation,
        timeout, and reconnect teardown can happen after the PTY has emitted
        useful output and before a ``WorkerResult`` is available.  Preserve
        that output at the lifecycle boundary so a disconnected terminal can
        still be inspected or reconnected without retaining the PTY in
        memory.  The content remains raw ANSI text; the browser terminal
        emulator owns rendering it.
        """
        snapshot = self.terminal.snapshot(node_id)
        transcript = snapshot.get("output")
        if not isinstance(transcript, str) or not transcript.strip():
            return
        artifacts = await self.store.get_artifacts(node_id)
        if any(
            artifact.name == "transcript" and artifact.content == transcript
            for artifact in artifacts
        ):
            return
        created = await self.store.add_artifacts(
            node_id,
            [ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)],
        )
        for artifact in created:
            await self._emit("artifact.created", project_id, _dump(artifact))

    async def _finish_provider_terminal(
        self, node_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        """Detach Turn's PTY without destroying the harness conversation.

        The Herdr pane is the durable conversation boundary. A worker can
        finish its handoff while the native harness remains open for review,
        reconnect, or a later follow-up. Only the explicit terminal-close or
        fresh re-run actions are allowed to kill that session.
        """
        await self._persist_terminal_transcript(node_id, project_id)
        self.terminal.release(node_id)
        node = await self.store.get_node(node_id)
        if node is not None:
            # Outcome events are emitted while the worker is still unwinding.
            # This second event is intentionally after PTY release so the
            # browser cannot leave a completed/cancelled node spinning.
            await self._emit("node.updated", project_id, _dump(node))

    async def _plan_node(self, node: Node, project_id: uuid.UUID) -> list[Node]:
        ctx = await self._build_context(node)
        # The planner and all descendants use the same assigned project
        # directory, so files are immediately available downstream.
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
            await self._finish_provider_terminal(node.id, project_id)
            self.wake()

    async def _run_worker(self, node: Node, project_id: uuid.UUID) -> None:
        ctx = await self._build_context(node)
        worker_key = node.agent.harness.value if node.agent and node.executor != PLANNER_EXECUTOR else node.executor
        # A node's agent selection is an execution contract. Never substitute
        # the workspace default when that harness is missing: OpenCode must
        # launch OpenCode, not silently become Codex (or Echo).
        worker = self.registry.get(worker_key)
        if worker is None:
            await self._mark_failed(node, f"no worker registered for executor '{node.executor}'")
            return
        run = await self.store.create_run(node, worker.name, self._retries.get(node.id, 0) + 1)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))

        async def remember_live_session(session_id: str) -> None:
            if not session_id:
                return
            await self._remember_session(node, session_id)
            await self.store.update_run(run.id, session_id=session_id)
            await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))

        ctx.session_callback = remember_live_session
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
            result: WorkerResult = await self.exec_adapter.run(worker, ctx, timeout=timeout)
        except asyncio.TimeoutError:
            await self._handle_outcome(
                node, run, project_id,
                WorkerResult(outcome=Outcome.FAIL, summary="timed out", error="timeout",
                             retry_recommended=False),
            )
            await self._finish_provider_terminal(node.id, project_id)
            return
        except asyncio.CancelledError:
            await self._mark_cancelled(node)
            await self.store.update_run(run.id, status=RunStatus.CANCELLED, outcome=Outcome.FAIL)
            await self._finish_provider_terminal(node.id, project_id)
            raise
        except Exception as e:
            logger.exception("worker failed for node %s", node.id)
            await self.store.update_run(
                run.id, status=RunStatus.FAILED, outcome=Outcome.FAIL, error=str(e)
            )
            await self._mark_failed(node, f"worker error: {e}")
            await self._finish_provider_terminal(node.id, project_id)
            return
        try:
            await self._handle_outcome(node, run, project_id, result)
        finally:
            # The provider process has completed (or been terminated by the
            # worker). Durable transcript artifacts remain available for
            # reconnect; do not retain the finished PTY in server memory.
            await self._finish_provider_terminal(node.id, project_id)

    async def _handle_outcome(
        self, node: Node, run: Run, project_id: uuid.UUID, result: WorkerResult
    ) -> None:
        fresh = await self.store.get_node(node.id)
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

    async def _maybe_finalize(self, root: Node) -> None:
        """Mark a settled project complete after direct filesystem execution."""
        if root.status != NodeStatus.COMPLETE:
            await self.store.set_status(root.id, NodeStatus.COMPLETE)
            await self._emit(
                "node.updated", root.project_id, _dump(await self.store.get_node(root.id))
            )

    async def accept_merge(self, node_id: uuid.UUID) -> None:
        """Accept a reviewed node without changing the shared project files.

        Cleaning the whole subtree (node + all descendants) means accepting a
        high-level container also resolves every review beneath it.
        """
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
            for task in (self._running.get(nid),):
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
                # already accepted by the user as cancelled.
                n.status = NodeStatus.COMPLETE
                await self.store._save_node(n)
                await self._emit("node.updated", n.project_id, _dump(n))
        self.wake()

    async def _remove_merged_resources(self, ids: list[uuid.UUID], repo: str | None) -> bool:
        """Compatibility hook: shared project files are never removed."""
        return True

    async def _cleanup_accepted(self, node: Node) -> None:
        """Repair filesystem residue for a node already marked accepted."""
        # Accepted nodes do not own disposable directories in filesystem mode.
        return None

    async def reject_merge(self, node_id: uuid.UUID, feedback: str) -> None:
        """Reject a merged node: send feedback into the SAME node (no new node)
        and re-run it in place so it can correct its output.

        For a planner/container this supersedes its descendants and re-plans the
        same node; for a leaf it re-runs the same node with the feedback appended
        to its prompt. The shared project directory remains in place so the
        re-run starts from the current files.
        """
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
        # Serialize the accepted -> active transition with review metadata.
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
        kwargs.pop("cascade_agent", None)
        node = await self.store.edit_node(node_id, **kwargs)
        if node is not None:
            await self._emit("node.updated", node.project_id, _dump(node))
            if node.agent is not None:
                for child in await self.store.descendants(node_id):
                    if child.status == NodeStatus.CANCELLED:
                        continue
                    inherited = node.agent.model_copy(deep=True)
                    inherited = inherited.as_type(
                        "planner" if child.executor == PLANNER_EXECUTOR else "executor"
                    )
                    changed = await self.store.edit_node(child.id, agent=inherited)
                    if changed is not None:
                        await self._emit("node.updated", changed.project_id, _dump(changed))
        self.wake()

    async def regenerate_descendants(
        self, node_id: uuid.UUID, *, fresh_session: bool = False
    ) -> dict:
        node = await self.store.get_node(node_id)
        if node is None:
            return {"created": [], "removed": []}
        descendants = await self.store.descendants(node_id)
        cancelling: list[asyncio.Task] = []
        for descendant in descendants:
            for task in (self._running.get(descendant.id),):
                if task is not None and task is not asyncio.current_task() and not task.done():
                    task.cancel()
                    cancelling.append(task)
        if cancelling:
            await asyncio.gather(*cancelling, return_exceptions=True)
        # Removed graph nodes must release their Herdr panes before their
        # persisted records disappear. Otherwise regeneration leaves invisible
        # terminals behind in the project's Herdr space.
        for descendant in descendants:
            await self.terminal.close_persistent_session(descendant.id)
        removed = await self.store.replace_descendants(node_id)
        # Re-plan through the same execution path as an initial planner run so
        # transcript, usage, and provider session continuity are preserved.
        node = await self.store.get_node(node_id)
        if node is None:
            return
        if fresh_session and node.agent is not None:
            # “Re-run” is the one intentionally destructive session action:
            # discard the old Herdr conversation before the new planner uses
            # the same node id. Otherwise the persistent transport quite correctly
            # finds the old session and attaches the fresh prompt to it.
            if self.terminal.snapshot(node_id).get("active"):
                await self.terminal.stop(node_id)
            close_persistent = getattr(self.terminal, "close_persistent_session", None)
            if close_persistent is not None:
                await close_persistent(node_id)
            self.terminal.release(node_id)
            node.agent.session_id = None
            node = await self.store._save_node(node)
        try:
            created = await self._plan_node(node, node.project_id)
        except Exception:
            await self.store.set_status(node.id, NodeStatus.FAILED)
            await self._emit("node.updated", node.project_id, _dump(await self.store.get_node(node.id)))
            raise
        await self._emit(
            "graph.replaced",
            node.project_id,
            {"node": _dump(node), "removed": [str(c) for c in removed],
             "created": len(created)},
        )
        for c in created:
            await self._emit("node.created", node.project_id, _dump(c))
        self.wake()
        return {"created": [str(c.id) for c in created], "removed": [str(c) for c in removed]}

    async def retry(self, node_id: uuid.UUID) -> None:
        node = await self.store.get_node(node_id)
        if node is None:
            return
        if node.status == NodeStatus.FAILED:
            self._retries[node.id] = 0
            await self.store.set_status(node_id, NodeStatus.RUNNABLE)
            await self._emit("node.updated", node.project_id, _dump(node))
            self.wake()

    def _reconnect_command(self, node: Node, cwd: str, session_id: str) -> list[str] | None:
        """Build a native interactive command for a stored provider session."""
        agent = node.agent
        if agent is None:
            return None
        return self.harness_commands.reconnect_command(agent, cwd, session_id)

    async def reconnect(self, node_id: uuid.UUID) -> bool:
        """Reopen the last provider conversation without rerunning the node."""
        node = await self.store.get_node(node_id)
        if node is None:
            return False
        if self.terminal.snapshot(node_id).get("active"):
            return True
        existing = self._reconnect_tasks.get(node_id)
        if existing is not None and not existing.done():
            return True
        cwd = await self._project_repo(node.project_id)
        persistent_exists = await self.terminal.has_persistent_session(node_id)
        command: list[str] | None = None
        if not persistent_exists:
            session_id = node.agent.session_id if node.agent else None
            if not session_id:
                for run in reversed(await self.store.get_runs(node_id)):
                    if run.session_id:
                        session_id = run.session_id
                        break
            command = self._reconnect_command(node, cwd, session_id) if cwd and session_id else None
            if not command:
                return False

        async def stream(nid, chunk):
            await self._emit(
                "node.terminal",
                node.project_id,
                {"node_id": str(nid), "chunk": chunk},
            )

        task = asyncio.create_task(
            self._run_reconnect(node, command or ["true"], cwd, stream)
        )
        self._reconnect_tasks[node_id] = task
        # Let the Herdr control stream create and register the PTY before the API
        # response allows the UI to open its websocket subscription.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if self.terminal.snapshot(node_id).get("active"):
                return True
            if task.done():
                return False
        return bool(self.terminal.snapshot(node_id).get("active"))

    async def open_shell(self, node_id: uuid.UUID) -> bool:
        """Open an ordinary interactive shell in the node's project directory."""
        if self.shell.snapshot(node_id).get("active"):
            return True
        existing = self._shell_tasks.get(node_id)
        if existing is not None and not existing.done():
            return True
        node = await self.store.get_node(node_id)
        if node is None:
            return False
        cwd = await self._project_repo(node.project_id)
        if not cwd:
            return False
        os.makedirs(cwd, exist_ok=True)
        shell = os.environ.get("SHELL") or "/bin/sh"
        if not os.path.exists(shell):
            shell = "/bin/sh"
        if not self.shell.available:
            logger.error("Herdr is required for user terminals")
            return False

        async def run_shell() -> None:
            try:
                await self.shell.run(
                    node_id,
                    [shell, "-i"],
                    cwd=cwd,
                    environment={"TURN_PROJECT_ID": str(node.project_id)},
                    stream=None,
                    timeout=None,
                    stall_timeout=None,
                    idle_warning=None,
                    idle_reap=None,
                )
            except FileNotFoundError:
                logger.warning("cannot open shell for %s", node_id)
            except asyncio.CancelledError:
                raise
            finally:
                self.shell.release(node_id)
                self._shell_tasks.pop(node_id, None)

        task = asyncio.create_task(run_shell())
        self._shell_tasks[node_id] = task
        # Do not let the websocket take its initial snapshot until the PTY is
        # registered. A Herdr control stream can emit the prompt immediately; if the
        # subscriber snapshots during the small create_subprocess window it
        # receives an empty terminal and misses the persistent scrollback.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if self.shell.snapshot(node_id).get("active"):
                return True
            if task.done():
                return False
        return bool(self.shell.snapshot(node_id).get("active"))

    async def detach_shell(self, node_id: uuid.UUID) -> bool:
        """Detach the browser PTY while retaining the Herdr pane."""
        task = self._shell_tasks.get(node_id)
        if task is None or task.done():
            self.shell.release(node_id)
            return False
        # A websocket disconnect only detaches Turn's outer client.  It must
        # not send C-c to the Herdr pane, which may currently host a planner.
        await self.shell.detach(node_id)
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def close_shell(self, node_id: uuid.UUID) -> bool:
        """Close a user shell and remove its persistent Herdr pane."""
        detached = await self.detach_shell(node_id)
        killed = await self.shell.close_persistent_session(node_id)
        return detached or killed

    async def close_provider_terminal(self, node_id: uuid.UUID) -> bool:
        """Close a reconnected completed-session PTY, never a live run."""
        node = await self.store.get_node(node_id)
        if node is None or node.status == NodeStatus.RUNNING:
            return False
        task = self._reconnect_tasks.get(node_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            close_persistent = getattr(self.terminal, "close_persistent_session", None)
            if close_persistent is not None:
                await close_persistent(node_id)
            return True
        if self.terminal.snapshot(node_id).get("active"):
            await self.terminal.stop(node_id)
            close_persistent = getattr(self.terminal, "close_persistent_session", None)
            if close_persistent is not None:
                await close_persistent(node_id)
            return True
        close_persistent = getattr(self.terminal, "close_persistent_session", None)
        return bool(await close_persistent(node_id)) if close_persistent is not None else False

    async def _run_reconnect(self, node: Node, command: list[str], cwd: str, stream) -> None:
        try:
            if getattr(self.terminal, "supports_inject", False):
                # The persistent pane is a plain shell. Attach it; if it did
                # not already exist, type the resume command into the fresh
                # shell. An already-running session is simply reattached
                # (injecting would spawn a second provider).
                existed = await self.terminal.has_persistent_session(node.id)
                task = asyncio.create_task(
                    self.terminal.ensure_session(
                        node.id,
                        cwd=cwd,
                        environment={"TURN_PROJECT_ID": str(node.project_id)},
                        stream=stream,
                        idle_warning=self.s.terminal_idle_warning_seconds,
                        idle_reap=self.s.terminal_idle_reap_seconds,
                    )
                )
                for _ in range(50):
                    snapshot = self.terminal.snapshot(node.id)
                    if task.done():
                        break
                    if snapshot.get("active"):
                        break
                    await asyncio.sleep(0.02)
                if not task.done() and command and command != ["true"] and not existed:
                    await self.terminal.inject_command(
                        node.id,
                        " ".join(shlex.quote(part) for part in command),
                        environment={"TURN_PROJECT_ID": str(node.project_id)},
                    )
                await task
            else:
                await self.terminal.run(
                    node.id,
                    command,
                    cwd=cwd,
                    stream=stream,
                    timeout=None,
                    stall_timeout=None,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                )
        except FileNotFoundError:
            logger.warning("cannot reconnect %s: harness binary is unavailable", node.id)
        except asyncio.CancelledError:
            raise
        finally:
            await self._finish_provider_terminal(node.id, node.project_id)
            self._reconnect_tasks.pop(node.id, None)

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
        reconnect = self._reconnect_tasks.get(node_id)
        if reconnect is not None and not reconnect.done():
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)
            self.wake()
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
            task for node in nodes
            for task in [self._reconnect_tasks.get(node.id)]
            if task is not None and not task.done()
        )
        tasks.extend(
            task for node in nodes
            for task in [self._shell_tasks.get(node.id)]
            if task is not None and not task.done()
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
        walker = GraphWalker(nodes, edges)
        ev = walker.evaluate()
        idx = walker.indexes
        candidates = [nid for nid in ev.runnable if nid not in self._running]
        if not candidates:
            return None
        # shallowest-first so planners/containers run before their leaves
        candidates.sort(key=lambda nid: (walker.depth(nid), str(nid)))
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

        # The project's assigned filesystem directory (root node's repo_path).
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
            session_callback=None,
            # Native harness sessions are conversational PTYs. They have
            # their own detached-idle reaper rather than a whole-run timeout.
            interactive_terminal=bool(
                node.agent
                and node.agent.harness
                in {HarnessKind.CODEX, HarnessKind.PI, HarnessKind.OPENCODE}
            ),
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
        if ctx.interactive_terminal:
            return await worker.execute(ctx)
        return await asyncio.wait_for(worker.execute(ctx), timeout=timeout)
