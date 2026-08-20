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
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("turn.runner")

from turn.db.store import PLANNER_EXECUTOR, Store
from turn.capabilities.catalog import CapabilityCatalog
from turn.contracts.organization import audit_plan
from turn.domain.organization import (
    EvidenceStatus,
    HandoffStatus,
    ManagerDecision,
    ManagerPhase,
    OrganizationPhase,
    OrganizationReview,
    OrganizationScale,
    PlanAuditDecision,
    PlanAuditResult,
    WorkItemStatus,
)
from turn.domain.schemas import (
    Artifact,
    ArtifactKind,
    ArtifactSpec,
    AgentConfig,
    AgentType,
    EdgeType,
    EventSource,
    HarnessKind,
    ManagerResult,
    Node,
    NodeStatus,
    Outcome,
    PlanResult,
    Resource,
    Run,
    RunStatus,
    SubgraphRef,
    VerificationDecision,
    WorkerResult,
)
from turn.graph.logic import GraphWalker, rejection_target
from turn.runner.events import EventBus
from turn.runner.execution import NodeExecutor
from turn.runner.recovery import backoff_seconds, should_retry
from turn.runner.scheduler import Scheduler
from turn.runner.organization import ManagerReviewDecision, OrganizationManager
from turn.runner.workspaces import WorkspaceError, WorkspaceManager
from turn.runner.sessions import SessionController
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.herdr import HerdrAdapter, HerdrAdapterError, HerdrResourceNotFound
from turn.workers import parsing
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.metrics import HarnessEvent
from turn.workers.interactive import read_result_file
from turn.workers.capabilities import CapabilityLaunch, harness_capability_adapter
from turn.workers.terminal import GenerationStalled, HerdrPtyTransport, TerminalTransport
from turn.workers.registry import WorkerRegistry, build_registry

from turn.config import REAL_HARNESSES, Settings, settings as default_settings
from turn.contracts.dag import (
    parse_plan,
    parse_result,
    parse_verification,
    validate_subgraph_sources,
)
from turn.contracts.organization_codecs import (
    parse_manager_result,
    parse_plan_audit,
    parse_structured_artifact,
)
from turn.contracts.text import sanitize_control_text


class ControlOperationUnavailable(RuntimeError):
    """A bounded provider/control operation could not return a decision."""


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


def _plan_submission_artifact(plan: PlanResult) -> ArtifactSpec:
    """Persist the planner receipt together with its editable source link."""
    source_refs: list[dict[str, object]] = []
    seen_refs: set[str] = set()
    for reference in [
        *plan.subgraph_refs,
        *(reference for node in plan.nodes for reference in node.subgraph_refs),
    ]:
        if reference.ref in seen_refs:
            continue
        seen_refs.add(reference.ref)
        source_refs.append(reference.model_dump(mode="json"))
    sequence_edges = {
        (predecessor, node.key)
        for node in plan.nodes
        for predecessor in node.follows
    }
    composition_edges = {
        (node.parent_key, node.key)
        for node in plan.nodes
        if node.parent_key
    }
    for edge in plan.edges:
        target = sequence_edges if edge.type is EdgeType.FOLLOWS else composition_edges
        target.add((edge.src, edge.dst))
    receipt = {
        "subgraph_refs": source_refs,
        "project_name": plan.project_name,
        "node_count": len(plan.nodes),
        # The canonical source form stores local sequence predecessors on each
        # node and composition ownership in parent_key. Count the effective
        # graph here rather than only the optional, pre-normalization edges
        # array, which is normally empty by the time a plan is accepted.
        "edge_count": len(sequence_edges | composition_edges),
        "sequence_edge_count": len(sequence_edges),
        "composition_edge_count": len(composition_edges),
        "document_ref_count": len(plan.document_refs),
        "artifact_count": len(plan.artifacts),
        "trigger_count": len(plan.triggers),
    }
    if plan.session_id:
        receipt["session_id"] = plan.session_id
    return ArtifactSpec(
        kind=ArtifactKind.JSON,
        name="plan-submission",
        content=receipt,
        ref=plan.subgraph_refs[0].ref if plan.subgraph_refs else None,
    )


