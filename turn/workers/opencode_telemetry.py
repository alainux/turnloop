"""OpenCode's documented local-server SSE telemetry side channel."""
from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable

from turn.metrics import emit_jsonl_telemetry
from turn.workers.native_telemetry import emit_telemetry_status


def reserve_loopback_port() -> int | None:
    """Pick an ephemeral loopback port for one native OpenCode TUI process."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])
    except OSError:
        # Local networking can be denied by a sandbox. This must be an
        # observability warning, never a reason to prevent the normal TUI.
        return None


@dataclass
class OpenCodeSseTelemetry:
    port: int
    sink: Callable | None
    connected: bool = False
    received: int = 0
    _task: asyncio.Task | None = None

    @property
    def source(self) -> str:
        return "opencode.server-sse"

    async def start(self) -> None:
        await emit_telemetry_status(
            self.sink, harness="opencode", source=self.source, status="ready",
            detail=f"OpenCode's local event stream will be read from loopback port {self.port}.",
        )
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 15.0
        while loop.time() < deadline:
            writer = None
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.write(
                    b"GET /event HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\nConnection: keep-alive\r\n\r\n"
                )
                await writer.drain()
                headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3)
                if b" 200 " not in headers.splitlines()[0]:
                    raise OSError("OpenCode event endpoint did not return 200")
                self.connected = True
                await emit_telemetry_status(
                    self.sink, harness="opencode", source=self.source, status="connected",
                    detail="OpenCode local event stream is connected; the native TUI remains in the terminal.",
                )
                await self._consume(reader)
                return
            except asyncio.CancelledError:
                raise
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                await asyncio.sleep(0.2)
        await emit_telemetry_status(
            self.sink, harness="opencode", source=self.source, status="unavailable",
            detail="OpenCode's local event endpoint did not become available; the interactive run continues with lifecycle evidence only.",
        )

    async def _consume(self, reader: asyncio.StreamReader) -> None:
        event_type = ""
        data: list[str] = []
        while True:
            raw = await reader.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line.startswith("event:"):
                event_type = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data.append(line.partition(":")[2].lstrip())
            elif not line and data:
                try:
                    payload = json.loads("\n".join(data))
                    if isinstance(payload, dict):
                        payload.setdefault("type", event_type)
                        self.received += 1
                        await emit_jsonl_telemetry("opencode", json.dumps(payload), self.sink)
                except Exception:
                    pass
                event_type = ""
                data = []
