from __future__ import annotations

import asyncio
import errno
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

from turn.tests.mocks import MockHerdrAdapter
from turn.workers.herdr import HerdrAdapterError, HerdrCliAdapter, HerdrResourceNotFound
from turn.workers.terminal import HerdrPtyTransport, LocalPtyTransport


def test_local_pty_advertises_terminal_availability():
    assert LocalPtyTransport().available


async def test_local_pty_forwards_machine_bytes_without_transform(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    raw = '{"type":"message","text":"leave this exactly as emitted"}'
    result = await transport.run(
        node_id,
        [sys.executable, "-c", f"print({raw!r}, flush=True)"],
        cwd=str(tmp_path),
        timeout=5,
    )
    assert raw.encode() in result.output
    assert result.display_output == result.output
    assert raw in transport.snapshot(node_id)["output"]


async def test_local_pty_attaches_subscribers_created_before_process_start(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    queue = transport.subscribe(node_id)
    task = asyncio.create_task(
        transport.run(
            node_id,
            [sys.executable, "-c", "print('control-ready', flush=True)"],
            cwd=str(tmp_path),
            timeout=5,
        )
    )
    chunks: list[str] = []
    while "control-ready" not in "".join(chunks):
        chunks.append(await asyncio.wait_for(queue.get(), timeout=5))
    result = await task
    transport.unsubscribe(node_id, queue)
    assert result.returncode == 0


async def test_local_pty_does_not_replace_utf8_split_across_reads(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    pieces: list[str] = []
    command = [
        sys.executable,
        "-c",
        (
            "import os,time; data='─'.encode(); "
            "[os.write(1, bytes([byte])) or time.sleep(.03) for byte in data]"
        ),
    ]
    result = await transport.run(
        node_id,
        command,
        cwd=str(tmp_path),
        stream=lambda _node_id, chunk: _record_terminal_chunk(pieces, chunk),
        timeout=5,
    )
    assert result.returncode == 0
    assert "".join(pieces) == "─"
    assert "�" not in "".join(pieces)


async def test_local_pty_does_not_treat_partial_utf8_decode_as_eof(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    queue = transport.subscribe(node_id)
    result = await transport.run(
        node_id,
        [
            sys.executable,
            "-c",
            (
                "import os,time; data='😀'.encode(); "
                "[os.write(1, bytes([byte])) or time.sleep(.04) for byte in data]"
            ),
        ],
        cwd=str(tmp_path),
        timeout=5,
    )
    assert result.returncode == 0
    assert await asyncio.wait_for(queue.get(), timeout=1) == "😀"
    assert await asyncio.wait_for(queue.get(), timeout=1) is None
    transport.unsubscribe(node_id, queue)


async def test_local_pty_eagain_keeps_the_reader_alive(tmp_path, monkeypatch):
    transport = LocalPtyTransport()
    master_fd: int | None = None
    injected = False
    real_openpty = os.openpty
    real_read = os.read

    def openpty():
        nonlocal master_fd
        master, slave = real_openpty()
        master_fd = master
        return master, slave

    def read(fd: int, size: int):
        nonlocal injected
        if fd == master_fd and not injected:
            injected = True
            raise BlockingIOError(errno.EAGAIN, "try again")
        return real_read(fd, size)

    monkeypatch.setattr(os, "openpty", openpty)
    monkeypatch.setattr(os, "read", read)
    result = await transport.run(
        uuid.uuid4(),
        [sys.executable, "-c", "print('after-eagain', flush=True)"],
        cwd=str(tmp_path),
        timeout=5,
    )
    assert injected
    assert result.returncode == 0
    assert b"after-eagain" in result.output


async def test_local_pty_flushes_decoder_at_explicit_eof(tmp_path):
    transport = LocalPtyTransport()
    pieces: list[str] = []
    result = await transport.run(
        uuid.uuid4(),
        [sys.executable, "-c", "import os; os.write(1, b'\\xf0')"],
        cwd=str(tmp_path),
        timeout=5,
        stream=lambda _node_id, chunk: _record_terminal_chunk(pieces, chunk),
    )
    assert result.returncode == 0
    assert "�" in "".join(pieces)


async def _record_terminal_chunk(pieces: list[str], chunk: str) -> None:
    pieces.append(chunk)


async def test_local_pty_preserves_ansi_accepts_input_and_resizes(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    command = [
        sys.executable,
        "-c",
        "import sys; print('\\x1b[31mRED\\x1b[0m', flush=True); value=input(); print('GOT:'+value, flush=True)",
    ]
    task = asyncio.create_task(transport.run(node_id, command, cwd=str(tmp_path), timeout=5, stall_timeout=2))
    for _ in range(100):
        if transport.snapshot(node_id).get("active") and "RED" in transport.snapshot(node_id).get("output", ""):
            break
        await asyncio.sleep(0.01)
    assert await transport.resize(node_id, 90, 28)
    assert await transport.write(node_id, "hello\n")
    result = await task
    assert result.returncode == 0 and not result.stalled
    assert b"\x1b[31mRED\x1b[0m" in result.output
    assert b"GOT:hello" in result.output
    assert transport.snapshot(node_id)["active"] is False


async def test_local_resize_keeps_a_usable_terminal_geometry(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    task = asyncio.create_task(
        transport.run(
            node_id,
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=str(tmp_path),
            timeout=2,
        )
    )
    for _ in range(100):
        if transport.snapshot(node_id).get("active"):
            break
        await asyncio.sleep(0.01)
    assert await transport.resize(node_id, 1, 1)
    session = transport.sessions[node_id]
    assert (session.cols, session.rows) == (40, 8)
    await transport.stop(node_id)
    await task


async def test_local_scroll_forwards_bounded_page_keys_without_buffer_rewrite():
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    sent: list[bytes] = []

    async def capture(_node_id, data):
        sent.append(data if isinstance(data, bytes) else data.encode())
        return True

    transport.write = capture  # type: ignore[method-assign]
    assert await transport.scroll(node_id, "up", amount=20)
    assert sent == [b"\x1b[5~" * 10]
    assert await transport.scroll(node_id, "down", amount=2)
    assert sent[-1] == b"\x1b[6~" * 2
    assert not await transport.scroll(node_id, "sideways")


async def test_local_pty_keeps_provider_stream_raw_and_releases_completed_session(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    result = await transport.run(
        node_id,
        [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'type':'text','part':{'text':'hello from provider'}}), flush=True)",
        ],
        cwd=str(tmp_path),
        timeout=5,
    )
    snapshot = transport.snapshot(node_id)
    assert '"type": "text"' in result.display_output.decode()
    assert "hello from provider" in snapshot["output"]
    assert '"type": "text"' in snapshot["output"]
    assert transport.release(node_id)
    assert transport.snapshot(node_id) == {"active": False, "output": ""}


async def test_local_pty_normalizes_noninteractive_terminal_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    transport = LocalPtyTransport()
    result = await transport.run(
        uuid.uuid4(),
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('TERM')+' '+os.getenv('COLORTERM')+' '+str(os.getenv('NO_COLOR')))"
        ],
        cwd=str(tmp_path),
        timeout=5,
    )
    assert b"xterm-256color truecolor None" in result.output


async def test_local_pty_detects_silent_live_process(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    result = await transport.run(
        node_id,
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(3)"],
        cwd=str(tmp_path),
        timeout=5,
        stall_timeout=0.2,
    )
    assert result.stalled
    assert b"started" in result.output


async def test_local_pty_stop_ends_an_interactive_session(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    task = asyncio.create_task(
        transport.run(
            node_id,
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=str(tmp_path),
            timeout=5,
        )
    )
    for _ in range(100):
        if transport.snapshot(node_id).get("active"):
            break
        await asyncio.sleep(0.01)
    assert await transport.stop(node_id)
    result = await task
    assert result.returncode != 0


async def test_detached_quiet_terminal_is_reaped_after_grace_period(tmp_path):
    transport = LocalPtyTransport()
    result = await transport.run(
        uuid.uuid4(),
        [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(3)"],
        cwd=str(tmp_path),
        timeout=None,
        stall_timeout=None,
        idle_warning=0.05,
        idle_reap=0.2,
    )
    assert result.idle_reaped
    assert result.returncode != 0


async def test_attached_terminal_is_not_reaped_while_quiet(tmp_path):
    transport = LocalPtyTransport()
    node_id = uuid.uuid4()
    task = asyncio.create_task(
        transport.run(
            node_id,
            [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(.35)"],
            cwd=str(tmp_path),
            timeout=2,
            stall_timeout=None,
            idle_warning=0.05,
            idle_reap=0.1,
        )
    )
    for _ in range(100):
        if transport.snapshot(node_id).get("active"):
            break
        await asyncio.sleep(0.01)
    queue = transport.subscribe(node_id)
    result = await task
    transport.unsubscribe(node_id, queue)
    assert result.returncode == 0
    assert not result.idle_reaped


async def test_herdr_replaces_an_externally_closed_workspace_mapping(tmp_path):
    adapter = MockHerdrAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    created = await adapter.create_workspace(cwd=str(tmp_path), label="stale")
    transport._projects["project-1"] = {
        "workspace_id": created.workspace_id,
        "root_pane": created.root_pane_id,
        "panes": {},
    }
    await adapter.close_workspace(created.workspace_id)

    replacement = await transport.ensure_project_workspace(
        "project-1", cwd=str(tmp_path), label="replacement"
    )

    assert replacement != created.workspace_id
    assert await adapter.get_workspace(replacement)


async def test_herdr_keeps_a_durable_pane_mapping_on_transient_permission_error(tmp_path):
    class StalePaneAdapter(MockHerdrAdapter):
        async def get_pane(self, pane_id: str):
            raise HerdrAdapterError(
                f"Error: Os {{ kind: PermissionDenied, message: Operation not permitted }} for {pane_id}"
            )

    transport = HerdrPtyTransport(str(tmp_path), adapter=StalePaneAdapter())

    assert await transport._pane_exists("w-stale:p1")


async def test_herdr_retry_forces_new_command_when_pane_close_is_denied(tmp_path):
    class DeferredCloseAdapter(MockHerdrAdapter):
        async def close_pane(self, pane_id: str):
            del pane_id
            raise HerdrAdapterError("Operation not permitted")

    adapter = DeferredCloseAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    node_id = uuid.uuid4()
    await transport.ensure_persistent_shell(
        node_id,
        cwd=str(tmp_path),
        environment={"TURN_PROJECT_ID": "project-1"},
    )

    assert await transport.close_persistent_session(node_id)
    assert node_id in transport._fresh_launch_nodes
    assert transport.pane_id(node_id) is not None
    assert not await transport.has_persistent_session(node_id)


async def test_herdr_stop_interrupts_and_closes_the_provider_pane(tmp_path):
    adapter = MockHerdrAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    node_id = uuid.uuid4()
    await transport.ensure_persistent_shell(node_id, cwd=str(tmp_path))
    pane_id = transport.pane_id(node_id)

    assert pane_id is not None
    assert await transport.stop(node_id)
    assert adapter.sent_keys == [(pane_id, ("esc",))]
    assert pane_id not in adapter.panes
    assert transport.pane_id(node_id) is None


def test_herdr_cli_keeps_permission_errors_visible_for_lookup_commands():
    with pytest.raises(HerdrAdapterError, match="Operation not permitted"):
        HerdrCliAdapter._raise_command_error(
            "Error: Os { kind: PermissionDenied, message: Operation not permitted }",
            ("pane", "get", "w-stale:p1"),
        )


async def test_herdr_scroll_targets_the_node_pane_not_the_control_stream(tmp_path):
    adapter = MockHerdrAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    node_id = uuid.uuid4()
    assert await transport.ensure_persistent_shell(node_id, cwd=str(tmp_path))

    commands: list[dict] = []

    async def capture_control(_node_id, command):
        commands.append(command)
        return True

    transport.sessions[node_id] = SimpleNamespace(ended=False)
    transport._send_control = capture_control

    assert await transport.scroll(node_id, "up", amount=2)
    assert commands == [
        {"type": "terminal.scroll", "direction": "up", "lines": 2}
    ]

    assert await transport.scroll(node_id, "down")
    assert commands[-1] == {
        "type": "terminal.scroll",
        "direction": "down",
        "lines": 1,
    }


async def test_herdr_wait_until_ready_uses_pane_output_signal(tmp_path):
    adapter = MockHerdrAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    node_id = uuid.uuid4()

    assert await transport.ensure_persistent_shell(node_id, cwd=str(tmp_path))
    await transport.wait_until_ready(node_id)

    assert adapter.wait_outputs == [
        (transport.pane_id(node_id), ".", "recent-unwrapped", None)
    ]



async def test_local_pty_bounds_completed_reconnect_snapshots(tmp_path):
    transport = LocalPtyTransport(backlog_limit=256, completed_session_limit=2)
    ids = [uuid.uuid4() for _ in range(4)]
    for index, node_id in enumerate(ids):
        await transport.run(
            node_id,
            [sys.executable, "-c", f"print('session-{index}')"],
            cwd=str(tmp_path),
            timeout=5,
        )
    assert set(transport.sessions) == set(ids[-2:])
    assert transport.snapshot(ids[0]) == {"active": False, "output": ""}
    assert "session-3" in transport.snapshot(ids[-1])["output"]


def test_herdr_transport_is_available_from_an_external_turn_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SESSION", "turn-demo")
    transport = HerdrPtyTransport(str(tmp_path), adapter=HerdrCliAdapter("herdr"))
    assert transport.available
    assert transport.backend_name == "herdr"
    assert transport.adapter.command("workspace", "list")[:3] == [
        "herdr", "--session", "turn-demo"
    ]


def test_herdr_adapter_is_a_client_and_never_owns_daemon_lifecycle():
    adapter = HerdrCliAdapter("herdr")

    assert adapter.command("workspace", "list") == ["herdr", "workspace", "list"]
    assert "server" not in adapter.command("workspace", "list")
    assert not hasattr(adapter, "start")
    assert not hasattr(adapter, "stop")


def test_herdr_transport_scopes_nodes_to_project_workspaces(tmp_path):
    transport = HerdrPtyTransport(str(tmp_path), adapter=HerdrCliAdapter("herdr"))
    assert transport._project_key(str(tmp_path), {"TURN_PROJECT_ID": "project-1"}) == "project-1"
    assert transport._project_key(str(tmp_path), None).startswith("path:")
    assert transport._control_command("w1:p1")[-4:] == [
        "--cols", "80", "--rows", "24"
    ]


async def test_herdr_atomic_pane_commands_accept_empty_cli_responses(tmp_path):
    log = tmp_path / "herdr-commands.log"
    binary = tmp_path / "herdr"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(log)!r}\n"
    )
    binary.chmod(0o755)
    adapter = HerdrCliAdapter(str(binary))

    assert await adapter.send_keys("w1:p1", ("ctrl+c",))
    assert await adapter.run_command("w1:p1", "export X=1; codex")
    assert log.read_text().splitlines() == [
        "pane send-keys w1:p1 ctrl+c",
        "pane run w1:p1 export X=1; codex",
    ]


async def test_herdr_resource_existence_uses_get_surfaces(tmp_path):
    binary = tmp_path / "herdr"
    binary.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  'workspace get w1') printf '%s\\n' '{\"result\":{\"workspace\":{\"workspace_id\":\"w1\",\"label\":\"demo\",\"pane_count\":1,\"tab_count\":1}}}' ;;\n"
        "  'pane get w1:p1') printf '%s\\n' '{\"result\":{\"pane\":{\"pane_id\":\"w1:p1\"}}}' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    binary.chmod(0o755)
    adapter = HerdrCliAdapter(str(binary))

    workspace = await adapter.get_workspace("w1")
    pane = await adapter.get_pane("w1:p1")

    assert workspace.workspace_id == "w1"
    assert pane.pane_id == "w1:p1"


async def test_herdr_workspace_snapshot_reads_all_projects_once(tmp_path):
    adapter = MockHerdrAdapter()
    transport = HerdrPtyTransport(str(tmp_path), adapter=adapter)
    await transport.ensure_project_workspace("project-1", cwd=str(tmp_path))
    calls = 0
    original = adapter.list_workspaces

    async def counted_list():
        nonlocal calls
        calls += 1
        return await original()

    adapter.list_workspaces = counted_list
    states = await transport.project_workspace_states({"project-1", "project-2"})

    assert states == {"project-1": "mapped", "project-2": "unmapped"}
    assert calls == 1


async def test_herdr_output_wait_uses_native_wait_command(tmp_path):
    log = tmp_path / "herdr-commands.log"
    binary = tmp_path / "herdr"
    binary.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(log)!r}\n"
        "printf '%s\\n' '{\"result\":{}}'\n"
    )
    binary.chmod(0o755)
    adapter = HerdrCliAdapter(str(binary))

    assert await adapter.wait_for_output("w1:p1")
    assert log.read_text().splitlines() == [
        "pane wait-output w1:p1 --regex . --source recent-unwrapped",
    ]
