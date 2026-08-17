"""Execution-attempt lifecycle for one Turn node.

Provider-specific planning and worker adapters remain on Runner for now. This
component owns the attempt envelope around them: refresh, status watching,
terminal preparation, cancellation/failure handling, and task release.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from turn.config import Settings
from turn.db.store import PLANNER_EXECUTOR, Store
from turn.domain.schemas import Node, NodeStatus
from turn.runner.scheduler import Scheduler
from turn.workers.terminal import GenerationStalled

logger = logging.getLogger("turn.runner.execution")


def _dump(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


class NodeExecutor:
    """Own one node's execution attempt without owning scheduler state."""

    def __init__(
        self,
        store: Store,
        settings: Settings,
        scheduler: Scheduler,
        status_watchers: dict[uuid.UUID, asyncio.Task],
        forbidden_sessions: dict[uuid.UUID, str],
        emit: Callable[[str, uuid.UUID, object], Awaitable[None]],
        wake: Callable[[], None],
        ensure_terminal: Callable[[uuid.UUID], Awaitable[bool]],
        detach_shell: Callable[[uuid.UUID], Awaitable[bool]],
        agent_status_path: Callable[[Node], Awaitable[Path | None]],
        watch_agent_status: Callable[[uuid.UUID, uuid.UUID, Path], Awaitable[None]],
        plan_node: Callable[..., Awaitable[list[Node]]],
        run_worker: Callable[..., Awaitable[None]],
        mark_cancelled: Callable[[Node], Awaitable[None]],
        mark_failed: Callable[[Node, str], Awaitable[None]],
    ) -> None:
        self.store = store
        self.settings = settings
        self.scheduler = scheduler
        self.status_watchers = status_watchers
        self.forbidden_sessions = forbidden_sessions
        self.emit = emit
        self.wake = wake
        self.ensure_terminal = ensure_terminal
        self.detach_shell = detach_shell
        self.agent_status_path = agent_status_path
        self.watch_agent_status = watch_agent_status
        self.plan_node = plan_node
        self.run_worker = run_worker
        self.mark_cancelled = mark_cancelled
        self.mark_failed = mark_failed

    async def execute(self, node: Node, project_id: uuid.UUID) -> None:
        watcher: asyncio.Task | None = None
        try:
            fresh = await self.store.get_node(node.id)
            if fresh is None:
                return
            node = fresh
            await self.store.set_agent_status(node.id, state=None, message=None)
            status_path = await self.agent_status_path(node)
            if status_path is not None:
                watcher = asyncio.create_task(
                    self.watch_agent_status(node.id, project_id, status_path)
                )
                self.status_watchers[node.id] = watcher
            await self.ensure_terminal(node.id)
            await self.detach_shell(node.id)
            forbidden_session_id = self.forbidden_sessions.pop(node.id, None)
            if node.executor == PLANNER_EXECUTOR and node.status != NodeStatus.EXPANDED:
                await self.plan_node(
                    node,
                    project_id,
                    forbidden_session_id=forbidden_session_id,
                )
            else:
                await self.run_worker(
                    node,
                    project_id,
                    forbidden_session_id=forbidden_session_id,
                )
        except asyncio.CancelledError:
            await self.mark_cancelled(node)
            raise
        except GenerationStalled as error:
            root = await self.store.get_node(project_id)
            policy = root.run_policy if root else None
            max_retries = policy.max_retries if policy else self.settings.max_retries
            retry_stalled = (
                policy.retry_choked_models
                if policy
                else self.settings.retry_choked_models
            )
            if retry_stalled and self.scheduler.retries.get(node.id, 0) < max_retries:
                self.scheduler.retries[node.id] = self.scheduler.retries.get(node.id, 0) + 1
                await self.store.set_status(node.id, NodeStatus.RUNNABLE)
            else:
                await self.mark_failed(node, str(error))
            await self.emit(
                "node.updated",
                project_id,
                _dump(await self.store.get_node(node.id)),
            )
        except Exception as error:
            logger.exception("node %s failed", node.id)
            await self.mark_failed(node, f"runner error: {error}")
        finally:
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                self.status_watchers.pop(node.id, None)
            cleared = await self.store.set_agent_status(
                node.id,
                state=None,
                message=None,
            )
            if cleared is not None:
                await self.emit("node.updated", project_id, _dump(cleared))
            self.scheduler.release(node.id)
            self.wake()
