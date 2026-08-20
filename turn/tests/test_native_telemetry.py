from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid

import pytest

from turn.config import Settings
from turn.domain.schemas import AgentConfig, HarnessKind, Node
from turn.metrics import HarnessEventKind, normalize_claude_event, normalize_codex_event
from turn.workers.base import NodeExecutionContext
from turn.workers.codex_worker import CodexWorker
from turn.workers.terminal import TerminalResult
from turn.workers.native_telemetry import (
    codex_notify_flags,
    drain_late_sidecar,
    prepare_claude_hook_telemetry,
    prepare_codex_notify_telemetry,
    with_claude_telemetry,
)
from turn.workers.opencode_telemetry import OpenCodeSseTelemetry


def test_codex_notify_sidecar_is_attempt_scoped_and_preserves_user_notify_command(tmp_path):
    sidecar = prepare_codex_notify_telemetry(str(tmp_path), uuid.uuid4())
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('notify = ["/usr/local/bin/user-notify", "existing-argument"]\n')

    flags = codex_notify_flags(str(tmp_path), home=home)
    override = json.loads(flags[1].removeprefix("notify="))

    assert sidecar.path.parent == tmp_path / ".turn" / "metrics"
    assert sidecar.source == "codex.notify-rollout"
    assert flags[0] == "-c" and flags[1].startswith("notify=")
    assert override[:2] == [sys.executable, str(tmp_path / ".turn" / "metrics" / "codex-turn-notify.py")]
    assert json.loads(override[2]) == ["/usr/local/bin/user-notify", "existing-argument"]
    contents = (tmp_path / ".turn" / "metrics" / "codex-turn-notify.py").read_text()
    assert "turn.codex.rollout" in contents
    assert "subprocess.run" in contents


def test_codex_notify_script_exports_only_the_correlated_turn_as_jsonl(tmp_path):
    sidecar = prepare_codex_notify_telemetry(str(tmp_path), uuid.uuid4())
    thread_id = "thread-123"
    turn_id = "turn-456"
    session = tmp_path / "codex-home" / "sessions" / "2026" / "08" / "20"
    session.mkdir(parents=True)
    rollout = session / f"rollout-2026-08-20T00-00-00-{thread_id}.jsonl"
    records = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "other-turn"}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "wrong"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": turn_id}},
        {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "input": "npm test", "call_id": "call-1"}},
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "later-turn"}},
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(tmp_path / ".turn" / "metrics" / "codex-turn-notify.py"),
            "[]",
            json.dumps({"type": "agent-turn-complete", "thread-id": thread_id, "turn-id": turn_id}),
        ],
        check=True,
        env={**os.environ, "CODEX_HOME": str(tmp_path / "codex-home"), "TURN_METRICS_FILE": str(sidecar.path)},
    )

    batch = json.loads(sidecar.path.read_text(encoding="utf-8"))
    exported = batch["payload"]["records"]
    assert batch["payload"]["type"] == "turn_rollout"
    assert [record["payload"].get("turn_id") for record in exported if record["type"] == "event_msg"] == [turn_id, turn_id]
    assert [record["payload"].get("input") for record in exported if record["type"] == "response_item"] == ["npm test"]


def test_claude_hooks_are_additive_and_keep_native_prompt_last(tmp_path):
    sidecar, settings = prepare_claude_hook_telemetry(str(tmp_path), uuid.uuid4())

    command = with_claude_telemetry(["claude", "--model", "test", "prompt"], settings)
    payload = json.loads(settings.read_text())

    assert sidecar.source == "claude.hooks"
    assert command[-1] == "prompt"
    assert command[-3:-1] == ["--settings", str(settings)]
    assert {"PreToolUse", "PostToolUse", "Stop"} <= set(payload["hooks"])


