"""Provider-neutral local terminal transport.

Local coding harnesses run inside a real pseudo-terminal so their native ANSI
output is preserved and a connected client can send input or resize the
terminal.  Cloud/API harnesses can implement the same ``TerminalTransport``
protocol later without changing workers, the graph, or the UI websocket.
"""
from __future__ import annotations

import asyncio
import base64
import codecs
import json
import os
import pty
import shlex
import signal
import struct
import tempfile
import termios
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol, runtime_checkable

from turn.workers.herdr import (
    HerdrAdapter,
    HerdrCliAdapter,
    HerdrResourceNotFound,
)

StreamCallback = Callable[[uuid.UUID, str], Awaitable[None]]
SessionCallback = Callable[[str], Awaitable[None]]
WorkspaceLinkState = Literal["unmapped", "mapped", "missing"]


def _atomic_write_json(path: str, payload: object) -> None:
    """Persist small transport metadata without leaving a partial file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


class GenerationStalled(RuntimeError):
    """A live harness stopped producing terminal output."""


@dataclass
class TerminalResult:
    returncode: int
    # Exact bytes emitted by the child PTY. Turn never interprets these bytes;
    # the browser's terminal emulator is the presentation layer.
    output: bytes
    # Compatibility alias for older workers. It is intentionally the same
    # raw stream, never provider-specific presentation.
    display_output: bytes = b""
    stalled: bool = False
    idle_reaped: bool = False


@runtime_checkable
class TerminalTransport(Protocol):
    supports_inject: bool

    @property
    def available(self) -> bool: ...

    @property
    def backend_name(self) -> str: ...

    async def run(
        self,
        node_id: uuid.UUID,
        command: list[str],
        *,
        cwd: str,
        environment: dict[str, str] | None = None,
        stream: StreamCallback | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
        idle_warning: float | None = None,
        idle_reap: float | None = None,
    ) -> TerminalResult: ...

    async def write(self, node_id: uuid.UUID, data: str | bytes) -> bool: ...
    async def scroll(self, node_id: uuid.UUID, direction: str, amount: int = 1) -> bool: ...
    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool: ...
    async def stop(self, node_id: uuid.UUID) -> bool: ...
    async def close_persistent_session(self, node_id: uuid.UUID) -> bool: ...
    async def ensure_persistent_shell(
        self, node_id: uuid.UUID, *, cwd: str, environment: dict[str, str] | None = None
    ) -> bool: ...
    async def has_persistent_session(self, node_id: uuid.UUID) -> bool: ...
    async def project_workspace_state(self, project_key: str) -> WorkspaceLinkState: ...
    async def close_project_workspace(self, project_key: str) -> bool: ...
    async def close_orphaned_project_workspaces(self, project_keys: set[str]) -> int: ...
    async def detach(self, node_id: uuid.UUID) -> bool: ...
    def release(self, node_id: uuid.UUID) -> bool: ...
    def snapshot(self, node_id: uuid.UUID) -> dict: ...


@dataclass
class _Session:
    node_id: uuid.UUID
    master_fd: int
    process: asyncio.subprocess.Process
    output: bytearray = field(default_factory=bytearray)
    subscribers: set[asyncio.Queue[str]] = field(default_factory=set)
    started_at: float = field(default_factory=time.monotonic)
    last_output_at: float = field(default_factory=time.monotonic)
    last_input_at: float = field(default_factory=time.monotonic)
    last_activity_at: float = field(default_factory=time.monotonic)
    idle_warning_seconds: float = 300.0
    cols: int = 80
    rows: int = 24
    ended: bool = False
    stalled: bool = False
    idle_reaped: bool = False
    # PTY reads can split a UTF-8 code point across two chunks. Keep the
    # decoder state with the stream so the browser receives the same text a
    # native terminal would render instead of replacement characters.
    decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")("replace")
    )


def _prepare_child_tty(fd: int) -> None:
    """Make the PTY slave the child's controlling terminal.

    Passing a PTY as stdin/stdout is not quite enough for an interactive
    process.  Without a controlling terminal, the attach client has no
    foreground terminal process group, so SIGWINCH and terminal queries do
    not behave like they do in a normal terminal emulator.  This is
    especially visible with a persistent multiplexer: the pane can be resized, but the attached
    client remains at its launch size and full-screen output becomes corrupt.
    """
    import fcntl

    os.setsid()
    try:
        fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    except OSError:
        # Some platforms do not expose TIOCSCTTY for an already-established
        # session.  The new session is still useful, and the normal PTY
        # resize path remains available there.
        pass


class LocalPtyTransport:
    """Run one harness process per node in a POSIX PTY."""

    # Native, non-persistent transports run the harness command directly and
    # therefore cannot inject a command into an already-running shell.
    supports_inject = False

    @property
    def backend_name(self) -> str:
        return "local"

    def __init__(self, backlog_limit: int = 2_000_000, completed_session_limit: int = 32):
        self.sessions: dict[uuid.UUID, _Session] = {}
        self.backlog_limit = backlog_limit
        self.completed_session_limit = completed_session_limit

    @property
    def available(self) -> bool:
        """Local PTY support is available whenever this transport is constructed."""
        return True

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
        environment: dict[str, str] | None = None,
        stream: StreamCallback | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
        idle_warning: float | None = None,
        idle_reap: float | None = None,
    ) -> TerminalResult:
        master, slave = pty.openpty()
        # ``os.write`` is used by browser keystrokes and by the initial agent
        # prompt.  A PTY master is blocking by default, so a harness that is
        # slow to consume stdin can otherwise freeze the entire asyncio event
        # loop (including the runner and every UI action).  Reads are already
        # driven by the loop's reader callback; make writes equally safe.
        os.set_blocking(master, False)
        # Start with a conventional terminal size. The browser sends the
        # fitted size as the first websocket message before it receives a
        # replay, so full-screen TUIs repaint at the real viewport dimensions.
        self._window(slave, 80, 24)
        env = os.environ.copy()
        if environment:
            env.update(environment)
        # The Turn server itself may run with NO_COLOR=1 because it is not an
        # interactive terminal. Do not leak that server presentation setting
        # into a native harness: the child owns a real PTY and should decide
        # its own ANSI color output from the terminal capabilities below.
        env.pop("NO_COLOR", None)
        # Server processes are often launched with TERM=dumb, but a native
        # harness needs a real terminal description to enable its TUI. Keep a
        # caller-provided terminal type when it is useful, while normalizing
        # the non-interactive defaults commonly inherited by web servers.
        if env.get("TERM") in (None, "", "dumb"):
            env["TERM"] = "xterm-256color"
        if env.get("COLORTERM") in (None, "", "dumb"):
            env["COLORTERM"] = "truecolor"
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                # Establish the session and controlling terminal in the child
                # before exec.  ``start_new_session`` alone creates a new
                # session but leaves the PTY unattached as a controlling TTY.
                start_new_session=False,
                preexec_fn=lambda: _prepare_child_tty(slave),
            )
        finally:
            os.close(slave)
        session = _Session(
            node_id=node_id,
            master_fd=master,
            process=process,
            idle_warning_seconds=idle_warning or 300.0,
        )
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
                now = time.monotonic()
                session.last_output_at = now
                session.last_activity_at = now
                session.output.extend(chunk)
                if len(session.output) > self.backlog_limit:
                    del session.output[: len(session.output) - self.backlog_limit]
                # The PTY is the source of truth. Decode only at the transport
                # boundary; never parse or rewrite a harness stream here.
                raw_text = session.decoder.decode(chunk, final=False)
                for subscriber in list(session.subscribers):
                    subscriber.put_nowait(raw_text)
                if stream is not None:
                    await stream(node_id, raw_text)

            # Flush a partial code point when the PTY closes. This is the only
            # place where replacement is appropriate: the process has ended,
            # so there can be no later byte that completes the sequence.
            tail = session.decoder.decode(b"", final=True)
            if tail:
                for subscriber in list(session.subscribers):
                    subscriber.put_nowait(tail)
                if stream is not None:
                    await stream(node_id, tail)

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
                if idle_reap and not session.subscribers:
                    idle_for = time.monotonic() - session.last_activity_at
                    if idle_for >= idle_reap:
                        session.idle_reaped = True
                        self._terminate(session)
                        await self._wait_or_kill(session)
                        break
                await asyncio.sleep(0.1)
            await process.wait()
            await consumer
            return TerminalResult(
                process.returncode or 0,
                bytes(session.output),
                bytes(session.output),
                session.stalled,
                session.idle_reaped,
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
        except (PermissionError, ProcessLookupError):
            return

    @staticmethod
    async def _wait_or_kill(session: _Session) -> None:
        try:
            await asyncio.wait_for(session.process.wait(), timeout=2)
        except asyncio.TimeoutError:
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
            await session.process.wait()

    async def write(self, node_id: uuid.UUID, data: str | bytes) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        try:
            os.write(session.master_fd, data if isinstance(data, bytes) else data.encode())
            now = time.monotonic()
            session.last_input_at = now
            session.last_activity_at = now
            return True
        except (BlockingIOError, OSError):
            return False

    async def scroll(self, node_id: uuid.UUID, direction: str, amount: int = 1) -> bool:
        if direction not in {"up", "down"}:
            return False
        sequence = b"\x1b[5~" if direction == "up" else b"\x1b[6~"
        return await self.write(node_id, sequence * max(1, min(amount, 10)))

    async def stop(self, node_id: uuid.UUID) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended or session.process.returncode is not None:
            return False
        self._terminate(session)
        # Do not leave the runner coroutine polling a process that has already
        # been asked to exit. Interactive shells and a few harness wrappers
        # can ignore SIGTERM; use the same bounded wait/kill path as timeout
        # cleanup so closing a terminal always releases its task and PTY.
        await self._wait_or_kill(session)
        return True

    async def close_persistent_session(self, node_id: uuid.UUID) -> bool:
        stopped = await self.stop(node_id)
        released = self.release(node_id)
        return stopped or released

    async def ensure_persistent_shell(
        self, node_id: uuid.UUID, *, cwd: str, environment: dict[str, str] | None = None
    ) -> bool:
        """Local process runs do not need a pre-created multiplexer pane."""
        return True

    async def has_persistent_session(self, node_id: uuid.UUID) -> bool:
        """Report whether this local process transport still has a session."""
        session = self.sessions.get(node_id)
        return session is not None and not session.ended

    async def project_workspace_state(self, project_key: str) -> WorkspaceLinkState:
        """Local PTYs have no external project workspace to reconcile."""
        return "unmapped"

    async def close_project_workspace(self, project_key: str) -> bool:
        """There is no multiplexer workspace in the local process adapter."""
        return False

    async def close_orphaned_project_workspaces(self, project_keys: set[str]) -> int:
        """No-op counterpart to the Herdr workspace reconciliation port."""
        return 0

    async def detach(self, node_id: uuid.UUID) -> bool:
        """End the attaching PTY client without killing the harness.

        For a local subprocess transport this ends the harness too. Subclasses
        with a separate harness process override this to keep the
        harness running and reattachable.
        """
        return await self.stop(node_id)

    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        # A tiny PTY causes full-screen prompts and box drawing to wrap one
        # character at a time. Keep the browser and Herdr panes above the
        # smallest usable geometry even during a resize race.
        session.cols = max(40, cols)
        session.rows = max(8, rows)
        self._window(session.master_fd, session.cols, session.rows)
        session.last_activity_at = time.monotonic()
        return True

    def release(self, node_id: uuid.UUID) -> bool:
        """Drop a completed PTY and its in-memory replay buffer.

        Durable transcripts live in project state. Keeping completed PTYs
        around is unnecessary and makes a long-running Turn server grow
        memory with every finished node. Active or waiting sessions are never
        released by this method.
        """
        session = self.sessions.get(node_id)
        if session is None or not session.ended:
            return False
        self.sessions.pop(node_id, None)
        return True

    def snapshot(self, node_id: uuid.UUID) -> dict:
        session = self.sessions.get(node_id)
        if session is None:
            return {"active": False, "output": ""}
        now = time.monotonic()
        return {
            "active": not session.ended,
            "stalled": session.stalled,
            "idle": bool(
                not session.ended
                and now - session.last_activity_at >= session.idle_warning_seconds
            ),
            "idle_seconds": max(0, now - session.last_activity_at),
            "output_idle_seconds": max(0, now - session.last_output_at),
            "input_idle_seconds": max(0, now - session.last_input_at),
            "subscribers": len(session.subscribers),
            "idle_reaped": session.idle_reaped,
            "cols": session.cols,
            "rows": session.rows,
            # Reconnects receive the same raw byte stream as live subscribers.
            "output": bytes(session.output).decode(errors="replace"),
        }

    def subscribe(self, node_id: uuid.UUID) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        session = self.sessions.get(node_id)
        if session is not None:
            session.subscribers.add(queue)
            session.last_activity_at = time.monotonic()
        return queue

    def unsubscribe(self, node_id: uuid.UUID, queue: asyncio.Queue[str]) -> None:
        session = self.sessions.get(node_id)
        if session is not None:
            session.subscribers.discard(queue)


class HerdrPtyTransport(LocalPtyTransport):
    """Use Herdr workspaces and panes as Turn's durable terminal layer.

    Herdr owns the real shell PTY. Turn only owns a short-lived
    ``terminal session control`` client for each browser/provider attachment;
    closing that client leaves the project pane running in Herdr. A project
    gets one workspace and each node gets one tab/pane inside it.

    The transport intentionally speaks Herdr's documented CLI bridge instead
    of its private socket protocol. That keeps the integration compatible with
    named Herdr sessions and lets Herdr remain the source of truth for pane
    persistence, layout, and human inspection.
    """

    supports_inject = True

    def __init__(
        self,
        data_dir: str,
        backlog_limit: int = 2_000_000,
        completed_session_limit: int = 32,
        adapter: HerdrAdapter | None = None,
    ):
        super().__init__(backlog_limit, completed_session_limit)
        self.adapter = adapter or HerdrCliAdapter()
        self._metadata_path = os.path.join(data_dir, "herdr-workspaces.json")
        try:
            with open(self._metadata_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw = {}
        self._projects: dict[str, dict] = raw if isinstance(raw, dict) else {}
        self._metadata_lock = asyncio.Lock()
        self._pane_create_lock = asyncio.Lock()
        self._control_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._control_closed: dict[uuid.UUID, asyncio.Event] = {}
        self._pane_ready_events: dict[uuid.UUID, asyncio.Event] = {}
        self._node_projects: dict[uuid.UUID, str] = {}

    @staticmethod
    def _is_interactive_shell(command: list[str]) -> bool:
        return (
            len(command) == 2
            and command[1] == "-i"
            and Path(command[0]).name in {"sh", "bash", "zsh", "fish", "ksh"}
        )

    @property
    def available(self) -> bool:
        return self.adapter.available

    @property
    def backend_name(self) -> str:
        return "herdr"

    @staticmethod
    def _project_key(cwd: str, environment: dict[str, str] | None) -> str:
        project_id = (environment or {}).get("TURN_PROJECT_ID")
        if project_id:
            return project_id
        # Shell terminals do not carry the worker environment. The project
        # directory is stable and unique in Turn's project model, so it is a
        # deterministic key for those attaches.
        return f"path:{os.path.realpath(cwd)}"

    async def _save_metadata(self) -> None:
        await asyncio.to_thread(_atomic_write_json, self._metadata_path, self._projects)

    async def _pane_exists(self, pane_id: str) -> bool:
        try:
            await self.adapter.get_pane(pane_id)
        except HerdrResourceNotFound:
            return False
        return True

    async def _ensure_project(
        self,
        project_key: str,
        *,
        cwd: str,
        label: str | None = None,
    ) -> dict:
        async with self._metadata_lock:
            record = self._projects.get(project_key)
            if isinstance(record, dict) and record.get("workspace_id"):
                workspace_id = record["workspace_id"]
                try:
                    await self.adapter.get_workspace(workspace_id)
                except HerdrResourceNotFound:
                    # Herdr is authoritative for externally closed spaces.
                    # Drop only this project's stale mapping before creating a
                    # new one; never reuse a workspace id from another node or
                    # project.
                    for node_id in record.get("panes", {}):
                        try:
                            self._node_projects.pop(uuid.UUID(node_id), None)
                        except (TypeError, ValueError):
                            pass
                    self._projects.pop(project_key, None)
                    await self._save_metadata()
                else:
                    return record

            created = await self.adapter.create_workspace(
                cwd=cwd,
                label=label or f"Turn · {project_key[:8]}",
            )
            record = {"workspace_id": created.workspace_id, "panes": {}}
            # Reuse the workspace's initial shell for the first node.
            record["root_pane"] = created.root_pane_id
            self._projects[project_key] = record
            await self._save_metadata()
            return record

    async def ensure_project_workspace(
        self, project_key: str, *, cwd: str, label: str | None = None
    ) -> str:
        record = await self._ensure_project(project_key, cwd=cwd, label=label)
        return str(record["workspace_id"])

    async def project_workspace_state(self, project_key: str) -> WorkspaceLinkState:
        """Report whether a Turn project still owns a live Herdr workspace."""
        async with self._metadata_lock:
            record = self._projects.get(project_key)
            workspace_id = record.get("workspace_id") if isinstance(record, dict) else None
        if not isinstance(workspace_id, str) or not workspace_id:
            return "unmapped"
        try:
            await self.adapter.get_workspace(workspace_id)
        except HerdrResourceNotFound:
            return "missing"
        return "mapped"

    def project_workspace_id(self, project_key: str) -> str | None:
        record = self._projects.get(project_key)
        workspace_id = record.get("workspace_id") if isinstance(record, dict) else None
        return workspace_id if isinstance(workspace_id, str) and workspace_id else None

    async def close_project_workspace(self, project_key: str) -> bool:
        """Close the Herdr space owned by a Turn project and forget its panes."""
        async with self._metadata_lock:
            record = self._projects.get(project_key)
            workspace_ids: list[str] = []
            if isinstance(record, dict):
                workspace_id = record.get("workspace_id")
                if isinstance(workspace_id, str) and workspace_id:
                    workspace_ids.append(workspace_id)
            else:
                # The workspace is durable in Herdr, while this small mapping
                # file can be edited, truncated, or lost independently. A
                # project deletion must still be able to find the space it
                # owns, otherwise its provider process keeps its conversation
                # locked and the subsequent harness delete command fails.
                expected_label = f"Turn · {project_key[:8]}"
                for workspace in await self.adapter.list_workspaces():
                    if workspace.label == expected_label:
                        workspace_ids.append(workspace.workspace_id)

            if not workspace_ids:
                self._projects.pop(project_key, None)
                await self._save_metadata()
                return False

            for workspace_id in workspace_ids:
                await self.adapter.close_workspace(workspace_id)
            panes_record = record if isinstance(record, dict) else {}
            for node_id in (panes_record.get("panes") or {}):
                try:
                    self._node_projects.pop(uuid.UUID(node_id), None)
                except (TypeError, ValueError):
                    pass
            self._projects.pop(project_key, None)
            await self._save_metadata()
            return True

    async def close_orphaned_project_workspaces(self, project_keys: set[str]) -> int:
        """Close mapped Herdr spaces whose Turn projects no longer exist."""
        async with self._metadata_lock:
            orphaned = [key for key in self._projects if key not in project_keys]
        closed = 0
        for project_key in orphaned:
            if await self.close_project_workspace(project_key):
                closed += 1
        return closed

    async def _ensure_pane(
        self,
        node_id: uuid.UUID,
        *,
        cwd: str,
        environment: dict[str, str] | None,
    ) -> str:
        async with self._pane_create_lock:
            project_key = self._project_key(cwd, environment)
            record = await self._ensure_project(project_key, cwd=cwd)
            panes = record.setdefault("panes", {})
            existing = panes.get(str(node_id))
            if isinstance(existing, str) and await self._pane_exists(existing):
                self._node_projects[node_id] = project_key
                self._pane_ready_events.setdefault(node_id, asyncio.Event()).set()
                return existing

            # The root shell is created together with the workspace. Subsequent
            # nodes receive their own tab, which keeps Herdr's project space easy
            # to scan even when a graph grows beyond a handful of nodes.
            if not panes and isinstance(record.get("root_pane"), str):
                pane_id = record.pop("root_pane")
            else:
                created = await self.adapter.create_tab(
                    workspace_id=str(record["workspace_id"]),
                    cwd=cwd,
                    label=f"node-{node_id.hex[:8]}",
                )
                pane_id = created.pane_id
            panes[str(node_id)] = pane_id
            self._projects[project_key] = record
            await self._save_metadata()
            self._node_projects[node_id] = project_key
            self._pane_ready_events.setdefault(node_id, asyncio.Event()).set()
            return pane_id

    async def has_persistent_session(self, node_id: uuid.UUID) -> bool:
        project_key = self._node_projects.get(node_id)
        if project_key is None:
            for key, record in self._projects.items():
                if str(node_id) in (record.get("panes") or {}):
                    project_key = key
                    self._node_projects[node_id] = key
                    break
        if project_key is None:
            return False
        pane_id = (self._projects.get(project_key, {}).get("panes") or {}).get(str(node_id))
        return isinstance(pane_id, str) and await self._pane_exists(pane_id)

    def pane_id(self, node_id: uuid.UUID) -> str | None:
        project_key = self._node_projects.get(node_id)
        if project_key is None:
            for key, record in self._projects.items():
                if str(node_id) in (record.get("panes") or {}):
                    project_key = key
                    self._node_projects[node_id] = key
                    break
        if project_key is None:
            return None
        value = (self._projects.get(project_key, {}).get("panes") or {}).get(str(node_id))
        return value if isinstance(value, str) else None

    async def foreground_process_names(self, node_id: uuid.UUID) -> tuple[str, ...]:
        pane_id = self.pane_id(node_id)
        if pane_id is None:
            return ()
        reader = getattr(self.adapter, "foreground_process_names", None)
        if reader is None:
            return ()
        return await reader(pane_id)

    async def wait_until_ready(self, node_id: uuid.UUID) -> None:
        """Wait for Herdr's pane-native first-output readiness signal."""
        pane_ready = self._pane_ready_events.setdefault(node_id, asyncio.Event())
        if self.pane_id(node_id) is None:
            await pane_ready.wait()
        pane_id = self.pane_id(node_id)
        if pane_id is None:
            raise HerdrResourceNotFound("pane", str(node_id))
        waiter = getattr(self.adapter, "wait_for_output", None)
        if waiter is None:
            await self.adapter.get_pane(pane_id)
            return
        await waiter(pane_id, regex=".", source="recent-unwrapped")

    def _control_command(self, pane_id: str, cols: int = 80, rows: int = 24) -> list[str]:
        return self.adapter.terminal_control_command(
            pane_id,
            cols=max(40, cols),
            rows=max(8, rows),
        )

    async def _start_control(
        self,
        node_id: uuid.UUID,
        *,
        cwd: str,
        environment: dict[str, str] | None,
        stream: StreamCallback | None,
        idle_warning: float | None,
        idle_reap: float | None,
    ) -> asyncio.Task:
        pane_id = await self._ensure_pane(node_id, cwd=cwd, environment=environment)
        process = await asyncio.create_subprocess_exec(
            *self._control_command(pane_id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=self.backlog_limit,
        )
        session = _Session(
            node_id=node_id,
            master_fd=-1,
            process=process,
            idle_warning_seconds=idle_warning or 300.0,
        )
        self.sessions[node_id] = session
        self._control_locks[node_id] = asyncio.Lock()
        closed = asyncio.Event()
        self._control_closed[node_id] = closed

        async def consume() -> None:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    closed.set()
                    return
                try:
                    record = json.loads(line.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_type = record.get("type") or record.get("event")
                if record_type == "terminal.closed":
                    closed.set()
                    return
                if record_type != "terminal.frame":
                    continue
                encoded = record.get("data_base64")
                if encoded is None:
                    encoded = record.get("bytes")
                if not isinstance(encoded, str):
                    continue
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    continue
                now = time.monotonic()
                session.last_output_at = now
                session.last_activity_at = now
                session.output.extend(chunk)
                if len(session.output) > self.backlog_limit:
                    del session.output[: len(session.output) - self.backlog_limit]
                text = session.decoder.decode(chunk, final=False)
                for subscriber in list(session.subscribers):
                    subscriber.put_nowait(text)
                if stream is not None and text:
                    await stream(node_id, text)

        async def supervise() -> TerminalResult:
            consumer = asyncio.create_task(consume())
            started = time.monotonic()
            try:
                while process.returncode is None and not closed.is_set():
                    if idle_reap and not session.subscribers:
                        idle_for = time.monotonic() - session.last_activity_at
                        if idle_for >= idle_reap:
                            session.idle_reaped = True
                            await self._close_control(node_id)
                            break
                    await asyncio.sleep(0.1)
                await process.wait()
                await consumer
                tail = session.decoder.decode(b"", final=True)
                if tail:
                    for subscriber in list(session.subscribers):
                        subscriber.put_nowait(tail)
                    if stream is not None:
                        await stream(node_id, tail)
                return TerminalResult(
                    process.returncode or 0,
                    bytes(session.output),
                    bytes(session.output),
                    session.stalled,
                    session.idle_reaped,
                )
            except asyncio.CancelledError:
                await self._close_control(node_id)
                raise
            finally:
                session.ended = True
                self._control_locks.pop(node_id, None)
                self._control_closed.pop(node_id, None)
                for subscriber in list(session.subscribers):
                    subscriber.put_nowait("")
                self._evict_completed()

        task = asyncio.create_task(supervise())
        return task

    async def ensure_persistent_shell(
        self, node_id: uuid.UUID, *, cwd: str, environment: dict[str, str] | None = None
    ) -> bool:
        if not self.available:
            return False
        await self._ensure_pane(node_id, cwd=cwd, environment=environment)
        return True

    async def ensure_session(
        self,
        node_id: uuid.UUID,
        *,
        cwd: str,
        environment: dict[str, str] | None = None,
        stream: StreamCallback | None = None,
        idle_warning: float | None = None,
        idle_reap: float | None = None,
    ) -> TerminalResult:
        """Attach to a durable Herdr pane without launching another shell."""
        if not self.available:
            raise RuntimeError("Herdr is required for Herdr terminal sessions")
        task = await self._start_control(
            node_id,
            cwd=cwd,
            environment=environment,
            stream=stream,
            idle_warning=idle_warning,
            idle_reap=idle_reap,
        )
        return await task

    async def refresh_snapshot_from_persistent_pane(
        self, node_id: uuid.UUID, *, source: str = "recent"
    ) -> None:
        """Re-read Herdr's canonical buffer before a browser replay.

        The browser terminal is a renderer, not a second source of terminal
        history. Herdr owns scrollback and its pane read gives reconnects a
        stable, correctly-sized buffer instead of relying on whatever partial
        redraw happened to arrive during control-stream takeover. ``visible``
        is used after a pane scroll so the presenter follows Herdr's viewport.
        """
        session = self.sessions.get(node_id)
        pane_id = self.pane_id(node_id)
        if session is None or pane_id is None:
            return
        output = await self.adapter.read_pane(
            pane_id,
            source=source,
            lines=None,
        )
        if output or source == "visible":
            session.output = bytearray(output.encode())

    async def run(
        self,
        node_id: uuid.UUID,
        command: list[str],
        *,
        cwd: str,
        environment: dict[str, str] | None = None,
        stream: StreamCallback | None = None,
        timeout: float | None = None,
        stall_timeout: float | None = None,
        idle_warning: float | None = None,
        idle_reap: float | None = None,
    ) -> TerminalResult:
        if not self.available:
            raise RuntimeError("Herdr is required for Herdr terminal sessions")
        existed = await self.has_persistent_session(node_id)
        task = await self._start_control(
            node_id,
            cwd=cwd,
            environment=environment,
            stream=stream,
            idle_warning=idle_warning,
            idle_reap=idle_reap,
        )
        if not existed and not self._is_interactive_shell(command):
            injected = await self.inject_command(
                node_id,
                " ".join(shlex.quote(part) for part in command),
                environment=environment,
            )
            if not injected:
                await self.stop(node_id)
                raise RuntimeError("Turn could not inject the harness command into Herdr")
        # Herdr owns the actual terminal process. The controller's timeout and
        # stall policy still apply to Turn's attachment, matching the
        # browser-facing behavior.
        if timeout is not None or stall_timeout is not None:
            started = time.monotonic()
            while not task.done():
                if timeout is not None and time.monotonic() - started >= timeout:
                    await self.stop(node_id)
                    await asyncio.gather(task, return_exceptions=True)
                    raise asyncio.TimeoutError
                session = self.sessions.get(node_id)
                if stall_timeout and session and time.monotonic() - session.last_output_at >= stall_timeout:
                    session.stalled = True
                    await self.stop(node_id)
                    break
                await asyncio.sleep(0.1)
        return await task

    async def _send_control(self, node_id: uuid.UUID, command: dict) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended or session.process.stdin is None:
            return False
        lock = self._control_locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            try:
                session.process.stdin.write((json.dumps(command) + "\n").encode())
                await session.process.stdin.drain()
                session.last_input_at = time.monotonic()
                session.last_activity_at = session.last_input_at
                return True
            except (BrokenPipeError, ConnectionError, OSError):
                return False

    async def inject_command(
        self,
        node_id: uuid.UUID,
        command: str,
        *,
        environment: dict[str, str] | None = None,
    ) -> bool:
        pane_id = self.pane_id(node_id)
        if pane_id is None:
            return False

        # A Herdr pane can retain a partially typed shell line after a failed
        # launch. Clear it with a logical key, then submit the complete command
        # through Herdr's atomic pane-run API. Sending a burst of raw PTY bytes
        # is lossy for long exports and can interleave shell echoes with later
        # chunks, leaving the provider command unexecuted.
        if not await self.adapter.send_keys(pane_id, ("ctrl+c",)):
            return False
        parts = [
            f"export {key}={shlex.quote(str(value))}"
            for key, value in (environment or {}).items()
            if value is not None
        ]
        parts.append(command)
        return await self.adapter.run_command(pane_id, "; ".join(parts))

    async def write(self, node_id: uuid.UUID, data: str | bytes) -> bool:
        payload = data if isinstance(data, bytes) else data.encode()
        return await self._send_control(
            node_id,
            {"type": "terminal.input", "bytes": base64.b64encode(payload).decode()},
        )

    async def scroll(self, node_id: uuid.UUID, direction: str, amount: int = 1) -> bool:
        if direction not in {"up", "down"}:
            return False
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        # Scrollback is owned by Herdr's terminal-control session. Sending
        # logical page keys to the pane would deliver them to the foreground
        # shell/application, and Herdr intentionally rejects those aliases.
        # Keep browser scrolling on the same control stream that owns the
        # pane viewport so Herdr remains the single source of truth.
        return await self._send_control(
            node_id,
            {
                "type": "terminal.scroll",
                "direction": direction,
                "lines": max(1, min(amount, 10)),
            },
        )

    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool:
        session = self.sessions.get(node_id)
        if session is None or session.ended:
            return False
        session.cols = max(40, cols)
        session.rows = max(8, rows)
        return await self._send_control(
            node_id,
            {"type": "terminal.resize", "cols": session.cols, "rows": session.rows},
        )

    async def _close_control(self, node_id: uuid.UUID) -> bool:
        sent = await self._send_control(node_id, {"type": "terminal.release"})
        session = self.sessions.get(node_id)
        if session is not None and not session.ended:
            try:
                await asyncio.wait_for(session.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    pass
                await session.process.wait()
        return sent

    async def stop(self, node_id: uuid.UUID) -> bool:
        # Interrupt the foreground harness, then release only Turn's control
        # stream. The Herdr pane and its shell remain available to the user.
        interrupted = await self.write(node_id, b"\x03")
        released = await self._close_control(node_id)
        return interrupted or released

    async def detach(self, node_id: uuid.UUID) -> bool:
        return await self._close_control(node_id)

    async def close_persistent_session(self, node_id: uuid.UUID) -> bool:
        pane_id = self.pane_id(node_id)
        detached = await self._close_control(node_id)
        if not pane_id or not self.available:
            return detached
        closed = await self.adapter.close_pane(pane_id)
        async with self._metadata_lock:
            for record in self._projects.values():
                (record.get("panes") or {}).pop(str(node_id), None)
            self._node_projects.pop(node_id, None)
            pane_ready = self._pane_ready_events.pop(node_id, None)
            if pane_ready is not None:
                pane_ready.set()
            await self._save_metadata()
        return detached or closed
