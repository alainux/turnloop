"""Minimal async publish/subscribe event bus used to stream changes to clients."""
from __future__ import annotations

import asyncio
from typing import Any

from turn.logging import EventLog


class EventBus:
    def __init__(self, logs: EventLog | None = None):
        self._subscribers: set[asyncio.Queue] = set()
        self.logs = logs

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        if self.logs is not None and event.get("type") not in {"node.terminal", "node.updated", "heartbeat"}:
            await self.logs.emit(
                event.get("project_id"),
                kind=str(event.get("type") or "event"),
                message=str(event.get("type") or "event"),
                status="error" if str(event.get("type") or "").endswith((".error", "_failed")) else "info",
                source="event-bus",
                data=event.get("data"),
            )
        for q in list(self._subscribers):
            try:
                await q.put(event)
            except Exception:  # pragma: no cover - defensive
                self._subscribers.discard(q)