class Runner:
    def __init__(
        self,
        store: Store,
        registry: Optional[WorkerRegistry] = None,
        events: Optional[EventBus] = None,
        settings: Settings | None = None,
        execution_adapter=None,
        herdr_adapter: HerdrAdapter | None = None,
        terminal_transport: TerminalTransport | None = None,
        trigger_dispatcher=None,
        semantic_plan_auditor: Callable[
            [object, PlanResult], Awaitable[PlanAuditResult]
        ] | None = None,
        manager_reviewer: Callable[[dict], Awaitable[ManagerResult]] | None = None,
    ):
        settings = settings or Settings()
        self.store = store
        self.registry = registry or build_registry(settings)
        self.events = events or EventBus()
        self.trigger_dispatcher = trigger_dispatcher
        self.semantic_plan_auditor = semantic_plan_auditor
        self.manager_reviewer = manager_reviewer
        # The served runtime enables fresh provider review turns explicitly.
        # Unit/test runners remain provider-neutral unless they inject a
        # callback, so deterministic fixtures never contact a live harness.
        self.provider_reviews_enabled = False
        self.s = settings
        self.harness_commands = HarnessCommandFactory(
            codex_binary=settings.codex_binary,
        )
        self.exec_adapter = execution_adapter or DirectExecutionAdapter(settings)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: Optional[asyncio.Task] = None
        # Give the first scheduled pass time to establish freshly-created
        # Herdr project mappings before treating an absent mapping as an
        # externally deleted workspace.
        self._last_workspace_reconcile_at = time.monotonic()
        # Herdr owns one durable project workspace and one pane per node. Turn
        # only opens short-lived control streams into those panes, so Herdr's
        # UI remains the place where project terminals are managed.
        terminal = terminal_transport or HerdrPtyTransport(
            settings.data_dir, adapter=herdr_adapter
        )
        self.sessions = SessionController(terminal)
        self.terminal = self.sessions.terminal
        # Shell access and harness access use the same per-node Herdr pane; the
        # UI's terminal endpoint still decides whether the node is generating,
        # so shell activity does not make a node appear active.
        self.shell = self.terminal
        self._shell_tasks: dict[uuid.UUID, asyncio.Task] = self.sessions.shell_tasks
        self._status_watchers: dict[uuid.UUID, asyncio.Task] = {}
        self._handoff_watchers: dict[uuid.UUID, asyncio.Task] = {}
        # A production daemon restart can detach from a provider while Herdr
        # keeps that provider's pane alive. These maps let the next Runner
        # generation preserve the in-flight run and settle that same Run when
        # the provider writes its handoff.
        self._recovered_active_node_ids: set[uuid.UUID] = set()
        self._recovered_run_ids: dict[uuid.UUID, uuid.UUID] = {}
        self._reconnect_tasks = self.sessions.reconnect_tasks
        self._forbidden_fresh_sessions = self.sessions.forbidden_fresh_sessions
        self.organization_manager = OrganizationManager()
        self.workspaces = WorkspaceManager(self.s.data_dir)
        self.scheduler = Scheduler(
            store=self.store,
            settings=self.s,
            execute_node=self._execute_node,
            emit=self._emit,
            finalize=self._maybe_finalize,
            wake=self.wake,
            is_externally_busy=lambda node_id: bool(
                (task := self._reconnect_tasks.get(node_id))
                and not task.done()
            ) or node_id in self._recovered_active_node_ids,
            isolation_available=self._workspace_isolation_available,
            request_review=self._request_organization_review,
        )
        self.node_executor = NodeExecutor(
            store=self.store,
            settings=self.s,
            scheduler=self.scheduler,
            status_watchers=self._status_watchers,
            forbidden_sessions=self._forbidden_fresh_sessions,
            emit=self._emit,
            wake=self.wake,
            ensure_terminal=self.ensure_node_terminal,
            has_persistent_session=self.terminal.has_persistent_session,
            close_persistent_session=self.terminal.close_persistent_session,
            detach_shell=self.detach_shell,
            stop_handoff_watcher=self._stop_handoff_watcher,
            agent_status_path=self._agent_status_path,
            watch_agent_status=self._watch_agent_status,
            plan_node=self._plan_node,
            run_worker=self._run_worker,
            mark_cancelled=self._mark_cancelled,
            mark_failed=self._mark_failed,
        )
        self.scheduler.set_executor(self.node_executor.execute)
        # Keep the existing Runner-level aliases for compatibility with
        # integrations that inspect runner state; Scheduler owns the actual
        # collections and all new control flows use its public API.
        self._running = self.scheduler.running
        self._running_projects = self.scheduler.running_projects
        self._retries = self.scheduler.retries
        self._manual_stages = self.scheduler.manual_stages
        self._last_launch_at = self.scheduler.last_launch_at
        self._deleting_projects = self.scheduler.deleting_projects

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self._recover_external_runs()
        self._task = asyncio.create_task(self._loop())
        # Restore editability for retained provider sessions after a daemon
        # restart. The CLI writes the same handoff files during an agent's
        # original run and during a later user-requested correction.
        for project in await self.store.list_projects():
            nodes = [project, *await self.store.descendants(project.id)]
            for node in nodes:
                if node.agent is not None and node.agent.session_id:
                    await self._ensure_handoff_watcher(
                        node.id, project.id, project.repo_path,
                    )

    async def stop(self, *, close_workspaces: bool = False) -> None:
        self._stop = True
        self._wake.set()
        self._manual_stages.clear()
        self._deleting_projects.clear()
        running = list(self._running.values())
        for node_id in list(self._running):
            # Herdr owns the provider process. A production daemon restart
            # must detach Turn's control stream without interrupting the
            # interactive provider; test runtimes that own their workspace
            # still use the hard stop path.
            if close_workspaces:
                await self.terminal.stop(node_id)
            else:
                await self.terminal.detach(node_id)
        for t in running:
            t.cancel()
        await self.sessions.stop_all()
        for task in self._handoff_watchers.values():
            task.cancel()
        if self._handoff_watchers:
            await asyncio.gather(*self._handoff_watchers.values(), return_exceptions=True)
            self._handoff_watchers.clear()
        for t in self._status_watchers.values():
            t.cancel()
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

    async def _recover_external_runs(self) -> None:
        """Preserve persisted RUNNING runs whose Herdr provider is alive."""
        self._recovered_active_node_ids.clear()
        self._recovered_run_ids.clear()
        if not getattr(self.terminal, "supports_inject", False):
            return
        reconcile = getattr(self.terminal, "reconcile_provider_session", None)
        foreground = getattr(self.terminal, "foreground_process_names", None)
        if reconcile is None and foreground is None:
            return
        for project in await self.store.list_projects():
            nodes = [project, *await self.store.descendants(project.id)]
            for node in nodes:
                if node.status is not NodeStatus.RUNNING or node.agent is None:
                    continue
                runs = await self.store.get_runs(node.id)
                active_run = next(
                    (run for run in reversed(runs) if run.status is RunStatus.RUNNING),
                    None,
                )
                if active_run is None or not node.agent.session_id:
                    continue
                try:
                    matched = False
                    if reconcile is not None:
                        matched = await reconcile(
                            node.id,
                            project_key=str(project.id),
                            session_id=node.agent.session_id,
                            provider=node.agent.harness.value,
                        )
                    if not matched and not await self.terminal.has_persistent_session(node.id):
                        continue
                    if foreground is None:
                        matched = True
                    else:
                        names = {
                            Path(name).name.lower()
                            for name in await foreground(node.id)
                            if isinstance(name, str) and name
                        }
                        provider = node.agent.harness.value.lower()
                        matched = matched or provider in names
                except (HerdrResourceNotFound, OSError, RuntimeError):
                    continue
                if not matched:
                    continue
                self._recovered_active_node_ids.add(node.id)
                self._recovered_run_ids[node.id] = active_run.id

    def _take_recovered_run(
        self, node_id: uuid.UUID, runs: list[Run]
    ) -> Run | None:
        run_id = self._recovered_run_ids.pop(node_id, None)
        self._recovered_active_node_ids.discard(node_id)
        if run_id is None:
            return None
        return next((run for run in runs if run.id == run_id), None)

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
        return self.scheduler.begin_project_deletion(project_id)

    def end_project_deletion(self, project_id: uuid.UUID) -> None:
        self.scheduler.end_project_deletion(project_id)

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
        if now - self._last_workspace_reconcile_at >= 5.0:
            try:
                await self._reconcile_project_workspaces(projects)
            except HerdrAdapterError as error:
                # Workspace reconciliation is advisory. A transient failure
                # from the user-owned daemon must not block graph scheduling
                # or be mistaken for an externally deleted workspace.
                logger.warning("Herdr workspace reconciliation deferred: %s", error)
            self._last_workspace_reconcile_at = now
            projects = await self.store.list_projects()
        for p in projects:
            if p.id in self._deleting_projects:
                continue
            try:
                await self._schedule_project(p.id)
            except Exception as e:  # pragma: no cover
                print(f"[runner] schedule error for {p.id}: {e}")

    async def schedule_once(self, project_id: uuid.UUID) -> None:
        """Run one scheduler pass for a project through the public contract."""
        await self._review_safe_organizations(project_id)
        await self.scheduler.schedule_once(project_id)

    def active_node_ids(self, project_id: uuid.UUID | None = None) -> frozenset[uuid.UUID]:
        """Return nodes with live runner tasks, without exposing task objects."""
        return self.scheduler.active_node_ids(project_id)

    async def wait_for_idle(self, project_id: uuid.UUID | None = None) -> None:
        """Wait until all currently active work for a project has settled."""
        await self.scheduler.wait_for_idle(project_id)

    async def _reconcile_project_workspaces(self, projects: list[Node]) -> None:
        """Reattach projects whose provider workspace disappeared externally.

        Project state is durable on disk and a provider workspace is only an
        execution resource. Losing the latter must never delete the former;
        recreate the workspace and its root pane instead.
        """
        projects = [project for project in projects if project.id not in self._deleting_projects]
        await self.terminal.close_orphaned_project_workspaces(
            {str(project.id) for project in projects}
        )
        states = await self.terminal.project_workspace_states(
            {str(project.id) for project in projects}
        )
        for project in projects:
            # Reconciliation runs from the same heartbeat as scheduling. A
            # provider can briefly report a stale/missing workspace while a
            # run is opening its control stream; never close that workspace
            # underneath an active node.
            nodes, _, _ = await self.store.get_workgraph(project.id)
            if any(self.generation_active(node.id) for node in nodes):
                continue
            state = states.get(str(project.id), "unmapped")
            if state != "missing":
                continue
            await self.cancel_project_runs(project.id)
            # The Herdr space has already disappeared externally. Forget the
            # stale mapping, then recreate the provider workspace from the
            # durable project repository without touching graph state.
            await self.terminal.close_project_workspace(str(project.id))
            recreated = await self.ensure_node_terminal(project.id)
            await self._emit(
                "project.workspace.recreated",
                project.id,
                {
                    "source": "herdr",
                    "reason": "workspace_missing",
                    "recreated": recreated,
                },
            )

    async def close_project_workspace(self, project_id: uuid.UUID) -> bool:
        """Close a project's Herdr space without touching unrelated workspaces."""
        nodes, _, _ = await self.store.get_workgraph(project_id)
        for node in nodes:
            # Close each node pane first so no provider, shell, or control
            # process survives while the project workspace is being removed.
            await self.terminal.close_persistent_session(node.id)
        return await self.terminal.close_project_workspace(str(project_id))

    async def _project_repo(self, project_id: uuid.UUID) -> str | None:
        """Resolve the filesystem directory assigned to a project."""
        root = await self.store.get_node(project_id)
        if root is None:
            return None
        if root.repo_path:
            return str(Path(root.repo_path).expanduser().resolve())
        project_path = self.store.project_path(project_id)
        return str(project_path.expanduser().resolve()) if project_path is not None else None

    async def _workspace_isolation_available(self, project_id: uuid.UUID) -> bool:
        """Ask the workspace adapter whether this project may run in parallel."""
        repo = await self._project_repo(project_id)
        return bool(repo and await self.workspaces.isolation_available(repo))

    async def _request_organization_review(
        self, organization_id: uuid.UUID, reason: str
    ) -> None:
        await self.organization_manager.request_review(self.store, organization_id, reason)

    async def _schedule_project(self, project_id: uuid.UUID) -> None:
        # Compatibility shim for older in-process callers. Scheduling
        # decisions and task reservation live in Scheduler.
        await self._review_safe_organizations(project_id)
        await self.scheduler.schedule_once(project_id)

    async def _review_safe_organizations(self, project_id: uuid.UUID) -> None:
        """Review settled material boundaries before the scheduler stalls.

        Scheduler finalization only runs when every projected node is terminal.
        A material planner is intentionally projected as EXPANDED until its
        manager accepts it, so waiting for that finalizer would deadlock the
        organization at the exact safe point where a manager should decide.
        Reviews are deepest-first and only happen when no descendant provider
        is live and no runnable frontier remains.
        """
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return
        walker = GraphWalker(nodes, edges)
        evaluation = walker.evaluate()
        material = [
            node
            for node in nodes
            if node.executor == PLANNER_EXECUTOR
            and node.organization_contract is not None
            and node.organization_contract.scale.value != "focused"
            and node.manager_phase in {
                ManagerPhase.EXECUTING,
                ManagerPhase.REVIEW_PENDING,
            }
            and node.status in {NodeStatus.EXPANDED, NodeStatus.COMPLETE}
        ]
        material.sort(key=lambda item: walker.depth(item.id), reverse=True)
        safe: list[Node] = []
        material_ids = {node.id for node in material}
        for boundary in material:
            descendants = walker.descendants(boundary.id)
            descendant_ids = {node.id for node in descendants}
            if any(self.generation_active(node_id) for node_id in descendant_ids):
                continue
            if any(
                node.id in descendant_ids
                and node.status
                in {NodeStatus.PENDING, NodeStatus.RUNNABLE, NodeStatus.RUNNING}
                for node in nodes
            ):
                # A settled-looking graph can still contain an in-process or
                # not-yet-runnable descendant. Do not promote the boundary to
                # review until that execution frontier is genuinely settled.
                continue
            if any(
                node.id in descendant_ids
                and node.manager_phase not in {
                    ManagerPhase.ACCEPTED,
                    ManagerPhase.BLOCKED,
                }
                and node.id in material_ids
                for node in descendants
            ):
                # Let the deepest unsettled organization finish its own
                # review before its parent judges the combined charter.
                continue
            if evaluation.runnable & descendant_ids:
                continue
            work_items = await self.store.list_work_items(
                project_id, organization_id=boundary.id
            )
            if any(
                item.status not in {
                    WorkItemStatus.COMPLETE,
                    WorkItemStatus.CANCELLED,
                }
                and item.node_id is None
                for item in work_items
            ):
                continue
            safe.append(boundary)
        if safe:
            await self._review_organizations(project_id, boundaries=safe)

    # -- execution -------------------------------------------------------

    async def _execute_node(self, node: Node, project_id: uuid.UUID) -> None:
        """Compatibility entry point; NodeExecutor owns the lifecycle."""
        await self.node_executor.execute(node, project_id)

    async def _agent_status_path(self, node: Node) -> Path | None:
        # Status is control-plane state. It must remain visible after a
        # worker worktree is removed and must never be confused with source
        # files in the execution workspace.
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

    def _handoff_paths(self, repo_path: str, node_id: uuid.UUID) -> tuple[Path, ...]:
        root = Path(repo_path) / ".turn" / "interactive"
        return tuple(root / f"{node_id}.{kind}.json" for kind in ("plan", "result", "verification"))

    async def _ensure_handoff_watcher(
        self, node_id: uuid.UUID, project_id: uuid.UUID, repo_path: str | None
    ) -> None:
        """Keep every retained provider conversation able to accept edits."""
        if self._stop:
            return
        if not repo_path or not getattr(self.terminal, "supports_inject", False):
            return
        node = await self.store.get_node(node_id)
        if node is None or node.agent is None or not node.agent.session_id:
            return
        existing = self._handoff_watchers.get(node_id)
        if existing is not None and not existing.done():
            return
        self._handoff_watchers[node_id] = asyncio.create_task(
            self._watch_agent_handoffs(
                node_id,
                project_id,
                self._handoff_paths(repo_path, node_id),
                repo_path,
            )
        )

    async def _stop_handoff_watcher(self, node_id: uuid.UUID) -> None:
        task = self._handoff_watchers.pop(node_id, None)
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _handoff_kind(path: Path) -> str:
        return path.name.rsplit(".", 2)[-2]

    async def _watch_agent_handoffs(
        self,
        node_id: uuid.UUID,
        project_id: uuid.UUID,
        paths: tuple[Path, ...],
        repo_path: str,
    ) -> None:
        """Apply later plan, result, or verification submissions."""
        try:
            while not self._stop:
                # A live provider worker owns the attempt-scoped handoff file
                # and will parse it when its PTY returns. Retained-session
                # watchers are for edits after that run has settled; letting
                # one consume the file first creates a second Run and leaves
                # the real provider task waiting forever for a file that was
                # already unlinked.
                active_run = self._running.get(node_id)
                if active_run is not None and not active_run.done():
                    await asyncio.sleep(0.05)
                    continue
                submission: tuple[str, Path, dict, bool] | None = None
                for path in paths:
                    payload = read_result_file(path)
                    if payload is not None:
                        force = bool(payload.pop("__turn_force", False))
                        submission = (self._handoff_kind(path), path, payload, force)
                        break
                if submission is None:
                    await asyncio.sleep(0.05)
                    continue
                kind, path, payload, force = submission
                # Claim the current atomic handoff before processing it. A
                # user may submit a second correction while this one is
                # updating the graph; deleting in ``finally`` would erase
                # that newer payload.
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                try:
                    current = await self.store.get_node(node_id)
                    if current is None:
                        return
                    if kind == "plan":
                        plan = parse_plan(payload)
                        catalog = CapabilityCatalog(Path(self.s.data_dir) / "capabilities")
                        plan_payload = plan.model_dump(mode="json")
                        catalog.load_plan_role_capabilities(plan_payload, repo_path)
                        catalog.validate_plan(
                            plan_payload,
                            repo_path,
                            planner_capabilities=current.agent.capabilities if current.agent else None,
                        )
                        validate_subgraph_sources(plan, repo_path)
                        await self._apply_plan_revision(node_id, project_id, plan, force=force)
                        await self._emit_trigger_event(
                            "agent.plan.submitted",
                            project_id=project_id,
                            node_id=node_id,
                            data={"node_id": str(node_id), "kind": kind},
                        )
                    elif kind == "verification" or (
                        kind == "result"
                        and "decision" in payload
                        and "outcome" not in payload
                    ):
                        await self._apply_verification_revision(
                            node_id, project_id, parse_verification(payload)
                        )
                        await self._emit_trigger_event(
                            "agent.verification.submitted",
                            project_id=project_id,
                            node_id=node_id,
                            data={"node_id": str(node_id), "kind": "verification"},
                        )
                    elif kind == "result":
                        await self._apply_result_revision(
                            node_id, project_id, parse_result(payload)
                        )
                        await self._emit_trigger_event(
                            "agent.result.submitted",
                            project_id=project_id,
                            node_id=node_id,
                            data={"node_id": str(node_id), "kind": kind},
                        )
                    current = await self.store.get_node(node_id)
                    if current is not None and current.agent_state == "correction_required":
                        await self.store.set_agent_status(node_id, state=None, message=None)
                except Exception as error:
                    logger.exception("agent %s revision failed for node %s", kind, node_id)
                    current = await self.store.get_node(node_id)
                    if current is not None:
                        await self.store.set_agent_status(
                            node_id,
                            state="correction_required",
                            message=(
                                f"{kind} submission rejected: "
                                f"{sanitize_control_text(error)}. Correct and resubmit in the live provider session."
                            ),
                        )
                        await self._emit(
                            "node.updated", project_id, _dump(await self.store.get_node(node_id))
                        )
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if current_task is not None and self._handoff_watchers.get(node_id) is current_task:
                self._handoff_watchers.pop(node_id, None)

    async def _apply_plan_revision(
        self, node_id: uuid.UUID, project_id: uuid.UUID, plan: PlanResult, *, force: bool = False
    ) -> list[Node]:
        """Apply an explicit source-file replacement for this boundary.

        This is intentionally different from a normal planner handoff with
        ``nodes=[]``. The CLI watcher calls this method for a submitted plan
        file, so an intentionally empty replacement still clears descendants;
        the normal planner path uses ``Store.apply_plan`` directly and keeps
        an existing composition intact.
        """
        node = await self.store.get_node(node_id)
        if node is None:
            return []
        contract = self._organization_contract_for_plan(node, plan)
        structural_audit = None
        semantic_audit = None
        if contract is not None:
            scale_rank = {
                OrganizationScale.FOCUSED: 0,
                OrganizationScale.DELIVERY: 1,
                OrganizationScale.ORGANIZATION: 2,
            }
            if (
                node.organization_contract is not None
                and scale_rank[contract.scale]
                < scale_rank[node.organization_contract.scale]
            ):
                raise RuntimeError(
                    "organization plan cannot downgrade its existing contract scale"
                )
            structural_audit = audit_plan(contract, plan)
            if not structural_audit.accepted:
                rejection = PlanAuditResult(
                    decision=PlanAuditDecision.REJECT,
                    summary="Deterministic plan audit rejected the proposal.",
                    findings=list(structural_audit.errors),
                    required_changes=list(structural_audit.errors),
                )
                await self._record_plan_audit(
                    node.id, structural=structural_audit, semantic=rejection
                )
                raise RuntimeError(
                    "organization plan rejected: " + "; ".join(structural_audit.errors)
                )
            if node.id == project_id or contract.scale.value != "focused":
                try:
                    semantic_audit = await self._run_semantic_plan_audit(node, contract, plan)
                except ControlOperationUnavailable as error:
                    await self.store.set_status(node.id, NodeStatus.RUNNABLE)
                    await self._emit(
                        "organization.audit_control_failed",
                        project_id,
                        {"node_id": str(node.id), "reason": sanitize_control_text(error), "retryable": True},
                    )
                    return []
                if semantic_audit is not None:
                    await self._record_plan_audit(
                        node.id,
                        structural=structural_audit,
                        semantic=semantic_audit,
                    )
                if semantic_audit is not None and semantic_audit.decision is PlanAuditDecision.REJECT:
                    raise RuntimeError(
                        "semantic organization plan audit rejected the plan: "
                        + "; ".join(
                            semantic_audit.required_changes or semantic_audit.findings
                        )
                    )
            else:
                await self._record_plan_audit(node.id, structural=structural_audit)
        incoming_refs = {
            reference.ref
            for reference in [
                *plan.subgraph_refs,
                *(reference for item in plan.nodes for reference in item.subgraph_refs),
            ]
        }
        # A submitted plan is an agent-owned replacement of this planner's
        # boundary. Its exact node/source set is authoritative, including an
        # intentional deletion. The force guard remains for direct destructive
        # regeneration APIs, not for the planner's own graph revision.
        removed = await self._remove_descendants_before_replan(
            node_id, force=True, preserved_refs=incoming_refs
        )
        node = await self.store.get_node(node_id)
        if node is None:
            return []
        prior_runs = await self.store.get_runs(node.id)
        run = self._take_recovered_run(node.id, prior_runs)
        if run is None:
            run = await self.store.create_run(node, PLANNER_EXECUTOR, len(prior_runs) + 1)
        # A successful user-directed revision supersedes any error/status
        # message left by an earlier failed submission.
        node.agent_state = None
        node.agent_message = None
        # A replacement owns the exact source links in the submitted plan;
        # links omitted by the planner are intentionally removed with the
        # descendants above.
        node.subgraph_refs = []
        created = await self.store.apply_plan(
            node,
            plan,
            enforce_organization_audit=True,
        )
        if not created and (
            node.organization_contract is None
            or node.organization_contract.scale.value == "focused"
        ):
            # An explicit empty replacement is a valid focused no-op handoff;
            # its boundary has no remaining executable frontier.
            await self.store.set_status(node.id, NodeStatus.COMPLETE)
        artifacts = await self.store.add_artifacts(
            node.id,
            [_plan_submission_artifact(plan)],
        )
        for artifact in artifacts:
            await self._emit("artifact.created", project_id, _dump(artifact))
        await self.store.update_run(
            run.id,
            status=RunStatus.COMPLETE,
            outcome=Outcome.COMPLETE,
            summary=f"revised plan with {len(created)} node(s)",
            logs=f"replaced {len(removed)} descendant node(s)",
            usage=plan.usage,
            session_id=plan.session_id or (node.agent.session_id if node.agent else None),
        )
        if plan.session_id:
            await self._remember_session(node, plan.session_id)
        await self._emit(
            "graph.replaced",
            project_id,
            {"node": _dump(node), "removed": [str(item) for item in removed], "created": len(created)},
        )
        await self._emit(
            "plan.applied", project_id, {"parent": _dump(node), "created": len(created)}
        )
        for child in created:
            await self._emit("node.created", project_id, _dump(child))
        self.wake()
        return created

    async def _apply_result_revision(
        self, node_id: uuid.UUID, project_id: uuid.UUID, result: WorkerResult
    ) -> None:
        await self._settle_reconnect_after_handoff(node_id)
        node = await self.store.get_node(node_id)
        if node is None:
            return
        prior_runs = await self.store.get_runs(node.id)
        run = self._take_recovered_run(node.id, prior_runs)
        if run is None:
            run = await self.store.create_run(
                node,
                node.agent.harness.value if node.agent else node.executor or "agent",
                len(prior_runs) + 1,
            )
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._handle_outcome(node, run, project_id, result)

    async def _apply_verification_revision(
        self, node_id: uuid.UUID, project_id: uuid.UUID, decision: VerificationResult
    ) -> None:
        await self._settle_reconnect_after_handoff(node_id)
        node = await self.store.get_node(node_id)
        if node is None:
            return
        prior_runs = await self.store.get_runs(node.id)
        run = self._take_recovered_run(node.id, prior_runs)
        if run is None:
            run = await self.store.create_run(
                node,
                node.agent.harness.value if node.agent else node.executor or "agent",
                len(prior_runs) + 1,
            )
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._handle_outcome(
            node,
            run,
            project_id,
            WorkerResult(outcome=Outcome.COMPLETE, verification=decision),
        )

    async def _settle_reconnect_after_handoff(self, node_id: uuid.UUID) -> None:
        """Release a completed rejection follow-up before accepting its handoff.

        A native rejection follow-up deliberately retains its Herdr pane so a
        later correction can resume the provider session.  Its reconnect task
        waits on that durable pane, though, and therefore stays alive after the
        agent has written a result or verification handoff.  Leaving it in the
        task map suppresses the next rejection prompt and makes the UI show a
        node as preparing without an actual new harness launch.

        Cancelling the control task only detaches Turn's awaiter; the durable
        provider pane and its session remain available for the next reconnect.
        """
        task = self._reconnect_tasks.get(node_id)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _remove_descendants_before_replan(
        self,
        node_id: uuid.UUID,
        *,
        force: bool = False,
        preserved_refs: set[str] | None = None,
    ) -> list[uuid.UUID]:
        descendants = await self.store.descendants(node_id)
        running = [
            descendant
            for descendant in descendants
            if descendant.status is NodeStatus.RUNNING
        ]
        if running:
            ids = ", ".join(str(item.id) for item in running)
            raise RuntimeError(
                "cannot replace a graph containing running nodes: "
                f"{ids}; wait for them to finish or cancel them first"
            )
        composed = [
            item
            for item in [*descendants, await self.store.get_node(node_id)]
            if item is not None and item.subgraph_refs
        ]
        existing_refs = {
            reference.ref
            for item in composed
            for reference in item.subgraph_refs
            if not reference.managed
        }
        missing_refs = existing_refs - (preserved_refs or set())
        if missing_refs and not force:
            raise RuntimeError(
                "graph contains composed subgraphs; preserve their links or "
                "resubmit with --force to replace them"
            )
        cancelling: list[asyncio.Task] = []
        for descendant in descendants:
            await self._stop_handoff_watcher(descendant.id)
            task = self._running.get(descendant.id)
            if task is not None and not task.done():
                await self.terminal.stop(descendant.id)
                task.cancel()
                cancelling.append(task)
        if cancelling:
            await asyncio.gather(*cancelling, return_exceptions=True)
        for descendant in descendants:
            await self.terminal.close_persistent_session(descendant.id)
        return await self.store.replace_descendants(
            node_id,
            force=force,
            preserved_refs=preserved_refs,
        )

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

    def _review_planner_for(self, node: Node):
        """Return the real planner adapter used for provider review turns.

        Reviews are management operations, not graph nodes. They still use
        the provider adapter that owns the planner contract, but only the
        served runtime enables this path. Test runners remain deterministic
        unless they inject a review callback.
        """
        agent = node.agent
        if (
            not self.provider_reviews_enabled
            or agent is None
            or agent.harness.value not in REAL_HARNESSES
        ):
            return None
        planner = self.registry.get_planner("real") or self.registry.planner
        if planner is None or not callable(getattr(planner, "call_structured", None)):
            return None
        return planner

    @staticmethod
    def _structured_artifact_payload(
        payload: dict,
        *,
        schema_name: str,
        artifact_name: str,
        schema_version: str = "v1",
    ) -> dict:
        """Extract one typed JSON artifact from a normal WorkerResult envelope."""
        return parse_structured_artifact(
            payload,
            schema_name=schema_name,
            artifact_name=artifact_name,
            schema_version=schema_version,
        )

    @staticmethod
    def _normalize_plan_audit_payload(content: dict) -> dict:
        """Normalize provider-friendly finding objects to the audit contract.

        The semantic-audit artifact is intentionally small, but providers
        commonly add useful fields such as ``area`` and ``severity`` to each
        finding.  Keep that information in the persisted human-readable
        strings instead of failing the whole planning run at the Pydantic
        boundary.
        """
        return parse_plan_audit(content).model_dump(mode="python")

    @staticmethod
    def _normalize_manager_result_payload(content: dict) -> dict:
        """Normalize provider manager reports to the retained manager schema.

        A manager review is a decision about an already-materialized
        boundary. Providers sometimes put their completion report under the
        JSON key ``plan`` even when they are not proposing a graph plan. Only
        a dictionary containing the real PlanResult ``nodes`` field is a
        graph plan; report metadata must not be validated as one.
        """
        return parse_manager_result(content).model_dump(mode="python")

    @staticmethod
    def _organization_contract_for_plan(node: Node, plan: PlanResult):
        """Resolve the contract declared by the current planner boundary.

        A nested planner may refine its inherited boundary with an explicit
        contract. Explicit contracts are authoritative here; the structural
        audit and store guard still reject a downgrade. An omitted contract
        inherits the persisted boundary.
        """
        return plan.organization_contract or node.organization_contract

    @staticmethod
    def _review_context(
        base: NodeExecutionContext,
        node: Node,
        *,
        purpose: str,
        repo_path: str | None,
    ) -> NodeExecutionContext:
        """Build an isolated provider context for a non-graph review turn."""
        if node.agent is None:
            raise RuntimeError("provider review requires a planner agent")
        review_node = node.model_copy(
            update={
                # A review writes the ordinary result handoff protocol, but it
                # must not collide with a live planner handoff watcher.
                "id": uuid.uuid4(),
            }
        )
        return base.model_copy(
            update={
                "node": review_node,
                "repo_path": repo_path or base.repo_path,
                "project_repo_path": base.project_repo_path or repo_path,
                # A review is deliberately a bounded one-shot provider turn.
                # LocalPtyTransport is selected by AgentPlanner when terminal
                # is None, while the retained session remains on the copied
                # planner agent when the manager invokes this context.
                "terminal": None,
                "session_callback": None,
                "forbidden_session_id": None,
                "purpose": purpose,
                "review_feedback": None,
                "interactive_terminal": False,
            }
        )

    async def _run_semantic_plan_audit(
        self,
        node: Node,
        contract,
        plan: PlanResult,
        ctx: NodeExecutionContext | None = None,
    ) -> PlanAuditResult | None:
        """Run an injected or provider-backed independent plan audit."""
        if self.semantic_plan_auditor is not None:
            runs = await self.store.get_runs(node.id)
            last_error: Exception | None = None
            for attempt in range(1, 4):
                run = await self.store.create_run(
                    node, "semantic-plan-auditor", len(runs) + attempt
                )
                try:
                    audit = await self.semantic_plan_auditor(contract, plan)
                    await self.store.update_run(
                        run.id,
                        status=RunStatus.COMPLETE,
                        outcome=Outcome.COMPLETE,
                        summary=audit.summary,
                    )
                    return audit
                except Exception as error:
                    last_error = error
                    await self.store.update_run(
                        run.id,
                        status=RunStatus.FAILED,
                        outcome=Outcome.FAIL,
                        summary="semantic organization plan audit failed",
                        error=sanitize_control_text(error),
                        retry_recommended=attempt < 3,
                    )
                    if attempt < 3:
                        await asyncio.sleep(0.05 * attempt)
            raise ControlOperationUnavailable(
                "semantic organization plan audit unavailable after 3 attempts: "
                + sanitize_control_text(last_error)
            ) from last_error
        planner = self._review_planner_for(node)
        if planner is None:
            return None
        base = ctx or await self._build_context(node)
        if node.agent is None:
            raise RuntimeError("provider semantic audit requires a planner agent")
        audit_agent = node.agent.as_type(AgentType.PLANNER).model_copy(
            update={"session_id": None}
        )
        audit_node = node.model_copy(update={"agent": audit_agent})
        review_ctx = self._review_context(
            base,
            audit_node,
            purpose="semantic-plan-audit",
            repo_path=base.project_repo_path or base.repo_path,
        )
        prompt = "\n".join(
            [
                render_context_block(review_ctx),
                "TURN_INDEPENDENT_PLAN_AUDIT",
                "You are an independent semantic auditor for the organization charter below.",
                "Inspect the real project files and the proposed plan. Do not edit files, create graph nodes, or perform the work.",
                "Approve only a coherent plan that preserves the charter, assigns one cohesive verifiable responsibility per leaf, includes real convergence and independent verification where required, and can complete within the stated budget.",
                "Reject plans that compress an organization into a flat checklist, duplicate ownership, hide unfinished work in a single vague executor, or claim evidence without an inspectable artifact.",
                "Mechanical guarantees: Turn injects role capabilities (including turn-basics and role-specific planning/execution skills), normalizes planner roles, inherits organization contracts for omitted nested planner contracts, and canonicalizes equivalent provider payload aliases. Do not reject a plan for omitting any property Turn guarantees mechanically; audit material semantic adequacy only.",
                "Return exactly one normal Turn WorkerResult envelope with outcome COMPLETE and one JSON artifact named 'plan-audit' (schema_name 'turn.plan-audit', schema_version 'v1'). The artifact content must be: decision APPROVE or REJECT, summary, findings, required_changes.",
                "ORGANIZATION_CONTRACT_JSON="
                + json.dumps(contract.model_dump(mode="json"), sort_keys=True),
                "PROPOSED_PLAN_JSON="
                + json.dumps(plan.model_dump(mode="json"), sort_keys=True),
            ]
        )
        runs = await self.store.get_runs(node.id)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            run = await self.store.create_run(
                node, "semantic-plan-auditor", len(runs) + attempt
            )
            try:
                payload, usage, session_id = await planner.call_structured(
                    review_ctx,
                    prompt,
                    handoff_kind="result",
                )
                content = self._structured_artifact_payload(
                    payload,
                    schema_name="turn.plan-audit",
                    artifact_name="plan-audit",
                )
                audit = parse_plan_audit(content)
                await self.store.update_run(
                    run.id,
                    status=RunStatus.COMPLETE,
                    outcome=Outcome.COMPLETE,
                    summary=audit.summary,
                    usage=usage,
                    session_id=session_id,
                )
                return audit
            except Exception as error:
                last_error = error
                await self.store.update_run(
                    run.id,
                    status=RunStatus.FAILED,
                    outcome=Outcome.FAIL,
                    summary="semantic organization plan audit failed",
                    error=sanitize_control_text(error),
                    retry_recommended=attempt < 3,
                )
                if attempt < 3:
                    await asyncio.sleep(0.05 * attempt)
        raise ControlOperationUnavailable(
            "semantic organization plan audit unavailable after 3 attempts: "
            + sanitize_control_text(last_error)
        ) from last_error

    async def _record_plan_audit(
        self,
        node_id: uuid.UUID,
        *,
        structural=None,
        semantic: PlanAuditResult | None = None,
        correction_count: int = 0,
    ) -> None:
        """Persist concise audit operations data without hidden reasoning."""
        current = await self.store.get_node(node_id)
        if current is None:
            return
        review = current.organization_review or OrganizationReview()
        if structural is not None:
            review.audit = structural
        if semantic is not None:
            review.audit_decision = semantic.decision
            review.audit_summary = semantic.summary
            review.audit_findings = list(semantic.findings)
            review.audit_required_changes = list(semantic.required_changes)
            review.audit_correction_count = correction_count
            review.audit_updated_at = datetime.now(timezone.utc)
            review.replan_requested = semantic.decision is PlanAuditDecision.REJECT
            review.last_reason = semantic.summary
            review.phase = (
                OrganizationPhase.REPLAN
                if semantic.decision is PlanAuditDecision.REJECT
                else OrganizationPhase.EXECUTE_FRONTIER
            )
        elif structural is not None:
            review.audit_updated_at = datetime.now(timezone.utc)
        await self.store.set_organization_review(node_id, review)

    async def _provider_manager_review(self, snapshot: dict) -> ManagerResult:
        """Ask the retained planner session for a typed management decision."""
        boundary_data = snapshot.get("boundary") or {}
        try:
            boundary_id = uuid.UUID(str(boundary_data["id"]))
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("manager snapshot has no valid boundary id") from error
        boundary = await self.store.get_node(boundary_id)
        if boundary is None:
            raise RuntimeError(f"manager boundary disappeared: {boundary_id}")
        planner = self._review_planner_for(boundary)
        if planner is None:
            raise RuntimeError("no real planner adapter is available for manager review")
        if boundary.agent is None:
            raise RuntimeError("manager review requires the retained planner agent")
        base = await self._build_context(boundary)
        manager_agent = boundary.agent.as_type(AgentType.PLANNER)
        manager_node = boundary.model_copy(update={"agent": manager_agent})
        review_ctx = self._review_context(
            base,
            manager_node,
            purpose="organization-manager-review",
            repo_path=base.project_repo_path or base.repo_path,
        )
        # A completed native planner handoff leaves its Codex TUI in the
        # durable pane so the user can inspect or reconnect it. Manager
        # review resumes the same provider session in a separate, bounded
        # structured call; close that stale writer first or Codex rejects the
        # resume with an "active writer" error. A live Turn-owned generation
        # is never touched here.
        if not self.generation_active(boundary_id):
            await self.terminal.close_persistent_session(boundary_id)
        prompt = "\n".join(
            [
                render_context_block(review_ctx),
                "TURN_ORGANIZATION_MANAGER_REVIEW",
                "You are the retained planner acting as the organization manager at a safe point.",
                "Observe the current persisted snapshot and real project files. Do not directly edit files or mutate Turn state.",
                "Choose ACCEPT only when the charter, deliverables, every acceptance criterion, independent verification, required handoffs, and the backlog are actually complete with inspectable evidence.",
                "Choose CONTINUE when more bounded work is required. Return work_items only; graph changes belong to a retained planner turn, not the manager result. Every work item must have a unique key, precise instructions, explicit acceptance criteria when needed, and dependencies that refer to existing or same-response keys.",
                "Choose BLOCK when missing user input, an impossible constraint, or a hard budget/workspace failure prevents safe progress. Include missing_inputs when user action is required.",
                "Return exactly one normal Turn WorkerResult envelope with outcome COMPLETE and one JSON artifact named 'manager-result' (schema_name 'turn.manager-result', schema_version 'v1'). The artifact content must be a ManagerResult object with decision ACCEPT, CONTINUE, or BLOCK, summary, and the relevant work_items or missing_inputs. ACCEPT has no work_items; CONTINUE returns work_items only; BLOCK may return missing_inputs.",
                "ORGANIZATION_SNAPSHOT_JSON="
                + json.dumps(snapshot, sort_keys=True),
            ]
        )
        runs = await self.store.get_runs(boundary.id)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            run = await self.store.create_run(
                boundary, "organization-manager", len(runs) + attempt
            )
            try:
                payload, usage, session_id = await planner.call_structured(
                    review_ctx,
                    prompt,
                    handoff_kind="result",
                )
                content = self._structured_artifact_payload(
                    payload,
                    schema_name="turn.manager-result",
                    artifact_name="manager-result",
                )
                result = parse_manager_result(content)
                await self.store.update_run(
                    run.id,
                    status=RunStatus.COMPLETE,
                    outcome=Outcome.COMPLETE,
                    summary=result.summary,
                    usage=usage,
                    session_id=session_id,
                )
                if session_id:
                    await self._remember_session(boundary, session_id)
                return result
            except Exception as error:
                last_error = error
                await self.store.update_run(
                    run.id,
                    status=RunStatus.FAILED,
                    outcome=Outcome.FAIL,
                    summary="organization manager review failed",
                    error=sanitize_control_text(error),
                    retry_recommended=attempt < 3,
                )
                if attempt < 3:
                    await asyncio.sleep(0.05 * attempt)
        raise ControlOperationUnavailable(
            "organization manager review unavailable after 3 attempts: "
            + sanitize_control_text(last_error)
        ) from last_error

    async def _plan_node(
        self,
        node: Node,
        project_id: uuid.UUID,
        *,
        forbidden_session_id: str | None = None,
    ) -> list[Node]:
        # The planner and all descendants use the same assigned project
        # directory, so files are immediately available downstream.
        self._recovered_active_node_ids.discard(node.id)
        self._recovered_run_ids.pop(node.id, None)
        prior_runs = await self.store.get_runs(node.id)
        run = await self.store.create_run(node, PLANNER_EXECUTOR, len(prior_runs) + 1)
        ctx = await self._build_context(node, run_id=str(run.id))
        ctx.forbidden_session_id = forbidden_session_id
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))

        # Stop can arrive immediately after the run record makes the node
        # visible as RUNNING, before this coroutine reaches the harness call.
        # Do not launch a process for a run that has already been cancelled.
        current = await self.store.get_node(node.id)
        if current is None or current.status is NodeStatus.CANCELLED:
            await self.store.update_run(
                run.id,
                status=RunStatus.CANCELLED,
                outcome=Outcome.FAIL,
                summary="run cancelled",
                error="run cancelled before harness launch",
            )
            await self._finish_provider_terminal(node.id, project_id)
            return []

        capability_launch = (
            self._prepare_capabilities(node.agent, ctx.repo_path, node.id)
            if node.agent and node.agent.harness in REAL_HARNESSES
            else CapabilityLaunch()
        )

        await self._emit("harness.launch", project_id, {
            "run_id": str(run.id), "node_id": str(node.id), "harness": node.agent.harness.value if node.agent else node.executor,
            "model": node.agent.model if node.agent else None, "reasoning": node.agent.reasoning.value if node.agent else None,
            "session_id": node.agent.session_id if node.agent else None, "attempt": run.attempt,
            "timeout_seconds": ctx.timeout_seconds, "purpose": "plan", "repo_path": ctx.repo_path,
            "flags": self._launch_flags(node, resume=bool(node.agent and node.agent.session_id)),
            "role": "setup" if node.id == project_id else (node.agent.type_id.value if node.agent else None),
            "capability_skills": list(capability_launch.skill_names),
        })

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
            planner = self._planner_for(node)
            if planner is None:
                raise RuntimeError("no planner registered")
            plan: PlanResult | None = None
            contract = node.organization_contract
            last_structural_audit = None
            last_semantic_audit: PlanAuditResult | None = None
            corrections_used = 0
            for correction_attempt in range(3):
                corrections_used = correction_attempt
                plan = await planner.plan(ctx)
                current = await self.store.get_node(node.id)
                if current is not None and current.status is NodeStatus.CANCELLED:
                    await self.store.update_run(
                        run.id,
                        status=RunStatus.CANCELLED,
                        outcome=Outcome.FAIL,
                        summary="run cancelled",
                        error="run cancelled by user",
                        retry_recommended=False,
                    )
                    return []
                await self._emit("harness.return", project_id, {"run_id": str(run.id), "node_id": str(node.id), "status": "returned", "outcome": "plan", "session_id": plan.session_id, "created": len(plan.nodes)})
                await self._emit_trigger_event(
                    "agent.plan.submitted",
                    project_id=project_id,
                    node_id=node.id,
                    data={"node_id": str(node.id), "kind": "plan", "created": len(plan.nodes)},
                )
                if forbidden_session_id and plan.session_id == forbidden_session_id:
                    raise RuntimeError("provider reused the previous session during a fresh run")
                validation_repo = ctx.project_repo_path or ctx.repo_path
                if validation_repo:
                    catalog = CapabilityCatalog(Path(self.s.data_dir) / "capabilities")
                    plan_payload = plan.model_dump(mode="json")
                    catalog.load_plan_role_capabilities(plan_payload, validation_repo)
                    catalog.validate_plan(
                        plan_payload,
                        validation_repo,
                        planner_capabilities=node.agent.capabilities if node.agent else None,
                    )
                    validate_subgraph_sources(plan, validation_repo)
                contract = self._organization_contract_for_plan(node, plan)
                structural_errors: list[str] = []
                if contract is not None:
                    scale_rank = {
                        OrganizationScale.FOCUSED: 0,
                        OrganizationScale.DELIVERY: 1,
                        OrganizationScale.ORGANIZATION: 2,
                    }
                    if (
                        node.organization_contract is not None
                        and scale_rank[contract.scale]
                        < scale_rank[node.organization_contract.scale]
                    ):
                        structural_errors.append(
                            "organization plan cannot downgrade its existing contract scale"
                        )
                    audit = audit_plan(contract, plan)
                    last_structural_audit = audit
                    structural_errors.extend(audit.errors)
                if structural_errors:
                    deterministic = PlanAuditResult(
                        decision=PlanAuditDecision.REJECT,
                        summary="Deterministic plan audit rejected the proposal.",
                        findings=list(structural_errors),
                        required_changes=list(structural_errors),
                    )
                    last_semantic_audit = deterministic
                    await self._record_plan_audit(
                        node.id,
                        structural=last_structural_audit,
                        semantic=deterministic,
                        correction_count=correction_attempt,
                    )
                    if correction_attempt >= 2:
                        raise RuntimeError(
                            "deterministic organization audit rejected the plan "
                            "after two correction attempts: "
                            + "; ".join(structural_errors)
                        )
                    ctx.review_feedback = (
                        "Deterministic organization audit rejected the previous plan. "
                        "Correct these structural errors before resubmitting: "
                        + "; ".join(structural_errors)
                    )
                    refreshed = await self.store.get_node(node.id)
                    if refreshed is not None:
                        ctx.node = refreshed
                    continue
                if contract is None:
                    break
                semantic = await self._run_semantic_plan_audit(
                    node, contract, plan, ctx
                ) if node.id == project_id or contract.scale.value != "focused" else None
                if semantic is None:
                    last_semantic_audit = None
                    await self._record_plan_audit(
                        node.id,
                        structural=last_structural_audit,
                        correction_count=correction_attempt,
                    )
                    break
                last_semantic_audit = semantic
                await self._record_plan_audit(
                    node.id,
                    structural=last_structural_audit,
                    semantic=semantic,
                    correction_count=correction_attempt,
                )
                if semantic.decision is PlanAuditDecision.APPROVE:
                    break
                if correction_attempt >= 2:
                    raise RuntimeError(
                        "semantic organization audit rejected the plan after two correction attempts: "
                        + "; ".join(semantic.required_changes or semantic.findings)
                    )
                ctx.review_feedback = (
                    "Independent semantic audit rejected the previous plan. "
                    + semantic.summary
                    + " Required changes: "
                    + "; ".join(semantic.required_changes or semantic.findings)
                )
                refreshed = await self.store.get_node(node.id)
                if refreshed is not None:
                    ctx.node = refreshed
            if plan is None:
                raise RuntimeError("planner returned no plan")
            plan = await self._ensure_plan_source(node, plan)
            created = await self.store.apply_plan(
                node,
                plan,
                enforce_organization_audit=True,
            )
            # Store.apply_plan records the deterministic structural audit. The
            # second write preserves the semantic decision and correction
            # count using the fresh persisted node rather than the planner's
            # stale in-memory snapshot.
            await self._record_plan_audit(
                node.id,
                structural=last_structural_audit,
                semantic=last_semantic_audit,
                correction_count=corrections_used,
            )
            submitted = await self.store.add_artifacts(
                node.id,
                [_plan_submission_artifact(plan)],
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
            applied_parent = await self.store.get_node(node.id) or node
            await self._emit("plan.applied", project_id, {"parent": _dump(applied_parent), "created": len(created)})
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            await self._ensure_handoff_watcher(
                node.id, project_id, ctx.project_repo_path or ctx.repo_path
            )
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
        except ControlOperationUnavailable as error:
            # A control-plane audit could not return a decision. Keep the
            # planner node runnable so a later scheduler tick can retry it;
            # this is not evidence that the proposed organization failed.
            await self.store.update_run(
                run.id,
                status=RunStatus.COMPLETE,
                outcome=Outcome.BLOCK,
                summary="control audit unavailable; retry pending",
                error=sanitize_control_text(error),
                retry_recommended=True,
            )
            await self.store.set_status(node.id, NodeStatus.RUNNABLE)
            await self._emit(
                "organization.audit_control_failed",
                project_id,
                {"node_id": str(node.id), "reason": sanitize_control_text(error), "retryable": True},
            )
            return []
        except Exception as error:
            current = await self.store.get_node(node.id)
            if current is not None and current.status is NodeStatus.CANCELLED:
                await self.store.update_run(
                    run.id,
                    status=RunStatus.CANCELLED,
                    outcome=Outcome.FAIL,
                    summary="run cancelled",
                    error="run cancelled by user",
                    retry_recommended=False,
                )
                return []
            await self._emit("application.error", project_id, {"run_id": str(run.id), "node_id": str(node.id), "phase": "planner", "error": str(error)})
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

    def _planner_for(self, node: Node):
        """Resolve planning from the node's explicit harness contract.

        Test mode can intentionally serve mock projects alongside real-data
        projects. The workspace default selects the fallback planner only; it
        must never turn a node explicitly configured for a real harness into a
        mock plan.
        """
        agent = node.agent
        if agent is not None:
            harness = agent.harness.value
            if harness == HarnessKind.MOCK.value:
                return self.registry.get_planner("mock") or self.registry.planner
            if harness in REAL_HARNESSES:
                return self.registry.get_planner("real") or self.registry.planner
        return self.registry.planner

    async def _run_worker(
        self,
        node: Node,
        project_id: uuid.UUID,
        *,
        forbidden_session_id: str | None = None,
    ) -> None:
        # Deterministic is an in-process unit-test adapter. It deliberately
        # does not overload the Mock process harness even when the test node
        # carries a Mock agent configuration for schema compatibility.
        worker_key = (
            "deterministic"
            if node.executor == "deterministic"
            else node.agent.harness.value
            if node.agent and node.executor != PLANNER_EXECUTOR
            else node.executor
        )
        # A node's agent selection is an execution contract. Never substitute
        # the workspace default when that harness is missing: OpenCode must
        # launch OpenCode, not silently become Codex (or a test adapter).
        worker = self.registry.get(worker_key)
        if worker is None:
            await self._mark_failed(node, f"no worker registered for executor '{node.executor}'")
            return
        self._recovered_active_node_ids.discard(node.id)
        self._recovered_run_ids.pop(node.id, None)
        prior_runs = await self.store.get_runs(node.id)
        run = await self.store.create_run(node, worker.name, len(prior_runs) + 1)
        ctx = await self._build_context(node, run_id=str(run.id))
        ctx.forbidden_session_id = forbidden_session_id
        ctx.attempt = run.attempt
        await self.store.set_status(node.id, NodeStatus.RUNNING)
        await self._emit("run.created", project_id, _dump(run))

        # Stop can arrive immediately after the run record makes the node
        # visible as RUNNING, before this coroutine reaches the harness call.
        # Do not launch a process for a run that has already been cancelled.
        current = await self.store.get_node(node.id)
        if current is None or current.status is NodeStatus.CANCELLED:
            await self.store.update_run(
                run.id,
                status=RunStatus.CANCELLED,
                outcome=Outcome.FAIL,
                summary="run cancelled",
                error="run cancelled before harness launch",
            )
            await self._finish_provider_terminal(node.id, project_id)
            return

        capability_launch = (
            self._prepare_capabilities(node.agent, ctx.repo_path, node.id)
            if node.agent and node.agent.harness in REAL_HARNESSES
            else CapabilityLaunch()
        )

        await self._emit("harness.launch", project_id, {
            "run_id": str(run.id), "node_id": str(node.id), "harness": node.agent.harness.value if node.agent else node.executor,
            "model": node.agent.model if node.agent else None, "reasoning": node.agent.reasoning.value if node.agent else None,
            "session_id": node.agent.session_id if node.agent else None, "attempt": run.attempt,
            "timeout_seconds": ctx.timeout_seconds, "purpose": "execute", "repo_path": ctx.repo_path,
            "flags": self._launch_flags(node, resume=bool(node.agent and node.agent.session_id)),
            "role": "setup" if node.id == project_id else (node.agent.type_id.value if node.agent else None),
            "capability_skills": list(capability_launch.skill_names),
        })

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
            # Short-lived process transports detach before returning from the
            # worker, but their completed PTY remains in the transport until
            # release(). Publish the attempt outcome only after that release
            # so terminal/session state cannot lag a COMPLETE verification or
            # trigger event. A live Herdr/Codex pane is unaffected: release
            # evicts ended control sessions and never closes an active pane.
            self.terminal.release(node.id)
            await self._emit("harness.return", project_id, {"run_id": str(run.id), "node_id": str(node.id), "status": "returned", "outcome": result.outcome.value, "session_id": result.session_id, "summary": result.summary, "error": result.error, "usage": _dump(result.usage)})
            if forbidden_session_id and result.session_id == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
            await self._emit_trigger_event(
                "agent.submitted",
                project_id=project_id,
                node_id=node.id,
                data={
                    "node_id": str(node.id),
                    "outcome": result.outcome.value,
                    "summary": result.summary,
                    "kind": "verification" if result.verification is not None else "result",
                },
            )
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
            current = await self.store.get_node(node.id)
            if current is not None and current.status is NodeStatus.CANCELLED:
                await self.store.update_run(
                    run.id,
                    status=RunStatus.CANCELLED,
                    outcome=Outcome.FAIL,
                    summary="run cancelled",
                    error="run cancelled by user",
                    retry_recommended=False,
                )
                await self._finish_provider_terminal(node.id, project_id)
                return
            logger.exception("worker failed for node %s", node.id)
            await self._emit("application.error", project_id, {"run_id": str(run.id), "node_id": str(node.id), "phase": "worker", "error": str(e)})
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
        current = await self.store.get_node(node.id)
        if current is not None and current.status is NodeStatus.CANCELLED:
            # Stop is a terminal user decision. A provider can return a result
            # while its terminal is shutting down, but it must not mutate the
            # graph, artifacts, or final run outcome after that decision.
            await self.store.update_run(
                run.id,
                status=RunStatus.CANCELLED,
                outcome=Outcome.FAIL,
                summary="run cancelled",
                error="run cancelled by user",
                retry_recommended=False,
            )
            await self._emit("node.updated", project_id, _dump(current))
            return
        if result.outcome in {Outcome.COMPLETE, Outcome.EXPAND}:
            try:
                await self._commit_workspace_result(node)
            except WorkspaceError as error:
                await self.store.update_run(
                    run.id,
                    status=RunStatus.FAILED,
                    outcome=Outcome.FAIL,
                    summary="workspace merge failed",
                    error=str(error),
                    retry_recommended=False,
                )
                await self.store.set_status(node.id, NodeStatus.FAILED)
                await self._request_reviews_for_node(node.id, "workspace_merge_failed")
                await self._emit("organization.workspace_failed", project_id, {
                    "node_id": str(node.id),
                    "error": str(error),
                })
                return
        await self._persist_result_materials(node.id, project_id, result)
        if result.outcome is Outcome.COMPLETE and node.acceptance_criteria:
            evidence_items = [
                *result.evidence,
                *(
                    result.verification.evidence
                    if result.verification is not None
                    else []
                ),
            ]
            evidence_by_id = {item.criterion_id: item for item in evidence_items}
            missing = [
                criterion.id
                for criterion in node.acceptance_criteria
                if criterion.id not in evidence_by_id
            ]
            failed = [
                item.criterion_id
                for item in evidence_items
                if item.status is EvidenceStatus.FAIL
            ]
            unverified = [
                item.criterion_id
                for item in evidence_items
                if item.status is EvidenceStatus.UNVERIFIED
            ]
            unreferenced = [
                item.criterion_id
                for item in evidence_items
                if not item.refs
            ]
            if missing or failed or unverified or unreferenced:
                reason_parts = []
                if missing:
                    reason_parts.append("missing evidence for " + ", ".join(missing))
                if failed:
                    reason_parts.append("failed criteria: " + ", ".join(failed))
                if unverified:
                    reason_parts.append(
                        "unverified criteria: " + ", ".join(unverified)
                    )
                if unreferenced:
                    reason_parts.append(
                        "evidence has no inspectable refs for "
                        + ", ".join(unreferenced)
                    )
                await self.store.update_run(
                    run.id,
                    status=RunStatus.FAILED,
                    outcome=Outcome.FAIL,
                    summary="acceptance evidence rejected",
                    error="; ".join(reason_parts),
                    retry_recommended=False,
                )
                await self.store.set_status(node.id, NodeStatus.FAILED)
                await self._request_reviews_for_node(
                    node.id, "acceptance_evidence_failed"
                )
                await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
                return
        if result.verification is not None:
            if (
                result.outcome is Outcome.COMPLETE
                and result.verification.decision is VerificationDecision.APPROVE
            ):
                await self._accept_consumed_handoffs(node, result)
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
            await self._accept_consumed_handoffs(node, result)
        elif result.outcome == Outcome.EXPAND:
            plan = result.children or PlanResult(nodes=[])
            repo_path = await self._project_repo(project_id)
            if repo_path:
                catalog = CapabilityCatalog(Path(self.s.data_dir) / "capabilities")
                plan_payload = plan.model_dump(mode="json")
                catalog.load_plan_role_capabilities(plan_payload, repo_path)
                catalog.validate_plan(
                    plan_payload,
                    repo_path,
                    planner_capabilities=node.agent.capabilities if node.agent else None,
                )
                validate_subgraph_sources(plan, repo_path)
            contract = self._organization_contract_for_plan(node, plan)
            structural_audit = None
            semantic_audit = None
            if contract is not None:
                structural_audit = audit_plan(contract, plan)
                if not structural_audit.accepted:
                    rejection = PlanAuditResult(
                        decision=PlanAuditDecision.REJECT,
                        summary="Deterministic plan audit rejected the proposal.",
                        findings=list(structural_audit.errors),
                        required_changes=list(structural_audit.errors),
                    )
                    await self._record_plan_audit(
                        node.id, structural=structural_audit, semantic=rejection
                    )
                    await self.store.update_run(
                        run.id,
                        status=RunStatus.FAILED,
                        outcome=Outcome.FAIL,
                        summary="organization plan rejected",
                        error="; ".join(structural_audit.errors),
                        retry_recommended=False,
                    )
                    await self.store.set_status(node.id, NodeStatus.FAILED)
                    await self._request_reviews_for_node(
                        node.id, "organization_plan_rejected"
                    )
                    await self._emit("node.updated", project_id, _dump(await self.store.get_node(node.id)))
                    return
                if node.id == project_id or contract.scale.value != "focused":
                    try:
                        semantic_audit = await self._run_semantic_plan_audit(
                            node, contract, plan
                        )
                    except ControlOperationUnavailable as error:
                        await self.store.update_run(
                            run.id,
                            status=RunStatus.COMPLETE,
                            outcome=Outcome.EXPAND,
                            summary="control audit unavailable; retry pending",
                            error=sanitize_control_text(error),
                            retry_recommended=True,
                        )
                        await self.store.set_status(node.id, NodeStatus.RUNNABLE)
                        await self._emit(
                            "organization.audit_control_failed",
                            project_id,
                            {"node_id": str(node.id), "reason": sanitize_control_text(error), "retryable": True},
                        )
                        return
                    if semantic_audit is not None:
                        await self._record_plan_audit(
                            node.id,
                            structural=structural_audit,
                            semantic=semantic_audit,
                        )
                    if semantic_audit is not None and semantic_audit.decision is PlanAuditDecision.REJECT:
                        await self.store.update_run(
                            run.id,
                            status=RunStatus.FAILED,
                            outcome=Outcome.FAIL,
                            summary="semantic organization plan rejected",
                            error="; ".join(
                                semantic_audit.required_changes or semantic_audit.findings
                            ),
                            retry_recommended=False,
                        )
                        await self.store.set_status(node.id, NodeStatus.FAILED)
                        await self._request_reviews_for_node(
                            node.id, "semantic_plan_rejected"
                        )
                        await self._emit(
                            "node.updated",
                            project_id,
                            _dump(await self.store.get_node(node.id)),
                        )
                        return
                else:
                    await self._record_plan_audit(
                        node.id, structural=structural_audit
                    )
            plan = await self._ensure_plan_source(node, plan)
            created = await self.store.apply_plan(
                node,
                plan,
                enforce_organization_audit=True,
            )
            await self._record_plan_audit(
                node.id,
                structural=structural_audit,
                semantic=semantic_audit,
            )
            await self.store.update_run(
                run.id, status=RunStatus.COMPLETE, outcome=Outcome.EXPAND,
                summary=result.summary, logs=result.executor_notes or result.summary or "",
                usage=result.usage, session_id=result.session_id,
            )
            await self._remember_session(node, result.session_id)
            for c in created:
                await self._emit("node.created", project_id, _dump(c))
            applied_parent = await self.store.get_node(node.id) or node
            await self._emit("plan.applied", project_id, {"parent": _dump(applied_parent), "created": len(created)})

        elif result.outcome == Outcome.BLOCK:
            node = await self.store.get_node(node.id)
            if node is not None:
                existing = {i.id for i in node.required_inputs}
                for mi in result.missing_inputs:
                    if mi.id not in existing:
                        node.required_inputs.append(mi)
                        existing.add(mi.id)
                await self.store.set_required_inputs(
                    node.id, node.required_inputs, merge=True
                )
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
        latest_outcome_node = await self.store.get_node(node.id)
        if latest_outcome_node is not None and latest_outcome_node.status in {
            NodeStatus.FAILED,
            NodeStatus.BLOCKED,
        }:
            await self._request_reviews_for_node(
                node.id,
                "child_failed" if latest_outcome_node.status is NodeStatus.FAILED else "child_blocked",
            )
        fresh_node = await self.store.get_node(node.id)
        await self._ensure_handoff_watcher(
            node.id,
            project_id,
            await self._project_repo(project_id),
        )
        self.wake()

    async def _accept_consumed_handoffs(
        self, node: Node, result: WorkerResult
    ) -> None:
        """Derive handoff acceptance from a successful consumer turn.

        Producers only make a matching artifact AVAILABLE. A consumer that
        completes successfully and cites inspectable evidence is the narrow
        authority that can move that input to ACCEPTED.
        """
        if result.outcome is not Outcome.COMPLETE:
            return
        evidence = [
            *result.evidence,
            *(
                result.verification.evidence
                if result.verification is not None
                else []
            ),
        ]
        evidence_refs = list(
            dict.fromkeys(
                [
                    *(
                        ref
                        for item in evidence
                        if item.status is EvidenceStatus.PASS
                        for ref in item.refs
                    ),
                    *(
                        ref
                        for artifact in result.artifacts
                        for ref in artifact.evidence_refs
                    ),
                ]
            )
        )
        for handoff in await self.store.list_handoffs(
            node.project_id, node_id=node.id
        ):
            if (
                handoff.consumer_node_id != node.id
                or handoff.status is not HandoffStatus.AVAILABLE
                or (
                    handoff.contract.evidence_required
                    and not evidence_refs
                )
            ):
                continue
            await self.store.update_handoff(
                handoff.id,
                status=HandoffStatus.ACCEPTED,
                artifact_id=handoff.artifact_id,
                evidence_refs=evidence_refs,
            )

    async def _commit_workspace_result(self, node: Node) -> None:
        """Commit one isolated worker without mutating the canonical branch."""
        node = await self.store.get_node(node.id) or node
        root = await self.store.get_node(node.project_id)
        if (
            root is None
            or root.run_policy is None
            or root.run_policy.workspace_isolation.value != "worktree"
            or not node.workspace_path
        ):
            return
        commit = await self.workspaces.commit(node.workspace_path, node.id)
        if commit is None:
            return
        await self.store.set_workspace_commit(node.id, commit)

    async def _persist_result_materials(
        self, node_id: uuid.UUID, project_id: uuid.UUID, result: WorkerResult
    ) -> list[Artifact]:
        """Persist concise output identities without retaining terminal transcripts."""
        linked = await self.store.add_document_refs(node_id, result.document_refs)
        await self.store.add_subgraph_refs(node_id, result.subgraph_refs)
        evidence_items = [
            *result.evidence,
            *(
                result.verification.evidence
                if result.verification is not None
                else []
            ),
        ]
        evidence_specs = [
            ArtifactSpec(
                kind=ArtifactKind.EVIDENCE,
                name=f"evidence-{item.criterion_id}",
                content=item.model_dump(mode="json"),
                evidence_refs=list(item.refs),
            )
            for item in evidence_items
        ]
        explicit = await self.store.add_artifacts(
            node_id,
            [*result.artifacts, *evidence_specs],
        )
        for artifact in [*linked, *explicit]:
            await self._emit("artifact.created", project_id, _dump(artifact))
        return [*linked, *explicit]

    async def _ensure_plan_source(self, node: Node, plan: PlanResult) -> PlanResult:
        """Give every provider-created planning handoff an editable source file.

        CLI submissions already carry their source link and remain untouched.
        Provider-returned inline plans get a unique project-relative source so
        a later correction can edit that file and submit it again.
        """
        repo = await self._project_repo(node.project_id)
        if not repo:
            return plan
        # Empty plans are intentional: a planner may only submit documents or
        # acknowledge an already-planned boundary. Do not create a misleading
        # empty graph source for that handoff. A source is created by default
        # when this turn actually introduces graph nodes.
        if not plan.nodes:
            return plan
        if plan.subgraph_refs:
            return plan
        relative = Path(".turn") / "graphs" / f"{node.id}-{uuid.uuid4().hex}.json"
        target = Path(repo).resolve() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = plan.model_dump(mode="json")
        await asyncio.to_thread(target.write_text, json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
        return plan.model_copy(update={
            "subgraph_refs": [SubgraphRef(ref=relative.as_posix(), managed=True)],
        })

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
        current = await self.store.complete_verification(
            current.id,
            decision,
            session_id=result.session_id,
            status=(
                NodeStatus.COMPLETE
                if decision.decision is VerificationDecision.APPROVE
                # Keep a rejected verifier active while the runner delivers
                # the correction and resets the affected sequence. Exposing
                # PENDING before the target is RUNNABLE lets the scheduler or
                # an API client observe a half-applied rejection and can race
                # the reset (especially with process-backed mock reconnects).
                else NodeStatus.RUNNING
            ),
        ) or current
        await self._emit("verification.completed", project_id, {
            "node_id": str(current.id),
            "decision": decision.decision.value,
            "target_node_id": str(decision.target_node_id) if decision.target_node_id else None,
        })
        if decision.decision is VerificationDecision.APPROVE:
            nodes, edges, _ = await self.store.get_workgraph(project_id)
            target = rejection_target(current, decision, GraphWalker(nodes, edges).indexes)
            if target is not None:
                await self._emit("verification.outcome", project_id, {
                    "node_id": str(target.id),
                    "reviewer_node_id": str(current.id),
                    "decision": decision.decision.value,
                })
        # Verification completion is useful as a general agent-action event,
        # while acceptance/rejection are the precise workflow events needed
        # for routing. A loop can therefore subscribe to acceptance without
        # also restarting on the rejected first pass.
        await self._emit_trigger_event(
            "verification.accepted"
            if decision.decision is VerificationDecision.APPROVE
            else "verification.rejected",
            project_id=project_id,
            node_id=current.id,
            data={
                "node_id": str(current.id),
                "decision": decision.decision.value,
                "summary": decision.summary,
                "target_node_id": (
                    str(decision.target_node_id)
                    if decision.target_node_id
                    else None
                ),
            },
            source=EventSource.AGENT_ACTION,
        )

        if decision.decision is VerificationDecision.REJECT:
            nodes, edges, _ = await self.store.get_workgraph(project_id)
            walker = GraphWalker(nodes, edges)
            target = rejection_target(current, decision, walker.indexes)
            if target is None:
                raise RuntimeError(
                    "rejection requires a valid target_node_id when the verifier has multiple preceding stages"
                )
            await self._emit("verification.outcome", project_id, {
                "node_id": str(target.id),
                "reviewer_node_id": str(current.id),
                "decision": decision.decision.value,
            })
            correction = self._rejection_message(reviewer, decision)
            await self._notify_rejection(target, reviewer, decision, message=correction)
            # A reviewer may be the active member of a manual Step barrier.
            # Rejection moves that reviewer back behind the corrected target,
            # so the old barrier can never settle. The next Step must be
            # allowed to select the repaired target, and then the reviewer
            # again after that target completes.
            self._manual_stages.pop(project_id, None)
            # A rejection invalidates the target, the review node, and every
            # dependent result reachable from either. The graph replays them
            # in sequence order; the target is runnable immediately and the
            # reviewer becomes runnable again after its predecessors settle.
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
                    pending.extend(walker.indexes.successors.get(dependent_id, []))
            for item in invalidated:
                if item.id == target.id or item.id == reviewer.id or item.status != NodeStatus.RUNNING:
                    item.status = NodeStatus.RUNNABLE if item.id == target.id else NodeStatus.PENDING
                    # Keep the target's provider session. The rejection was
                    # injected into that active conversation, so the next
                    # attempt must continue with the same context rather than
                    # starting a new conversation from zero.
                    updated = await self.store.reset_node_after_rejection(
                        item.id,
                        item.status,
                        agent_state="correction_requested" if item.id == target.id else None,
                        agent_message=correction if item.id == target.id else None,
                    )
                    await self._emit("node.updated", project_id, _dump(updated or item))
            await self._request_reviews_for_node(reviewer.id, "verification_rejected")
        await self._emit("node.updated", project_id, _dump(await self.store.get_node(reviewer.id)))
        await self._ensure_handoff_watcher(
            reviewer.id,
            project_id,
            await self._project_repo(project_id),
        )
        self.wake()

    @staticmethod
    def _rejection_message(reviewer: Node, decision) -> str:
        """Render a correction envelope for retained and fresh resumes."""
        return "\n".join([
            "TURN VERIFICATION REJECTED",
            f"Reviewer: {reviewer.objective}",
            f"Summary: {decision.summary}",
            *[f"- {item}" for item in decision.findings],
            "Required changes:",
            *[f"- {item}" for item in decision.required_changes],
            "Continue the responsible node through Turn after addressing these findings; the project execution mode controls when the refinement runs.",
        ])

    async def _notify_rejection(
        self,
        target: Node,
        reviewer: Node,
        decision,
        *,
        message: str | None = None,
    ) -> None:
        """Deliver feedback to the selected node's scoped conversation."""
        repo = await self._project_repo(target.project_id)
        if not repo:
            return
        message = message or self._rejection_message(reviewer, decision)
        # The process-level mock has no durable shell pane when tests inject
        # a LocalPtyTransport, but its provider session is still meaningful:
        # launch a fresh mock process with the retained session id so the
        # rejection path exercises the same command boundary as native
        # harnesses.
        backend_name = getattr(self.terminal, "backend_name", "local")
        process_mock = (
            target.executor == "mock"
            and target.agent is not None
            and target.agent.harness is HarnessKind.MOCK
            and backend_name != "mock"
        )
        native_provider = (
            target.agent is not None
            and target.agent.harness is not HarnessKind.MOCK
        )
        if process_mock or (native_provider and self.terminal.supports_inject):
            if not await self.reconnect(target.id, prompt=message):
                raise RuntimeError(
                    f"could not launch rejection follow-up for node {target.id}"
                )
            return
        # Deterministic is an in-memory provider and has no persisted
        # conversation to reopen. Preserve the byte-level fallback used by
        # its tests even when the replacement terminal advertises injection.
        # Deterministic non-Herdr transports used by tests do not expose a
        # process table. Preserve their byte-level assertion surface; served
        # runs always use the branch above.
        if not hasattr(self.terminal, "ensure_session"):
            # A one-shot local PTY has already exited with its handoff. There
            # is no durable conversation to inject into; the rejection is
            # still persisted in the graph and the next run receives the
            # normal Turn context.
            return
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
        decisions = await self._review_organizations(root.project_id)
        if any(decision.replan for decision in decisions):
            return
        if any(decision.phase.value == "BLOCKED" for decision in decisions):
            await self.store.set_status(root.id, NodeStatus.BLOCKED)
            await self._emit("organization.blocked", root.project_id, {
                "project_id": str(root.project_id),
                "reasons": [decision.reason for decision in decisions if decision.phase.value == "BLOCKED"],
            })
            return
        if any(
            decision.decision is not ManagerDecision.ACCEPT
            for decision in decisions
        ):
            # A manager can return CONTINUE without an immediate replan while
            # a safe-point frontier is still executing. Settled descendants
            # are not, by themselves, proof that the charter is accepted.
            return
        if (
            root.organization_contract is not None
            and root.organization_contract.scale.value != "focused"
            and root.manager_phase is not ManagerPhase.ACCEPTED
        ):
            return
        await self._merge_accepted_root_output(root)
        if root.status != NodeStatus.COMPLETE:
            await self.store.set_status(root.id, NodeStatus.COMPLETE)
            await self._emit(
                "node.updated", root.project_id, _dump(await self.store.get_node(root.id))
            )
            await self._emit_trigger_event(
                "project.completed",
                project_id=root.project_id,
                node_id=root.id,
                data={"project_id": str(root.project_id), "node_id": str(root.id)},
                source=EventSource.TRANSITION,
            )

    async def _merge_accepted_root_output(self, root: Node) -> None:
        """Merge only the accepted project output into the user's branch."""
        fresh_root = await self.store.get_node(root.id) or root
        policy = fresh_root.run_policy
        if policy is None or policy.workspace_isolation.value != "worktree":
            return
        nodes, edges, _ = await self.store.get_workgraph(root.project_id)
        walker = GraphWalker(nodes, edges)
        descendants = walker.descendants(root.id)
        candidates = [
            node
            for node in descendants
            if node.workspace_commit
            and node.status is NodeStatus.COMPLETE
            and node.executor == "integrator"
            and node.parent_id == root.id
        ]
        if not candidates:
            candidates = [
                node
                for node in descendants
                if node.workspace_commit
                and node.status is NodeStatus.COMPLETE
                and node.executor == "integrator"
            ]
        if not candidates:
            candidates = [
                node
                for node in descendants
                if node.workspace_commit and node.status is NodeStatus.COMPLETE
            ]
        commit = fresh_root.workspace_commit or (
            candidates[0].workspace_commit if candidates else None
        )
        if not commit:
            return
        repo = await self._project_repo(root.project_id)
        if not repo:
            raise WorkspaceError("project root is unavailable for accepted output merge")
        await self.workspaces.merge(repo, commit, root.id)
        if fresh_root.workspace_commit != commit:
            await self.store.set_workspace_commit(root.id, commit)

    async def _review_organizations(
        self,
        project_id: uuid.UUID,
        *,
        boundaries: list[Node] | None = None,
    ):
        """Run the durable manager review before a project is accepted."""
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if boundaries is None:
            boundaries = [
                node for node in nodes
                if node.executor == PLANNER_EXECUTOR
                and node.organization_contract is not None
                and node.organization_contract.scale.value != "focused"
                and node.status in {NodeStatus.EXPANDED, NodeStatus.COMPLETE}
                and node.manager_phase not in {ManagerPhase.ACCEPTED, ManagerPhase.BLOCKED}
            ]
        else:
            current = {node.id: node for node in nodes}
            boundaries = [
                current[node.id]
                for node in boundaries
                if node.id in current
            ]
        boundaries.sort(
            key=lambda node: len(GraphWalker(nodes, edges).ancestors(node.id)),
            reverse=True,
        )
        decisions = []
        for boundary in boundaries:
            if boundary.manager_phase not in {
                ManagerPhase.ACCEPTED,
                ManagerPhase.BLOCKED,
            }:
                await self.organization_manager.request_review(
                    self.store, boundary.id, "frontier_settled"
                )
            reviewer = self.manager_reviewer
            if reviewer is None and self.provider_reviews_enabled:
                reviewer = self._provider_manager_review
            if reviewer is not None:
                await self.store.set_manager_state(
                    boundary.id,
                    phase=ManagerPhase.REVIEWING,
                    iteration=boundary.manager_iteration + 1,
                    reasons=boundary.manager_review_reasons or ["frontier_settled"],
                )
                snapshot = await self.organization_manager.snapshot(
                    self.store, boundary.id
                )
                try:
                    manager_result = await reviewer(snapshot)
                    if manager_result.plan is not None:
                        contract = boundary.organization_contract
                        if contract is not None:
                            plan_audit = audit_plan(contract, manager_result.plan)
                            if not plan_audit.accepted:
                                raise RuntimeError(
                                    "organization manager plan rejected: "
                                    + "; ".join(plan_audit.errors)
                                )
                            if contract.scale.value != "focused":
                                semantic = await self._run_semantic_plan_audit(
                                    boundary,
                                    contract,
                                    manager_result.plan,
                                )
                                if (
                                    semantic is not None
                                    and semantic.decision is PlanAuditDecision.REJECT
                                ):
                                    raise RuntimeError(
                                        "semantic organization manager plan rejected: "
                                        + "; ".join(
                                            semantic.required_changes
                                            or semantic.findings
                                        )
                                    )
                    decision = await self.organization_manager.apply_result(
                        self.store, boundary.id, manager_result
                    )
                except Exception as error:
                    reason = "manager review unavailable: " + sanitize_control_text(error)
                    await self.organization_manager.request_review(
                        self.store,
                        boundary.id,
                        reason,
                    )
                    await self.store.set_manager_state(
                        boundary.id,
                        phase=ManagerPhase.REVIEW_PENDING,
                        reasons=[reason],
                    )
                    await self._emit("organization.review_control_failed", project_id, {
                        "node_id": str(boundary.id),
                        "reason": reason,
                        "retryable": True,
                    })
                    # A failed control operation is not a manager decision and
                    # must not strand a healthy frontier in BLOCKED/FAILED.
                    continue
            else:
                decision = await self.organization_manager.review(
                    self.store, boundary.id
                )
            if decision is None:
                continue
            decisions.append(decision)
            if decision.decision is ManagerDecision.ACCEPT:
                await self._expose_boundary_output_commit(boundary.id)
            await self._emit("organization.reviewed", project_id, {
                "node_id": str(boundary.id),
                "phase": decision.phase.value,
                "replan": decision.replan,
                "reason": decision.reason,
            })
            if decision.replan and reviewer is None:
                try:
                    current = await self.store.get_node(boundary.id)
                    if current is None:
                        break
                    # A manager review appends the next wave to the existing
                    # boundary. Completed nodes remain history and evidence;
                    # only the retained planner session is re-engaged.
                    await self._plan_node(current, project_id)
                except Exception as error:
                    current = await self.store.get_node(boundary.id)
                    if current is not None:
                        review = current.organization_review or OrganizationReview()
                        review.phase = OrganizationPhase.BLOCKED
                        review.replan_requested = False
                        review.last_reason = f"manager replan failed: {error}"
                        await self.store.set_organization_review(boundary.id, review)
                break
        return decisions

    async def _expose_boundary_output_commit(self, boundary_id: uuid.UUID) -> None:
        """Expose the accepted nested integrator commit to its parent edge."""
        boundary = await self.store.get_node(boundary_id)
        if boundary is None:
            return
        nodes, edges, _ = await self.store.get_workgraph(boundary.project_id)
        walker = GraphWalker(nodes, edges)
        descendants = walker.descendants(boundary_id)
        candidates = [
            node
            for node in descendants
            if node.workspace_commit
            and node.status is NodeStatus.COMPLETE
            and node.executor == "integrator"
        ]
        if not candidates:
            candidates = [
                node
                for node in descendants
                if node.workspace_commit and node.status is NodeStatus.COMPLETE
            ]
        if not candidates:
            return
        candidates.sort(key=lambda node: walker.depth(node.id), reverse=True)
        await self.store.set_workspace_commit(
            boundary.id,
            candidates[0].workspace_commit,
        )

    # -- user actions ----------------------------------------------------

    async def provide_input(self, node_id: uuid.UUID, input_id: str, value: str) -> None:
        node = await self.store.satisfy_input(node_id, input_id, value)
        if node is not None:
            # re-evaluate: if all inputs satisfied, it becomes runnable
            still_missing = [i for i in node.required_inputs if i.satisfied_by is None]
            if not still_missing:
                if (
                    node.executor == PLANNER_EXECUTOR
                    and node.manager_phase is ManagerPhase.BLOCKED
                    and node.organization_contract is not None
                ):
                    review = node.organization_review or OrganizationReview()
                    review.phase = OrganizationPhase.REVIEW
                    review.replan_requested = True
                    review.last_reason = "required manager input satisfied"
                    await self.store.set_organization_review(node.id, review)
                    descendants = await self.store.descendants(node.id)
                    work_items = await self.store.list_work_items(
                        node.project_id, organization_id=node.id
                    )
                    resume_status = (
                        NodeStatus.EXPANDED
                        if descendants or work_items
                        else NodeStatus.RUNNABLE
                    )
                    await self.store.set_manager_state(
                        node.id,
                        phase=ManagerPhase.REVIEW_PENDING,
                        reasons=["required manager input satisfied"],
                    )
                    await self.store.set_status(node.id, resume_status)
                else:
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
        self, node_id: uuid.UUID, *, fresh_session: bool = False, force: bool = False
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
            self._running_projects[node_id] = node.project_id
            claimed = True
        try:
            await self._stop_handoff_watcher(node_id)
            removed = await self._remove_descendants_before_replan(node_id, force=force)
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
                    self.sessions.retire_fresh_session(node_id, forbidden_session_id)
                node = await self.store.get_node(node_id) or node
            await self.store.clear_generated_artifacts(node_id)
            node.subgraph_refs = []
            await self.store.replace_subgraph_refs(node_id, [])
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

    async def resume_organization_review(self, node_id: uuid.UUID) -> Node:
        """Reopen a materialized manager boundary after a provider failure.

        This is deliberately different from ``retry``: retrying a planner
        clears its provider session and starts a fresh planning run, while a
        review failure must preserve the already-audited graph and its
        evidence. The operation only changes the control-plane state needed
        for the scheduler to revisit the persisted frontier.
        """
        node = await self.store.get_node(node_id)
        if node is None:
            raise ValueError("node not found")
        if (
            node.executor != PLANNER_EXECUTOR
            or node.organization_contract is None
            or node.organization_contract.scale is OrganizationScale.FOCUSED
        ):
            raise ValueError("node is not a material organization boundary")
        if self.generation_active(node_id):
            raise RuntimeError("organization provider is still active")
        descendants = await self.store.descendants(node_id)
        work_items = await self.store.list_work_items(
            node.project_id, organization_id=node_id
        )
        if not descendants and not work_items:
            raise ValueError("organization has no materialized frontier to resume")

        # A failed review may leave the provider TUI in a durable Herdr pane.
        # Closing that stale writer is safe here because generation_active()
        # already proved that Turn does not own a live provider task.
        await self.close_provider_terminal(node_id)
        review = node.organization_review or OrganizationReview()
        review.phase = OrganizationPhase.EXECUTE_FRONTIER
        review.replan_requested = False
        review.last_reason = "organization review resumed after provider failure"
        await self.store.set_organization_review(node_id, review)
        await self.store.set_manager_state(
            node_id,
            phase=ManagerPhase.REVIEW_PENDING,
            reasons=[review.last_reason],
        )
        resumed = await self.store.set_status(node_id, NodeStatus.EXPANDED)
        if resumed is None:
            raise ValueError("node disappeared while resuming organization review")
        await self._emit("node.updated", resumed.project_id, _dump(resumed))
        self.wake()
        return resumed

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
        if agent.harness is HarnessKind.MOCK:
            # The process-level mock is test-only and intentionally does not
            # belong in the real provider command catalog. It still needs an
            # explicit reconnect command so rejection flows exercise the same
            # retained-session lifecycle as native harnesses.
            from turn.workers.mock_harness import mock_harness_script

            command = [mock_harness_script(), "--reconnect", session_id]
            return [*command, prompt] if prompt is not None else command
        launch = self._prepare_capabilities(agent, cwd, node.id)
        return self.harness_commands.reconnect_command(
            agent,
            cwd,
            session_id,
            prompt=prompt,
            mcp_config=launch.claude_config,
            capability_mcp_overrides=launch.codex_overrides,
            skill_paths=list(launch.skill_paths),
        )

    def _prepare_capabilities(self, agent, cwd: str, node_id: object) -> CapabilityLaunch:
        catalog = CapabilityCatalog(Path(self.s.data_dir) / "capabilities")
        packages = [catalog.resolve_project(capability_id, cwd) for capability_id in agent.capabilities]
        adapter = harness_capability_adapter(agent.harness)
        for package in packages:
            adapter.install(package, cwd)
            verification = adapter.verify(package, cwd)
            if not verification.installed:
                raise RuntimeError(
                    f"capability {package.id!r} failed {agent.harness.value} installation verification"
                )
        return adapter.prepare_launch(packages, cwd, node_id)

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
        # A daemon restart can leave the durable provider pane alive while
        # there is no corresponding Turn task in this process. Persisted
        # RUNNING is therefore not sufficient to identify a live generation;
        # only refuse cleanup when this runner still owns an active task.
        if node is None or (node.status == NodeStatus.RUNNING and self.generation_active(node_id)):
            return False
        await self._stop_handoff_watcher(node_id)
        task = self._reconnect_tasks.get(node_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.terminal.close_persistent_session(node_id)
            return True
        if self.terminal.snapshot(node_id).get("active"):
            await self.terminal.stop(node_id)
            await self.terminal.close_persistent_session(node_id)
            return True
        return await self.terminal.close_persistent_session(node_id)

    async def _run_reconnect(self, node: Node, command: list[str], cwd: str, stream) -> None:
        launch = self._prepare_capabilities(node.agent, cwd, node.id) if node.agent else CapabilityLaunch()
        environment = {"TURN_PROJECT_ID": str(node.project_id)}
        if launch.claude_config or launch.pi_mcp_config:
            environment["TURN_AGENT_MCP_CONFIG"] = launch.claude_config or launch.pi_mcp_config or ""
        if launch.opencode_config:
            environment["OPENCODE_CONFIG_CONTENT"] = launch.opencode_config
        if launch.codex_overrides:
            environment["TURN_AGENT_CODEX_MCP_OVERRIDES"] = json.dumps(list(launch.codex_overrides))
        try:
            if self.terminal.supports_inject:
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
        self._recovered_active_node_ids.discard(node_id)
        self._recovered_run_ids.pop(node_id, None)
        await self._stop_handoff_watcher(node_id)
        reconnect = self._reconnect_tasks.get(node_id)
        if reconnect is not None and not reconnect.done():
            # A follow-up runs in a separate reconnect task, but Stop has the
            # same lifecycle meaning as it does for a normal worker: terminate
            # the provider and make the next user action an explicit fresh run.
            await self._mark_cancelled(node)
            await self.terminal.stop(node_id)
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)
            self.wake()
            return
        task = self._running.get(node_id)
        if task is not None and not task.done():
            # Stop the provider before cancelling Turn's awaiter. This makes
            # Stop effective even when the task is inside a native harness
            # call rather than inside the runner's Python bookkeeping.
            await self._mark_cancelled(node)
            await self.terminal.stop(node_id)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current = await self.store.get_node(node_id)
            if current is not None and current.status is NodeStatus.RUNNING:
                await self.store.set_status(node_id, NodeStatus.CANCELLED)
                await self._emit(
                    "node.updated", node.project_id, _dump(await self.store.get_node(node_id))
                )
        else:
            await self.terminal.stop(node_id)
            await self._mark_cancelled(node)
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
                await self._stop_handoff_watcher(target.id)
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
        for node in nodes:
            await self._stop_handoff_watcher(node.id)
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
        return await self.scheduler.step(project_id)

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
        self.scheduler.reserve(node, node.project_id)
        await self._emit("node.updated", node.project_id, _dump(node))
        return node.id

    async def set_mode(self, project_id: uuid.UUID, auto_run: bool) -> None:
        if auto_run:
            self._manual_stages.pop(project_id, None)
        node = await self.store.set_project_mode(project_id, auto_run)
        if node is not None:
            await self._emit("node.updated", project_id, _dump(node))
        self.wake()

    # -- helpers ---------------------------------------------------------

    async def _build_context(self, node: Node, *, run_id: str | None = None) -> NodeExecutionContext:
        ancestry = await self.store.ancestry(node.id)
        resource_refs = []
        for a in ancestry + [node]:
            resource_refs.extend(a.resource_refs)
        resources = await self._resolve_resources(resource_refs)
        graph_nodes, graph_edges, graph_artifacts = await self.store.get_workgraph(
            node.project_id
        )
        walker = GraphWalker(graph_nodes, graph_edges)
        predecessors = walker.predecessors(node.id)
        predecessor_ids = {predecessor.id for predecessor in predecessors}
        predecessor_commits = [
            predecessor.workspace_commit
            for predecessor in predecessors
            if predecessor.workspace_commit
        ]
        predecessor_artifacts = [
            artifact
            for artifact in graph_artifacts
            if artifact.node_id in predecessor_ids
        ]

        # The project's assigned filesystem directory is the canonical control
        # root. A worker may receive a durable Git worktree as its cwd, but all
        # Turn protocol files and state remain rooted at this control path.
        project_repo = await self._project_repo(node.project_id)
        execution_repo = project_repo
        root = await self.store.get_node(node.project_id)
        policy = root.run_policy if root else None
        if (
            project_repo
            and policy is not None
            and policy.workspace_isolation.value == "worktree"
            and node.id != node.project_id
        ):
            isolated = await self.workspaces.isolation_available(project_repo)
            if not isolated:
                # Dirty/non-Git repositories are an explicit serial fallback.
                # Check before allocating a worktree so this path agrees with
                # the scheduler's isolation decision.
                execution_repo = project_repo
            else:
                if node.workspace_path and Path(node.workspace_path).is_dir():
                    execution_repo = node.workspace_path
                else:
                    execution_repo = await self.workspaces.ensure(
                        project_repo,
                        node.id,
                        node.project_id,
                    )
                    branch = self.workspaces.branch_name(
                        project_repo, node.id, node.project_id
                    )
                    await self.store.set_workspace_ref(
                        node.id,
                        path=execution_repo,
                        branch=branch,
                    )
                if predecessor_commits:
                    await self.workspaces.merge_into_workspace(
                        execution_repo,
                        predecessor_commits,
                        node.id,
                    )
        elif node.workspace_path:
            execution_repo = node.workspace_path
        # Wire a live terminal stream: the worker emits raw output chunks and we
        # fan them out over the project SSE bus as `node.terminal` events.
        pid = node.project_id

        async def _stream(nid, chunk):
            await self._emit("node.terminal", pid, {"node_id": str(nid), "chunk": chunk})

        async def _telemetry(event: HarnessEvent) -> None:
            try:
                event = event.model_copy(update={
                    "node_id": event.node_id or str(node.id),
                    "run_id": event.run_id or run_id,
                    "role": event.role or ("setup" if node.id == node.project_id else (node.agent.type_id.value if node.agent else None)),
                    "harness": event.harness or (node.agent.harness.value if node.agent else None),
                    "model": event.model or (node.agent.model if node.agent else None),
                })
                # This is the one structured Turn event stream: EventBus
                # persists it in the ordinary project log and publishes it to
                # live UI subscribers. Do not create a parallel telemetry
                # transport or infer events from the terminal transcript.
                await self._emit("harness.event", pid, event.model_dump(mode="json"))
            except Exception:
                return

        return NodeExecutionContext(
            node=node,
            ancestry=ancestry,
            resources=resources,
            trigger_context=node.trigger_context,
            repo_path=execution_repo,
            project_repo_path=project_repo,
            predecessor_artifacts=predecessor_artifacts,
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
            telemetry=_telemetry,
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
        await self.store.set_agent_session(node.id, session_id, agent=fresh.agent)

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
            await self.store.clear_agent_session(node_id)
            await self._emit("decision.session_cleared", fresh.project_id, {"node_id": str(node_id), "session_id": previous, "reason": "fresh_run"})
        return previous

    async def _prepare_fresh_run(self, node_id: uuid.UUID) -> Node | None:
        """Reset a terminal node for a new prompt-driven harness launch."""
        node = await self.store.get_node(node_id)
        if node is None:
            return None
        try:
            await self.terminal.close_persistent_session(node_id)
        except HerdrAdapterError as error:
            if getattr(self.terminal, "backend_name", None) != "herdr":
                raise
            # A retry must retire the provider session even when Herdr
            # temporarily rejects the optional pane-close cleanup. The pane
            # remains durable and the next native launch can reuse it; a
            # cleanup denial must not turn a user-requested retry into HTTP
            # 500 or leave the graph failed.
            logger.warning("Herdr pane cleanup deferred during fresh run: %s", error)
        previous = await self._reset_provider_session(node_id)
        if previous:
            self.sessions.retire_fresh_session(node_id, previous)
        await self.store.clear_generated_artifacts(node_id)
        await self.store.set_status(node_id, NodeStatus.RUNNABLE)
        return await self.store.get_node(node_id)

    async def _mark_failed(self, node: Node, error: str) -> None:
        changed = await self.store.set_status_if_current(
            node.id,
            NodeStatus.FAILED,
            tuple(status for status in NodeStatus if status is not NodeStatus.CANCELLED),
        )
        if changed is None:
            return
        n = await self.store.get_node(node.id)
        if n is not None:
            await self._request_reviews_for_node(node.id, "child_failed")
            await self._emit("node.updated", n.project_id, _dump(n))

    async def _request_reviews_for_node(
        self, node_id: uuid.UUID, reason: str
    ) -> None:
        """Coalesce management review requests on every owning boundary."""
        node = await self.store.get_node(node_id)
        if node is None or node.parent_id is None:
            return
        nodes, edges, _ = await self.store.get_workgraph(node.project_id)
        for ancestor in GraphWalker(nodes, edges).ancestors(node.id):
            if ancestor.organization_contract is None:
                continue
            await self.organization_manager.request_review(
                self.store, ancestor.id, reason
            )

    async def _emit(self, etype: str, project_id: uuid.UUID, data) -> None:
        await self.events.publish(
            {"type": etype, "project_id": str(project_id), "data": data}
        )

    async def _emit_trigger_event(
        self,
        name: str,
        *,
        project_id: uuid.UUID | None,
        node_id: uuid.UUID | None = None,
        data: dict | None = None,
        source: EventSource = EventSource.AGENT_ACTION,
    ) -> None:
        if self.trigger_dispatcher is not None:
            await self.trigger_dispatcher.emit(
                name,
                source=source,
                project_id=project_id,
                node_id=node_id,
                data=data or {},
            )

    @staticmethod
    def _launch_flags(node: Node, *, resume: bool) -> list[str]:
        """Expose provider-neutral launch intent without leaking secrets."""
        agent = node.agent
        flags: list[str] = []
        if agent is not None and agent.model:
            flags.extend(["--model", agent.model])
        if agent is not None and agent.reasoning:
            flags.extend(["--reasoning", agent.reasoning.value])
        if resume:
            flags.append("--resume-session")
        return flags


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
