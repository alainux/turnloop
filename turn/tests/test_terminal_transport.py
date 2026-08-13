from __future__ import annotations

import asyncio
import sys
import uuid

from turn.workers.terminal import LocalPtyTransport


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
