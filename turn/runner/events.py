"""Minimal async publish/subscribe event bus used to stream changes to clients."""
from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                await q.put(event)
            except Exception:  # pragma: no cover - defensive
                self._subscribers.discard(q)
