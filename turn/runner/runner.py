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
    VerificationDecision,
    WorkerResult,
)
from turn.graph.logic import GraphWalker, rejection_target
from turn.runner.events import EventBus
from turn.runner.recovery import backoff_seconds, should_retry
from turn.workers.base import NodeExecutionContext, Worker
from turn.workers.herdr import HerdrAdapter
from turn.workers import parsing
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.mcp.runtime import prepare_runtime
from turn.skills.library import validate_plan_skill_files
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
        )
        self.exec_adapter = execution_adapter or DirectExecutionAdapter(settings)
        self._running: dict[uuid.UUID, asyncio.Task] = {}
        self._reconnect_tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._retries: dict[uuid.UUID, int] = {}
        # Session ids that an explicit fresh attempt must not receive back
        # from its provider. This is process-local because it describes the
        # transition between two attempts, not durable graph configuration.
        self._forbidden_fresh_sessions: dict[uuid.UUID, str] = {}
        self._wake = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._last_launch_at: dict[uuid.UUID, float] = {}
        # A manual step advances the whole runnable frontier. This barrier
        # prevents a fast branch from unlocking downstream work while a
        # sibling from the same stage is still running.
        self._manual_stages: dict[uuid.UUID, set[uuid.UUID]] = {}
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
        self._deleting_projects: set[uuid.UUID] = set()

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self, *, close_workspaces: bool = False) -> None:
        self._stop = True
        self._wake.set()
        self._manual_stages.clear()
        self._deleting_projects.clear()
        running = list(self._running.values())
        for node_id in list(self._running):
            await self.terminal.stop(node_id)
        for t in running:
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
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if close_workspaces:
            # Test runtimes own their entire Herdr session. Stopping node
            # tasks only releases control streams; close the durable test
            # workspaces too, including mappings for projects already deleted
            # from the test store. Production shutdown intentionally preserves
            # project workspaces across a daemon restart.
            await self.terminal.close_orphaned_project_workspaces(set())

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

    def begin_project_deletion(self, project_id: uuid.UUID) -> bool:
        """Reserve a project so the scheduler cannot relaunch it mid-delete."""
        if project_id in self._deleting_projects:
            return False
        self._deleting_projects.add(project_id)
        return True

    def end_project_deletion(self, project_id: uuid.UUID) -> None:
        self._deleting_projects.discard(project_id)

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
            if p.id in self._deleting_projects:
                continue
            try:
                await self._schedule_project(p.id)
            except Exception as e:  # pragma: no cover
                print(f"[runner] schedule error for {p.id}: {e}")

    async def _reconcile_project_workspaces(self, projects: list[Node]) -> None:
        """Reflect externally deleted Turn-owned Herdr spaces in project state."""
        projects = [project for project in projects if project.id not in self._deleting_projects]
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
        if project_id in self._deleting_projects:
            return
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

        # A process restart leaves the provider conversation outside this
        # Runner process, but the persisted provider id is still the source of
        # truth. The next launch replaces any stale foreground provider
        # process and resumes that id; only an explicit Run again is allowed
        # to retire it.

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
        if delay_ms and time.monotonic() - self._last_launch_at.get(project_id, 0) < delay_ms / 1000:
            return
        # Launch every currently runnable node. Dependency edges define the
        # frontier; there is no hidden concurrency cap in the MVP. A stable
        # topological order keeps manual inspection and launch logs readable,
        # while independent nodes remain genuinely parallel.
        runnable_order = [
            candidate.id
            for candidate in walker.topological()
            if candidate.id in ev.runnable
        ]
        for nid in runnable_order:
            if project_id in self._deleting_projects:
                return
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
            # Surface the reservation before pane allocation and provider
            # startup. The API projects this as `preparing` while the task is
            # alive, so the UI does not appear idle during that interval.
            await self._emit("node.updated", project_id, _dump(node))
            self._last_launch_at[project_id] = time.monotonic()
            if delay_ms:
                break

    # -- execution -------------------------------------------------------

    async def _execute_node(self, node: Node, project_id: uuid.UUID) -> None:
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
            forbidden_session_id = self._forbidden_fresh_sessions.pop(node.id, None)
            if node.executor == PLANNER_EXECUTOR and node.status != NodeStatus.EXPANDED:
                await self._plan_node(
                    node,
                    project_id,
                    forbidden_session_id=forbidden_session_id,
                )
            else:
                await self._run_worker(
                    node,
                    project_id,
                    forbidden_session_id=forbidden_session_id,
                )
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

    async def _finish_provider_terminal(
        self, node_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        """Detach Turn's PTY without destroying the harness conversation.

        The Herdr pane is the durable conversation boundary. A worker can
        finish its handoff while the native harness remains open for reconnect
        or a later follow-up. The next provider call replaces only the PTY
        process and resumes the saved provider session by id; Run again is the
        explicit path that clears that id and starts a new conversation.
        """
        self.terminal.release(node_id)
        node = await self.store.get_node(node_id)
        if node is not None:
            # Outcome events are emitted while the worker is still unwinding.
            # This second event is intentionally after PTY release so the
            # browser cannot leave a completed/cancelled node spinning.
            await self._emit("node.updated", project_id, _dump(node))

    async def _plan_node(
        self,
        node: Node,
        project_id: uuid.UUID,
        *,
        forbidden_session_id: str | None = None,
    ) -> list[Node]:
        ctx = await self._build_context(node)
        ctx.forbidden_session_id = forbidden_session_id
        # The planner and all descendants use the same assigned project
        # directory, so files are immediately available downstream.
        prior_runs = await self.store.get_runs(node.id)
        run = await self.store.create_run(node, PLANNER_EXECUTOR, len(prior_runs) + 1)
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))

        async def remember_live_session(session_id: str) -> None:
            if not session_id:
                return
            if forbidden_session_id and session_id == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
            await self._remember_session(node, session_id)
            await self.store.update_run(run.id, session_id=session_id)
            await self._emit(
                "node.updated",
                project_id,
                _dump(await self.store.get_node(node.id)),
            )

        ctx.session_callback = remember_live_session
        try:
            planner = self.registry.planner
            if planner is None:
                raise RuntimeError("no planner registered")
            plan: PlanResult = await planner.plan(ctx)
            if forbidden_session_id and plan.session_id == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
            if ctx.repo_path:
                validate_plan_skill_files(
                    plan.model_dump(mode="json"),
                    ctx.repo_path,
                    planner_skill_ids=node.agent.skill_ids if node.agent else None,
                )
            created = await self.store.apply_plan(node, plan)
            submitted = await self.store.add_artifacts(
                node.id,
                [ArtifactSpec(
                    kind=ArtifactKind.JSON,
                    name="plan-submission",
                    content=plan.model_dump(mode="json"),
                )],
            )
            for artifact in submitted:
                await self._emit("artifact.created", project_id, _dump(artifact))
            session_note = f"session_id={plan.session_id}" if plan.session_id else "session_id=unavailable"
            await self.store.update_run(
                run.id,
                status=RunStatus.COMPLETE,
                outcome=Outcome.COMPLETE,
                summary=f"planned {len(created)} node(s)",
                logs=f"{session_note}; planned {len(created)} node(s)",
                usage=plan.usage,
                session_id=plan.session_id,
            )
            await self._remember_session(node, plan.session_id)
            await self._emit("plan.applied", project_id, {"parent": _dump(node), "created": len(created)})
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            return created
        except asyncio.CancelledError:
            await self.store.update_run(
                run.id,
                status=RunStatus.CANCELLED,
                outcome=Outcome.FAIL,
                summary="run cancelled",
                error="run cancelled by user",
                retry_recommended=False,
            )
            raise
        except Exception as error:
            await self.store.update_run(
                run.id,
                status=RunStatus.FAILED,
                outcome=Outcome.FAIL,
                summary=str(error),
                logs="planner submission failed; inspect the live Herdr session",
                error=str(error),
                retry_recommended=isinstance(error, GenerationStalled),
            )
            raise
        finally:
            await self._finish_provider_terminal(node.id, project_id)
            self.wake()

    async def _run_worker(
        self,
        node: Node,
        project_id: uuid.UUID,
        *,
        forbidden_session_id: str | None = None,
    ) -> None:
        ctx = await self._build_context(node)
        ctx.forbidden_session_id = forbidden_session_id
        worker_key = node.agent.harness.value if node.agent and node.executor != PLANNER_EXECUTOR else node.executor
        # A node's agent selection is an execution contract. Never substitute
        # the workspace default when that harness is missing: OpenCode must
        # launch OpenCode, not silently become Codex (or Echo).
        worker = self.registry.get(worker_key)
        if worker is None:
            await self._mark_failed(node, f"no worker registered for executor '{node.executor}'")
            return
        prior_runs = await self.store.get_runs(node.id)
        run = await self.store.create_run(node, worker.name, len(prior_runs) + 1)
        ctx.attempt = run.attempt
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))

        async def remember_live_session(session_id: str) -> None:
            if not session_id:
                return
            if forbidden_session_id and session_id == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
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
            if forbidden_session_id and result.session_id == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
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
        await self._persist_result_materials(node.id, project_id, result)
        if result.verification is not None:
            await self._handle_verification(node, run, project_id, result)
            return
        fresh = await self.store.get_node(node.id)
        if result.outcome == Outcome.COMPLETE:
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.COMPLETE,
                summary=result.summary, logs=result.executor_notes or result.summary or "",
                usage=result.usage, session_id=result.session_id,
            )
            await self._remember_session(node, result.session_id)
            await self.store.set_status(node.id, NodeStatus.COMPLETE)
        elif result.outcome == Outcome.EXPAND:
            plan = result.children or PlanResult(nodes=[])
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

    async def _persist_result_materials(
        self, node_id: uuid.UUID, project_id: uuid.UUID, result: WorkerResult
    ) -> list[Artifact]:
        """Persist concise output identities without retaining terminal transcripts."""
        linked = await self.store.add_document_refs(node_id, result.document_refs)
        explicit = await self.store.add_artifacts(node_id, result.artifacts)
        for artifact in [*linked, *explicit]:
            await self._emit("artifact.created", project_id, _dump(artifact))
        return [*linked, *explicit]

    async def _handle_verification(
        self, reviewer: Node, run: Run, project_id: uuid.UUID, result: WorkerResult
    ) -> None:
        """Persist a review decision and route rejections to its target."""
        decision = result.verification
        if decision is None:
            raise RuntimeError("verification outcome missing decision")
        current = await self.store.get_node(reviewer.id)
        if current is None:
            return
        current.verification = decision
        await self.store.update_run(
            run.id,
            status=RunStatus.COMPLETE,
            outcome=Outcome.COMPLETE,
            summary=decision.summary,
            logs=result.executor_notes or decision.summary,
            usage=result.usage,
            session_id=result.session_id,
        )
        if result.session_id and current.agent is not None:
            current.agent.session_id = result.session_id
        current.status = NodeStatus.COMPLETE
        await self.store._save_node(current)

        if decision.decision is VerificationDecision.REJECT:
            nodes, edges, _ = await self.store.get_workgraph(project_id)
            walker = GraphWalker(nodes, edges)
            target = rejection_target(current, decision, walker.indexes)
            if target is None:
                raise RuntimeError(
                    "rejection requires a valid target_node_id or exactly one dependency target"
                )
            await self._notify_rejection(target, reviewer, decision)
            # A reviewer may be the active member of a manual Step barrier.
            # Rejection moves that reviewer back behind the corrected target,
            # so the old barrier can never settle. The next Step must be
            # allowed to select the repaired target, and then the reviewer
            # again after that target completes.
            self._manual_stages.pop(project_id, None)
            # A rejection invalidates the target, the review node, and every
            # dependent result reachable from either. The graph replays them
            # in dependency order; the target is runnable immediately and the
            # reviewer becomes runnable again after its prerequisites settle.
            invalidated: list[Node] = []
            pending = [target.id, reviewer.id]
            seen: set[uuid.UUID] = set()
            while pending:
                dependent_id = pending.pop(0)
                if dependent_id in seen:
                    continue
                seen.add(dependent_id)
                dependent = walker.indexes.node_by_id.get(dependent_id)
                if dependent is not None:
                    invalidated.append(dependent)
                    pending.extend(walker.indexes.dependents.get(dependent_id, []))
            for item in invalidated:
                if item.id == target.id or item.id == reviewer.id or item.status != NodeStatus.RUNNING:
                    item.status = NodeStatus.RUNNABLE if item.id == target.id else NodeStatus.PENDING
                    item.agent_state = None
                    item.agent_message = None
                    # Keep the target's provider session. The rejection was
                    # injected into that active conversation, so the next
                    # attempt must continue with the same context rather than
                    # starting a new conversation from zero.
                    await self.store._save_node(item)
                    await self._emit("node.updated", project_id, _dump(item))
        await self._emit("node.updated", project_id, _dump(await self.store.get_node(reviewer.id)))
        self.wake()

    async def _notify_rejection(self, target: Node, reviewer: Node, decision) -> None:
        """Deliver feedback to the selected node's scoped conversation."""
        repo = await self._project_repo(target.project_id)
        if not repo:
            return
        # Echo is a deterministic, non-conversational test worker. A served
        # Echo demo still needs to exercise routing, but it has no provider
        # session that Herdr can resume or receive pasted feedback in.
        if (
            target.agent is not None
            and target.agent.harness is HarnessKind.ECHO
            and getattr(self.terminal, "backend_name", None) == "herdr"
            and self.s.default_executor == "echo"
        ):
            return
        lines = [
            "TURN VERIFICATION REJECTED",
            f"Reviewer: {reviewer.objective}",
            f"Summary: {decision.summary}",
            *[f"- {item}" for item in decision.findings],
            "Required changes:",
            *[f"- {item}" for item in decision.required_changes],
            "Continue the responsible node through Turn after addressing these findings; the project execution mode controls when the refinement runs.",
        ]
        message = "\n".join(lines)
        # The process-level fake has no durable shell pane when tests inject
        # a LocalPtyTransport, but its provider session is still meaningful:
        # launch a fresh fake process with the retained session id so the
        # rejection path exercises the same command boundary as native
        # harnesses.
        if target.agent is not None and target.agent.harness is HarnessKind.FAKE:
            if not await self.reconnect(target.id, prompt=message):
                raise RuntimeError(
                    f"could not launch fake rejection follow-up for node {target.id}"
                )
            return
        if getattr(self.terminal, "supports_inject", False):
            if not await self.reconnect(target.id, prompt=message):
                raise RuntimeError(
                    f"could not launch rejection follow-up for node {target.id}'s "
                    "persisted harness session"
                )
            return

        # Deterministic non-Herdr transports used by tests do not expose a
        # process table. Preserve their byte-level assertion surface; served
        # runs always use the branch above.
        payload = "\x03\r" + message + "\r"
        if not self.terminal.snapshot(target.id).get("active"):
            task = asyncio.create_task(
                self.terminal.ensure_session(
                    target.id,
                    cwd=repo,
                    environment={"TURN_PROJECT_ID": str(target.project_id)},
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                )
            )
            try:
                for _ in range(100):
                    if self.terminal.snapshot(target.id).get("active"):
                        break
                    if task.done():
                        break
                    await asyncio.sleep(0.01)
                await self.terminal.write(target.id, payload)
            finally:
                await self.terminal.detach(target.id)
                await asyncio.gather(task, return_exceptions=True)
        else:
            await self.terminal.write(target.id, payload)

    async def _maybe_finalize(self, root: Node) -> None:
        """Mark a settled project complete after direct filesystem execution."""
        if root.status != NodeStatus.COMPLETE:
            await self.store.set_status(root.id, NodeStatus.COMPLETE)
            await self._emit(
                "node.updated", root.project_id, _dump(await self.store.get_node(root.id))
            )

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
                        child.agent.type_id
                        if child.agent is not None
                        else ("planner" if child.executor == PLANNER_EXECUTOR else "executor")
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
        current_task = asyncio.current_task()
        claimed = False
        if current_task is not None:
            existing = self._running.get(node_id)
            if existing is not None and existing is not current_task and not existing.done():
                await self.terminal.stop(node_id)
                existing.cancel()
                await asyncio.gather(existing, return_exceptions=True)
            self._running[node_id] = current_task
            claimed = True
        try:
            descendants = await self.store.descendants(node_id)
            cancelling: list[asyncio.Task] = []
            for descendant in descendants:
                task = self._running.get(descendant.id)
                if task is not None and task is not current_task and not task.done():
                    await self.terminal.stop(descendant.id)
                    task.cancel()
                    cancelling.append(task)
            if cancelling:
                await asyncio.gather(*cancelling, return_exceptions=True)
            # Removed graph nodes must release their Herdr panes before their
            # persisted records disappear. Otherwise regeneration leaves
            # invisible terminals behind in the project's Herdr space.
            for descendant in descendants:
                await self.terminal.close_persistent_session(descendant.id)
            removed = await self.store.replace_descendants(node_id)
            # Re-plan through the same execution path as an initial planner
            # run. The request task is registered in _running for the whole
            # operation, so the scheduler cannot orphan its run and Stop can
            # cancel the provider while planning is still in progress.
            node = await self.store.get_node(node_id)
            if node is None:
                return {"created": [], "removed": [str(c) for c in removed]}
            forbidden_session_id = None
            if fresh_session and node.agent is not None:
                # Run again closes the active provider call and its stale
                # control pane before launching a new one. A pane can survive
                # a server restart while its control stream is no longer
                # injectable; reusing it makes a valid rerun fail at launch.
                await self.terminal.close_persistent_session(node_id)
                forbidden_session_id = await self._reset_provider_session(node_id)
                if forbidden_session_id:
                    self._forbidden_fresh_sessions[node_id] = forbidden_session_id
                node = await self.store.get_node(node_id) or node
            await self.store.clear_generated_artifacts(node_id)
            created = await self._plan_node(
                node,
                node.project_id,
                forbidden_session_id=forbidden_session_id,
            )
            await self._emit(
                "graph.replaced",
                node.project_id,
                {"node": _dump(node), "removed": [str(c) for c in removed],
                 "created": len(created)},
            )
            for c in created:
                await self._emit("node.created", node.project_id, _dump(c))
            return {"created": [str(c.id) for c in created], "removed": [str(c) for c in removed]}
        except asyncio.CancelledError:
            current = await self.store.get_node(node_id)
            if current is not None:
                await self._mark_cancelled(current)
            raise
        except Exception:
            await self.store.set_status(node.id, NodeStatus.FAILED)
            await self._emit("node.updated", node.project_id, _dump(await self.store.get_node(node.id)))
            raise
        finally:
            if claimed and self._running.get(node_id) is current_task:
                self._running.pop(node_id, None)
            self.wake()

    async def retry(self, node_id: uuid.UUID) -> None:
        node = await self.store.get_node(node_id)
        if node is None:
            return
        if node.status in {NodeStatus.FAILED, NodeStatus.COMPLETE}:
            self._retries[node.id] = 0
            refreshed = await self._prepare_fresh_run(node_id)
            if refreshed is not None:
                await self._emit("node.updated", refreshed.project_id, _dump(refreshed))
            self.wake()

    def _reconnect_command(
        self,
        node: Node,
        cwd: str,
        session_id: str,
        *,
        prompt: str | None = None,
    ) -> list[str] | None:
        """Build a native interactive command for a stored provider session."""
        agent = node.agent
        if agent is None:
            return None
        if agent.harness is HarnessKind.FAKE:
            # The process-level fake is test-only and intentionally does not
            # belong in the real provider command catalog. It still needs an
            # explicit reconnect command so rejection flows exercise the same
            # retained-session lifecycle as native harnesses.
            from turn.workers.fake_harness import fake_harness_script

            command = [fake_harness_script(), "--reconnect", session_id]
            return [*command, prompt] if prompt is not None else command
        runtime = prepare_runtime(cwd, node.id, agent)
        return self.harness_commands.reconnect_command(
            agent,
            cwd,
            session_id,
            prompt=prompt,
            mcp_config=runtime.claude_config,
        )

    async def reconnect(
        self, node_id: uuid.UUID, *, prompt: str | None = None
    ) -> bool:
        """Reopen the last provider conversation, optionally with a follow-up."""
        node = await self.store.get_node(node_id)
        if node is None:
            return False
        if prompt is None and self.terminal.snapshot(node_id).get("active"):
            return True
        existing = self._reconnect_tasks.get(node_id)
        if existing is not None and not existing.done():
            return True
        cwd = await self._project_repo(node.project_id)
        if prompt is not None:
            # A follow-up is a new provider process with the same conversation
            # id. Close any existing provider/pane first so the prompt is
            # delivered through the provider's launch command, never into a
            # stale composer.
            await self.terminal.close_persistent_session(node_id)
        persistent_exists = await self.terminal.has_persistent_session(node_id)
        command: list[str] | None = None
        if prompt is not None or not persistent_exists:
            session_id = node.agent.session_id if node.agent else None
            command = (
                self._reconnect_command(node, cwd, session_id, prompt=prompt)
                if cwd and session_id
                else None
            )
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
        runtime = prepare_runtime(cwd, node.id, node.agent)
        environment = {"TURN_PROJECT_ID": str(node.project_id), **runtime.environment}
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
                        environment=environment,
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
                # Herdr's pane is the durable process boundary. The short
                # control task may finish while the pane remains valid, so
                # its completion is not evidence that the command cannot be
                # delivered. Prompted reconnects already closed the old pane;
                # inject into the newly attached pane regardless of that
                # control-task race.
                if command and command != ["true"] and not existed:
                    await self.terminal.inject_command(
                        node.id,
                        " ".join(shlex.quote(part) for part in command),
                        environment=environment,
                    )
                await task
            else:
                await self.terminal.run(
                    node.id,
                    command,
                    cwd=cwd,
                    environment=environment,
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
            # A follow-up runs in a separate reconnect task, but Stop has the
            # same lifecycle meaning as it does for a normal worker: terminate
            # the provider and make the next user action an explicit fresh run.
            await self.terminal.stop(node_id)
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)
            await self.store.set_status(node_id, NodeStatus.CANCELLED)
            await self._emit("node.updated", node.project_id, _dump(await self.store.get_node(node_id)))
            self.wake()
            return
        task = self._running.get(node_id)
        if task is not None:
            # Stop the provider before cancelling Turn's awaiter. This makes
            # Stop effective even when the task is inside a native harness
            # call rather than inside the runner's Python bookkeeping.
            await self.terminal.stop(node_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        else:
            await self.terminal.stop(node_id)
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
            cancelling: list[asyncio.Task] = []
            for target in targets:
                task = self._running.get(target.id)
                if task:
                    await self.terminal.stop(target.id)
                    task.cancel()
                    cancelling.append(task)
                elif target.status not in (NodeStatus.COMPLETE, NodeStatus.CANCELLED):
                    await self.terminal.stop(target.id)
                    await self.store.set_status(target.id, NodeStatus.CANCELLED)
            if cancelling:
                await asyncio.gather(*cancelling, return_exceptions=True)
        else:
            raise ValueError(f"unsupported branch action: {action}")
        await self._emit("graph.branch_updated", node.project_id, {"root": str(node_id), "action": action})
        self.wake()

    async def cancel_project_runs(self, project_id: uuid.UUID) -> None:
        """Stop every in-flight task before a project is removed."""
        self._manual_stages.pop(project_id, None)
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

    async def step(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        """Manual mode: run the next runnable DAG stage as one batch.

        A stage is the current runnable frontier. The next frontier is not
        exposed until every node in this batch settles, so a fast branch cannot
        cause downstream work to start while its siblings are still in flight.
        """
        if project_id in self._deleting_projects:
            return []
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return []

        stage = self._manual_stages.get(project_id)
        if stage:
            current = {node.id: node for node in nodes}
            settled = all(
                node_id not in self._running
                and current.get(node_id) is not None
                and current[node_id].status
                in (
                    NodeStatus.COMPLETE,
                    NodeStatus.FAILED,
                    NodeStatus.CANCELLED,
                    NodeStatus.EXPANDED,
                )
                for node_id in stage
            )
            if not settled:
                return []
            self._manual_stages.pop(project_id, None)

        walker = GraphWalker(nodes, edges)
        ev = walker.evaluate()
        stage_nodes = [
            candidate
            for candidate in walker.topological()
            if candidate.id in ev.runnable and candidate.id not in self._running
        ]
        if not stage_nodes:
            return []

        self._manual_stages[project_id] = {node.id for node in stage_nodes}
        for node in stage_nodes:
            if project_id in self._deleting_projects:
                self._manual_stages.pop(project_id, None)
                return []
            self._running[node.id] = asyncio.create_task(
                self._execute_node(node, project_id)
            )
            await self._emit("node.updated", project_id, _dump(node))
        return [node.id for node in stage_nodes]

    async def run_node(self, node_id: uuid.UUID) -> Optional[uuid.UUID]:
        """Manually execute a specific node regardless of auto-run mode."""
        node = await self.store.get_node(node_id)
        if node is None:
            return None
        if node.project_id in self._deleting_projects:
            return None
        # Outcome handling persists the next runnable projection before the
        # owning task reaches its finally block. If a user clicks Run in that
        # narrow window, wait for cleanup and re-read the node instead of
        # returning a false no-op for a valid retry.
        existing = self._running.get(node.id)
        if existing is not None:
            await asyncio.gather(existing, return_exceptions=True)
            node = await self.store.get_node(node_id)
            if node is None:
                return None
        if node.status in (
            NodeStatus.COMPLETE,
            NodeStatus.FAILED,
            NodeStatus.RUNNING,
        ):
            return None
        # A manual stop is an explicit fresh-run boundary. Close the durable
        # provider pane and retire its session before reviving the node. This
        # is intentionally limited to CANCELLED: rejection follow-ups use the
        # retained-session reconnect path, while retry() handles the other
        # terminal states through the same fresh-run preparation.
        if node.status == NodeStatus.CANCELLED:
            node = await self._prepare_fresh_run(node_id)
            if node is None:
                return None
        if node.paused:
            await self.store.set_paused(node_id, False)
        if node.project_id in self._deleting_projects:
            return None
        self._running[node.id] = asyncio.create_task(
            self._execute_node(node, node.project_id)
        )
        await self._emit("node.updated", node.project_id, _dump(node))
        return node.id

    async def set_mode(self, project_id: uuid.UUID, auto_run: bool) -> None:
        if auto_run:
            self._manual_stages.pop(project_id, None)
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
        updated = await self.store.get_node(node.id)
        await self._emit("node.updated", n.project_id, _dump(updated or n))

    async def _reset_provider_session(self, node_id: uuid.UUID) -> str | None:
        """Clear the provider session and return the identity being retired."""
        fresh = await self.store.get_node(node_id)
        if fresh is None or fresh.agent is None:
            return None
        previous = fresh.agent.session_id
        if previous:
            fresh.agent.session_id = None
            await self.store._save_node(fresh)
        return previous

    async def _prepare_fresh_run(self, node_id: uuid.UUID) -> Node | None:
        """Reset a terminal node for a new prompt-driven harness launch."""
        node = await self.store.get_node(node_id)
        if node is None:
            return None
        await self.terminal.close_persistent_session(node_id)
        previous = await self._reset_provider_session(node_id)
        if previous:
            self._forbidden_fresh_sessions[node_id] = previous
        await self.store.clear_generated_artifacts(node_id)
        await self.store.set_status(node_id, NodeStatus.RUNNABLE)
        return await self.store.get_node(node_id)

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
