"""Provider-neutral local terminal transport.

Local coding harnesses run inside a real pseudo-terminal so their native ANSI
output is preserved and a connected client can send input or resize the
terminal.  Cloud/API harnesses can implement the same ``TerminalTransport``
protocol later without changing workers, the graph, or the UI websocket.
"""
from __future__ import annotations

import asyncio
import json
import os
import pty
import signal
import struct
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol


StreamCallback = Callable[[uuid.UUID, str], Awaitable[None]]


class GenerationStalled(RuntimeError):
    """A live harness stopped producing terminal output."""


@dataclass
class TerminalResult:
    returncode: int
    output: bytes
    display_output: bytes = b""
    stalled: bool = False


class HarnessOutputPresenter:
    """Convert harness machine streams into a compact ANSI transcript.

    Workers still receive the byte-perfect stream for schema/session parsing.
    Terminal clients receive this independent presentation stream, so adding a
    cloud adapter later does not couple its event protocol to xterm.
    """

    def __init__(self, harness: str):
        self.harness = harness
        self._buffer = ""
        self._seen_tools: set[str] = set()

    @staticmethod
    def _text(value: object) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = [str(item.get("text") or "") for item in value if isinstance(item, dict)]
            joined = "\n".join(part for part in parts if part.strip())
            return joined or None
        return None

    def _render_event(self, event: dict) -> str:
        kind = str(event.get("type") or "")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        item_kind = str(item.get("type") or "")
        if kind in {"thread.started", "session", "session.started"}:
            identifier = event.get("thread_id") or event.get("session_id") or event.get("id")
            return f"\x1b[2m{self.harness} session {str(identifier)[:8]}\x1b[0m\r\n" if identifier else ""
        if item_kind in {"agent_message", "message", "reasoning"}:
            body = self._text(item.get("text") or item.get("content"))
            if body:
                structured, summary = self._structured_submission(body)
                if structured:
                    human = summary or "Result submitted to Turn"
                    return f"\x1b[2m{human}\x1b[0m\r\n"
            return f"{body}\r\n" if body else ""
        if item_kind in {"command_execution", "tool_call", "tool_use"}:
            command = self._text(item.get("command") or item.get("name"))
            output = self._text(item.get("aggregated_output") or item.get("output"))
            identity = str(item.get("id") or command or "")
            rendered = ""
            if command and identity not in self._seen_tools:
                self._seen_tools.add(identity)
                rendered = f"\x1b[36m❯ {command}\x1b[0m\r\n"
            if kind.endswith("completed") and output:
                rendered += f"\x1b[2m{output}\x1b[0m\r\n"
            return rendered
        candidate = self._text(event.get("result") or event.get("text") or event.get("content"))
        message = event.get("message")
        if candidate is None and isinstance(message, dict):
            candidate = self._text(message.get("content") or message.get("text"))
        if candidate:
            return f"{candidate}\r\n"
        if kind in {"turn.completed", "result"}:
            return "\x1b[32m✓ completed\x1b[0m\r\n"
        if "error" in kind:
            error = self._text(event.get("error") or event.get("message")) or "Harness error"
            return f"\x1b[31m{error}\x1b[0m\r\n"
        return ""

    @staticmethod
    def _structured_submission(body: str) -> tuple[bool, str | None]:
        """Keep result envelopes in the machine channel, not the terminal."""
        if "```turn-result" in body or "```turn-plan" in body:
            return True, None
        try:
            value = json.loads(body)
        except json.JSONDecodeError:
            return False, None
        structured = isinstance(value, dict) and (
            "outcome" in value or isinstance(value.get("nodes"), list)
        )
        summary = value.get("summary") if structured and isinstance(value, dict) else None
        return structured, summary if isinstance(summary, str) and summary.strip() else None

    def feed(self, chunk: str, *, final: bool = False) -> str:
        self._buffer += chunk
        lines = self._buffer.splitlines(keepends=True)
        if not final and lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""
        rendered: list[str] = []
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            try:
                event = json.loads(clean)
            except json.JSONDecodeError:
                rendered.append(line.replace("\n", "\r\n"))
                continue
            if isinstance(event, dict):
                value = self._render_event(event)
                if value:
                    rendered.append(value)
        return "".join(rendered)


class TerminalTransport(Protocol):
    async def run(
        self,
        node_id: uuid.UUID,
        command: list[str],
        *,
        cwd: str,
        stream: StreamCallback | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
    ) -> TerminalResult: ...

    async def write(self, node_id: uuid.UUID, data: str) -> bool: ...
    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool: ...
    def snapshot(self, node_id: uuid.UUID) -> dict: ...


