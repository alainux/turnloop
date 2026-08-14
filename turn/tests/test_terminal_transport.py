from __future__ import annotations

import asyncio
import sys
import uuid

from turn.workers.terminal import HarnessOutputPresenter, LocalPtyTransport


def test_machine_json_is_rendered_as_human_terminal_output():
    presenter = HarnessOutputPresenter("codex")
    raw = (
        '{"type":"thread.started","thread_id":"123456789"}\n'
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","aggregated_output":"3 passed"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"Implemented and verified."}}\n'
        '{"type":"turn.completed"}\n'
    )
    rendered = presenter.feed(raw, final=True)
    assert "pytest -q" in rendered and "3 passed" in rendered
    assert "Implemented and verified." in rendered
    assert '"type"' not in rendered and "completed" in rendered


def test_result_envelopes_stay_out_of_human_terminal():
    presenter = HarnessOutputPresenter("codex")
    raw = '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"outcome\\":\\"COMPLETE\\",\\"summary\\":\\"done\\"}"}}\n'
    rendered = presenter.feed(raw, final=True)
    assert "done" in rendered
    assert '"outcome"' not in rendered


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
