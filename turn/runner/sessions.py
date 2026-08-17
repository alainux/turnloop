"""Provider and terminal session state owned by the runner runtime."""
from __future__ import annotations

import asyncio
import uuid

from turn.workers.terminal import TerminalTransport


class SessionController:
    """Own durable terminal access and process-local session bookkeeping."""

    def __init__(self, terminal: TerminalTransport) -> None:
        self.terminal = terminal
        self.reconnect_tasks: dict[uuid.UUID, asyncio.Task] = {}
        self.shell_tasks: dict[uuid.UUID, asyncio.Task] = {}
        self.forbidden_fresh_sessions: dict[uuid.UUID, str] = {}

    async def stop_all(self) -> None:
        """Cancel reconnect and shell tasks without touching graph state."""
        tasks = [
            *self.reconnect_tasks.values(),
            *self.shell_tasks.values(),
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def retire_fresh_session(self, node_id: uuid.UUID, session_id: str | None) -> None:
        if session_id:
            self.forbidden_fresh_sessions[node_id] = session_id