@dataclass
class _Session:
    node_id: uuid.UUID
    master_fd: int
    process: asyncio.subprocess.Process
    output: bytearray = field(default_factory=bytearray)
    display_output: bytearray = field(default_factory=bytearray)
    subscribers: set[asyncio.Queue[str]] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)
    last_output_at: float = field(default_factory=time.monotonic)
    ended: bool = False
    stalled: bool = False


class LocalPtyTransport:
    """Run one harness process per node in a POSIX PTY."""

    def __init__(self, backlog_limit: int = 2_000_000, completed_session_limit: int = 32):
        self.sessions: dict[uuid.UUID, _Session] = {}
        self.backlog_limit = backlog_limit
        self.completed_session_limit = completed_session_limit

    @staticmethod
    def _window(fd: int, cols: int, rows: int) -> None:
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    async def run(
        self,
        node_id: uuid.UUID,
        command: list[str],
        *,
        cwd: str,
        stream: StreamCallback | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
    ) -> TerminalResult:
        master, slave = pty.openpty()
        self._window(slave, 120, 36)
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        session = _Session(node_id=node_id, master_fd=master, process=process)
        presenter = HarnessOutputPresenter(Path(command[0]).name)
        self.sessions[node_id] = session
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def readable() -> None:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if chunk:
                queue.put_nowait(chunk)
            else:
                loop.remove_reader(master)
                queue.put_nowait(None)

        loop.add_reader(master, readable)

        async def consume() -> None:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                session.last_output_at = time.monotonic()
                session.output.extend(chunk)
                if len(session.output) > self.backlog_limit:
                    del session.output[: len(session.output) - self.backlog_limit]
                raw_text = chunk.decode(errors="replace")
                text = presenter.feed(raw_text)
                if text:
                    session.display_output.extend(text.encode())
                    if len(session.display_output) > self.backlog_limit:
                        del session.display_output[: len(session.display_output) - self.backlog_limit]
                for subscriber in list(session.subscribers):
                    if text:
                        subscriber.put_nowait(text)
                if stream is not None:
                    await stream(node_id, text)

        consumer = asyncio.create_task(consume())
        started = time.monotonic()
        try:
            while process.returncode is None:
                if timeout and time.monotonic() - started >= timeout:
                    self._terminate(session)
                    await self._wait_or_kill(session)
                    raise asyncio.TimeoutError
                if stall_timeout and time.monotonic() - session.last_output_at >= stall_timeout:
                    session.stalled = True
                    self._terminate(session)
                    await self._wait_or_kill(session)
                    break
                await asyncio.sleep(0.1)
            await process.wait()
            await consumer
            tail = presenter.feed("", final=True)
            if tail:
                session.display_output.extend(tail.encode())
                for subscriber in list(session.subscribers):
                    subscriber.put_nowait(tail)
            return TerminalResult(
                process.returncode or 0,
                bytes(session.output),
                bytes(session.display_output),
                session.stalled,
            )
        except asyncio.CancelledError:
            self._terminate(session)
            await self._wait_or_kill(session)
            raise
        finally:
            session.ended = True
            try:
                loop.remove_reader(master)
            except OSError:
                pass
            try:
                os.close(master)
            except OSError:
                pass
            if not consumer.done():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            for subscriber in list(session.subscribers):
                subscriber.put_nowait("")
            self._evict_completed()

    def _evict_completed(self) -> None:
        """Bound reconnect snapshots; durable transcripts live in the store."""
        completed = sorted(
            (session for session in self.sessions.values() if session.ended),
            key=lambda session: session.started_at,
        )
        for session in completed[: max(0, len(completed) - self.completed_session_limit)]:
            self.sessions.pop(session.node_id, None)

    @staticmethod
    def _terminate(session: _Session) -> None:
        if session.process.returncode is not None:
            return
        try:
            os.killpg(session.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

    @staticmethod
    async def _wait_or_kill(session: _Session) -> None:
        try:
            await asyncio.wait_for(session.process.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await session.process.wait()

    async def write(self, node_id: uuid.UUID, data: str) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        try:
            os.write(session.master_fd, data.encode())
            return True
        except OSError:
            return False

    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        self._window(session.master_fd, max(20, cols), max(4, rows))
        return True

    def snapshot(self, node_id: uuid.UUID) -> dict:
        session = self.sessions.get(node_id)
        if session is None:
            return {"active": False, "output": ""}
        return {
            "active": not session.ended,
            "stalled": session.stalled,
            "output": bytes(session.display_output).decode(errors="replace"),
        }

    def subscribe(self, node_id: uuid.UUID) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        session = self.sessions.get(node_id)
        if session is not None:
            session.subscribers.add(queue)
        return queue

    def unsubscribe(self, node_id: uuid.UUID, queue: asyncio.Queue[str]) -> None:
        session = self.sessions.get(node_id)
        if session is not None:
            session.subscribers.discard(queue)
