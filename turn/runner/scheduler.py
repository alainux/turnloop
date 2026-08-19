"""Runnable-frontier scheduling for the Turn runner.

The scheduler owns task reservation, stage barriers, retry counters, and the
deleted-project guard. It deliberately knows nothing about provider commands
or worker result decoding; those remain Runner/NodeExecutor concerns.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import Node, NodeStatus
from turn.graph.logic import GraphWalker


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


class Scheduler:
    """Own scheduling state while delegating one-node work to the runner."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        execute_node: Callable[[Node, uuid.UUID], Awaitable[None]],
        emit: Callable[[str, uuid.UUID, object], Awaitable[None]],
        finalize: Callable[[Node], Awaitable[None]],
        wake: Callable[[], None],
    ) -> None:
        self.store = store
        self.settings = settings
        self._execute_node = execute_node
        self._emit = emit
        self._finalize = finalize
        self._wake = wake
        self.running: dict[uuid.UUID, asyncio.Task] = {}
        self.running_projects: dict[uuid.UUID, uuid.UUID] = {}
        self.retries: dict[uuid.UUID, int] = {}
        self.manual_stages: dict[uuid.UUID, set[uuid.UUID]] = {}
        self.last_launch_at: dict[uuid.UUID, float] = {}
        self.deleting_projects: set[uuid.UUID] = set()

    def set_executor(
        self,
        execute_node: Callable[[Node, uuid.UUID], Awaitable[None]],
    ) -> None:
        """Attach the concrete node-attempt owner after composition."""
        self._execute_node = execute_node

    def begin_project_deletion(self, project_id: uuid.UUID) -> bool:
        if project_id in self.deleting_projects:
            return False
        self.deleting_projects.add(project_id)
        return True

    def end_project_deletion(self, project_id: uuid.UUID) -> None:
        self.deleting_projects.discard(project_id)

    def reserve(self, node: Node, project_id: uuid.UUID) -> asyncio.Task:
        task = asyncio.create_task(self._execute_node(node, project_id))
        self.running[node.id] = task
        self.running_projects[node.id] = project_id
        return task

    def release(self, node_id: uuid.UUID) -> None:
        self.running.pop(node_id, None)
        self.running_projects.pop(node_id, None)

    def active_node_ids(self, project_id: uuid.UUID | None = None) -> frozenset[uuid.UUID]:
        return frozenset(
            node_id
            for node_id, task in self.running.items()
            if not task.done()
            and (project_id is None or self.running_projects.get(node_id) == project_id)
        )

    async def wait_for_idle(self, project_id: uuid.UUID | None = None) -> None:
        while True:
            tasks = [
                task
                for node_id, task in self.running.items()
                if not task.done()
                and (project_id is None or self.running_projects.get(node_id) == project_id)
            ]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def schedule_once(self, project_id: uuid.UUID) -> None:
        if project_id in self.deleting_projects:
            return
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return

        active = {node_id for node_id, task in self.running.items() if not task.done()}
        await self.store.cancel_orphaned_runs(project_id, active)
        by_id = {node.id: node for node in nodes}

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
            task = self.running.get(node.id)
            if task is not None and not task.done():
                task.cancel()
            node.status = NodeStatus.CANCELLED
            await self.store.set_status(node.id, NodeStatus.CANCELLED)
            await self._emit("node.updated", project_id, _dump(node))

        walker = GraphWalker(nodes, edges)
        evaluation = walker.evaluate()
        node_by_id = walker.indexes.node_by_id
        # A trigger target is intentionally dormant until its trigger supplies
        # a context. Without this gate an auto-run project can launch a target
        # in the small window between graph creation and the first scheduled
        # or external event, consuming the trigger before it can activate.
        trigger_targets = {
            trigger.target_node_id
            for trigger in await self.store.list_triggers(project_id)
            if trigger.enabled
        }

        for node in nodes:
            effective = evaluation.status.get(node.id)
            if effective not in (NodeStatus.RUNNABLE, NodeStatus.BLOCKED):
                continue
            fresh = await self.store.get_node(node.id)
            if fresh is None or fresh.status in {
                NodeStatus.RUNNING,
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.EXPANDED,
            }:
                continue
            if fresh.status != effective:
                changed = await self.store.set_status_if_current(
                    fresh.id,
                    effective,
                    (NodeStatus.PENDING, NodeStatus.RUNNABLE, NodeStatus.BLOCKED),
                )
                if changed is not None:
                    await self._emit("node.updated", project_id, _dump(changed))

        root = node_by_id.get(project_id)
        if root is not None and root.status in (NodeStatus.EXPANDED, NodeStatus.COMPLETE):
            settled = all(
                evaluation.status.get(node.id)
                in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED)
                for node in nodes
                if node.id != project_id
            )
            if settled and len(nodes) > 1:
                await self._finalize(root)

        if root is None or not root.auto_run:
            return
        policy = root.run_policy
        delay_ms = policy.delay_between_jobs_ms if policy else self.settings.delay_between_jobs_ms
        if delay_ms and time.monotonic() - self.last_launch_at.get(project_id, 0) < delay_ms / 1000:
            return

        runnable_order = [
            candidate.id
            for candidate in walker.topological()
            if candidate.id in evaluation.runnable
            and (
                candidate.id not in trigger_targets
                or candidate.trigger_context is not None
            )
        ]
        for node_id in runnable_order:
            if project_id in self.deleting_projects:
                return
            if node_id in self.running:
                continue
            node = await self.store.get_node(node_id)
            if node is None or node.status in {
                NodeStatus.RUNNING,
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.EXPANDED,
            } or node.paused:
                continue
            self.reserve(node, project_id)
            await self._emit("node.updated", project_id, _dump(node))
            self.last_launch_at[project_id] = time.monotonic()
            if delay_ms:
                break

    async def step(self, project_id: uuid.UUID) -> list[uuid.UUID]:
        """Launch exactly the current runnable frontier in manual mode."""
        if project_id in self.deleting_projects:
            return []
        nodes, edges, _ = await self.store.get_workgraph(project_id)
        if not nodes:
            return []

        stage = self.manual_stages.get(project_id)
        if stage:
            current = {node.id: node for node in nodes}
            settled = all(
                node_id not in self.running
                and current.get(node_id) is not None
                and current[node_id].status
                in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED, NodeStatus.EXPANDED)
                for node_id in stage
            )
            if not settled:
                return []
            self.manual_stages.pop(project_id, None)

        walker = GraphWalker(nodes, edges)
        evaluation = walker.evaluate()
        trigger_targets = {
            trigger.target_node_id
            for trigger in await self.store.list_triggers(project_id)
            if trigger.enabled
        }
        stage_nodes = [
            node
            for node in walker.topological()
            if node.id in evaluation.runnable and node.id not in self.running
            and (
                node.id not in trigger_targets
                or node.trigger_context is not None
            )
        ]
        if not stage_nodes:
            return []

        self.manual_stages[project_id] = {node.id for node in stage_nodes}
        for node in stage_nodes:
            if project_id in self.deleting_projects:
                self.manual_stages.pop(project_id, None)
                return []
            self.reserve(node, project_id)
            await self._emit("node.updated", project_id, _dump(node))
        return [node.id for node in stage_nodes]