def test_native_adapter_events_normalize_to_harness_agnostic_facts():
    codex = normalize_codex_event({"type": "turn.codex.rollout", "payload": {
        "type": "event_msg", "payload": {"type": "function_call", "name": "read_file", "arguments": "{}"},
    }})
    claude = normalize_claude_event({"type": "turn.claude.hook", "payload": {
        "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
        "tool_input": {"command": "pytest"}, "exit_code": 1,
    }})

    assert HarnessEventKind.FILE_READ in {event.kind for event in codex}
    assert HarnessEventKind.TOOL_RESULT in {event.kind for event in claude}
    assert HarnessEventKind.COMMAND_END in {event.kind for event in claude}


def test_codex_notify_batch_is_limited_to_its_correlated_turn():
    events = normalize_codex_event({"type": "turn.codex.rollout", "payload": {
        "type": "turn_rollout",
        "records": [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "input": "npm test", "call_id": "call-1",
            }},
            {"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "call-1", "output": "pass",
            }},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3},
            }}},
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1"}},
        ],
    }})

    assert HarnessEventKind.TOOL_CALL in {event.kind for event in events}
    assert HarnessEventKind.COMMAND_START in {event.kind for event in events}
    assert HarnessEventKind.TOOL_RESULT in {event.kind for event in events}
    assert HarnessEventKind.USAGE in {event.kind for event in events}


@pytest.mark.asyncio
async def test_late_sidecar_events_are_collected_without_holding_the_worker_open(tmp_path):
    sidecar = prepare_codex_notify_telemetry(str(tmp_path), uuid.uuid4())
    received: list[str] = []

    async def handler(line: str) -> None:
        received.append(line)

    async def write_after_handoff() -> None:
        await asyncio.sleep(0.02)
        sidecar.path.write_text('{"type":"turn.codex.rollout"}\n', encoding="utf-8")

    writer = asyncio.create_task(write_after_handoff())
    count = await drain_late_sidecar(sidecar.path, handler, timeout_seconds=0.5)
    await writer

    assert count == 1
    assert received == ['{"type":"turn.codex.rollout"}\n']
    assert not sidecar.path.exists()


@pytest.mark.asyncio
async def test_native_codex_sidecar_never_enables_stdout_capture(tmp_path, monkeypatch):
    import turn.workers.codex_worker as module

    captured = {}

    class NativeTransport:
        supports_inject = True

    async def fake_run(_transport, _node_id, _command, **kwargs):
        captured.update(kwargs)
        kwargs["result_path"].write_text('{"outcome":"COMPLETE","summary":"done"}')
        kwargs["machine_output_path"].write_text('{"type":"turn.codex.rollout"}\n')
        return TerminalResult(returncode=0, output=b"native tui bytes")

    monkeypatch.setattr(module, "LocalPtyTransport", NativeTransport)
    monkeypatch.setattr(module, "run_until_result", fake_run)
    monkeypatch.setattr(module, "agent_environment", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "schedule_late_sidecar_collection", lambda *_args, **_kwargs: None)
    node = Node(project_id=uuid.uuid4(), objective="Test", repo_path=str(tmp_path), executor="codex",
                agent=AgentConfig(harness=HarnessKind.CODEX))

    result = await CodexWorker(Settings(codex_binary="codex")).execute(
        NodeExecutionContext(node=node, repo_path=str(tmp_path), terminal=NativeTransport())
    )

    assert result.outcome.value == "COMPLETE"
    assert captured["machine_output_path"].suffix == ".jsonl"
    assert not captured.get("capture_machine_output", False)


@pytest.mark.asyncio
async def test_opencode_sse_collector_observes_server_events_without_terminal_bytes():
    received = []

    async def sink(event):
        received.append(event)

    collector = OpenCodeSseTelemetry(0, sink)
    reader = asyncio.StreamReader()
    reader.feed_data(b"event: message.part.updated\n")
    reader.feed_data(b'data: {"properties":{"part":{"type":"tool","tool":"read","state":{"status":"completed"},"input":{"path":"README.md"}}}}\n\n')
    reader.feed_eof()
    await collector._consume(reader)

    assert collector.received == 1
    assert HarnessEventKind.FILE_READ in {event.kind for event in received}
