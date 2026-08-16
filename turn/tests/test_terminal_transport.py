from __future__ import annotations

import asyncio
import sys
import uuid

from turn.tests.fakes import FakeHerdrAdapter
from turn.workers.herdr import HerdrCliAdapter
from turn.workers.terminal import HerdrPtyTransport, LocalPtyTransport


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
    adapter = FakeHerdrAdapter()
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


def test_herdr_transport_scopes_nodes_to_project_workspaces(tmp_path):
    transport = HerdrPtyTransport(str(tmp_path), adapter=HerdrCliAdapter("herdr"))
    assert transport._project_key(str(tmp_path), {"TURN_PROJECT_ID": "project-1"}) == "project-1"
    assert transport._project_key(str(tmp_path), None).startswith("path:")
    assert transport._control_command("w1:p1")[-4:] == [
        "--cols", "80", "--rows", "24"
    ]
