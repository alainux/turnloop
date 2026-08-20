from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from turn.workers.interactive import (
    _new_codex_session_id,
    _injected_command_with_markers,
    agent_environment,
    prepare_result_file,
    read_codex_session_usage,
    read_result_file,
    run_until_result,
)
from turn.tests.mocks import MockHerdrAdapter
from turn.tests.capability_fixtures import load_builtin_capabilities
from turn.workers.terminal import HerdrPtyTransport, LocalPtyTransport, TerminalResult
from turn.domain.schemas import AgentConfig


@pytest.fixture(autouse=True)
def load_builtin_capability_plugins(tmp_path):
    # Direct worker tests bypass the planner boundary; production workers only
    # receive projects whose planner has already installed these files.
    load_builtin_capabilities(tmp_path)


async def test_injected_command_clears_partial_shell_input(tmp_path, monkeypatch):
    transport = HerdrPtyTransport(str(tmp_path), adapter=MockHerdrAdapter())
    node_id = uuid.uuid4()
    await transport._ensure_pane(node_id, cwd=str(tmp_path), environment={"TURN_PROJECT_ID": "project"})
    await transport.inject_command(
        node_id, "codex --model test", environment={"TURN_PROJECT_ID": "project"}
    )

    assert transport.adapter.sent_keys[-1][1] == ("ctrl+c",)
    assert transport.adapter.run_commands[-1][1] == (
        "export TURN_PROJECT_ID=project; codex --model test"
    )


def test_worker_environment_uses_the_inherited_turn_command(tmp_path):
    node_id = uuid.uuid4()
    handoff = prepare_result_file(str(tmp_path), node_id, "result")
    environment = agent_environment(str(tmp_path), node_id, "result", handoff)

    assert "TURN_CLI" not in environment
    completed = subprocess.run(
        ["turn", "agent", "--help"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "submit" in completed.stdout


def test_injected_command_publishes_a_failed_child_exit_code(tmp_path):
    start_path = tmp_path / "attempt.started"
    exit_path = tmp_path / "attempt.exit"
    command = _injected_command_with_markers(
        [sys.executable, "-c", "import sys; sys.exit(23)"],
        start_path,
        exit_path,
    )

    completed = subprocess.run(["sh", "-c", command], check=True)

    assert completed.returncode == 0
    assert start_path.read_text().strip()
    assert exit_path.read_text() == "23\n"


def test_injected_command_captures_machine_stdout_without_reading_the_pty(tmp_path):
    start_path = tmp_path / "attempt.started"
    exit_path = tmp_path / "attempt.exit"
    machine_output = tmp_path / "attempt.jsonl"
    command = _injected_command_with_markers(
        [sys.executable, "-c", "print('{\\\"type\\\": \\\"item.completed\\\"}')"],
        start_path,
        exit_path,
        machine_output,
    )

    completed = subprocess.run(["sh", "-c", command], check=True, capture_output=True, text=True)

    assert completed.stdout == ""
    assert machine_output.read_text() == '{"type": "item.completed"}\n'
    assert start_path.exists()
    assert exit_path.read_text() == "0\n"


def test_worker_environment_rejects_an_unloaded_capability(tmp_path):
    node_id = uuid.uuid4()
    handoff = prepare_result_file(str(tmp_path), node_id, "result")
    agent = AgentConfig(capabilities=["visual-qa"])

    with pytest.raises(ValueError, match="not loaded"):
        agent_environment(str(tmp_path), node_id, "result", handoff, agent=agent)


def test_verifier_environment_uses_verification_handoff(tmp_path):
    node_id = uuid.uuid4()
    handoff = prepare_result_file(str(tmp_path), node_id, "verification")
    environment = agent_environment(str(tmp_path), node_id, "verification", handoff)

    assert handoff.name.endswith(".verification.json")
    assert environment["TURN_HANDOFF_KIND"] == "verification"
    assert environment["TURN_HANDOFF_FILE"] == str(handoff)
def test_stdin_handoff_prompt_is_safe_for_apostrophes(tmp_path):
    handoff = tmp_path / "node.result.json"
    environment = os.environ.copy()
    environment.update({
        "TURN_HANDOFF_FILE": str(handoff),
        "TURN_STATUS_FILE": str(tmp_path / "status.json"),
        "TURN_NODE_ID": str(uuid.uuid4()),
    })
    command = """turn agent submit --kind result --stdin <<'TURN_PAYLOAD'
{"outcome":"COMPLETE","summary":"agent's result is valid","missing_inputs":[]}
TURN_PAYLOAD
"""

    completed = subprocess.run(
        ["sh", "-c", command],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert json.loads(handoff.read_text()) == {
        "outcome": "COMPLETE",
        "summary": "agent's result is valid",
        "missing_inputs": [],
    }


def test_new_codex_session_id_matches_node_marker_not_latest_mtime(tmp_path, monkeypatch):
    import os
    import time

    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "15"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    node_id = str(uuid.uuid4())
    other_id = "01ffffffff-ffff-7fff-8fff-ffffffffffff"
    wanted_id = "01000000-0000-7000-8000-000000000001"
    common = {"cwd": str(tmp_path)}
    newest = session_dir / "rollout-newest.jsonl"
    wanted = session_dir / "rollout-wanted.jsonl"
    newest.write_text(json.dumps({"type": "session_meta", "payload": {"session_id": other_id, **common}}) + "\n")
    wanted.write_text(
        json.dumps({"type": "session_meta", "payload": {"session_id": wanted_id, **common}})
        + "\n"
        + ("{}\n" * 20)
        + json.dumps({"type": "message", "payload": {"text": f"TURN node id: {node_id}"}})
        + "\n"
    )
    now = time.time()
    os.utime(newest, (now + 2, now + 2))
    os.utime(wanted, (now + 1, now + 1))

    assert _new_codex_session_id(str(tmp_path), now - 1, node_id) == wanted_id
    assert (
        _new_codex_session_id(
            str(tmp_path),
            now - 1,
            node_id,
            excluded_session_ids={wanted_id},
        )
        is None
    )


def test_codex_session_usage_reads_latest_per_turn_count(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "15"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    session_id = "019fff3a-2c16-7143-8ec3-1f68bce525e7"
    session_file = session_dir / f"rollout-2026-08-15T00-00-00-000000-{session_id}.jsonl"
    session_file.write_text(
        json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 90,
                        "output_tokens": 20,
                    },
                    "last_token_usage": {
                        "input_tokens": 12,
                        "cached_input_tokens": 8,
                        "output_tokens": 3,
                    },
                },
            },
        })
        + "\n"
    )

    assert read_codex_session_usage(session_id).model_dump() == {
        "input_tokens": 12,
        "cached_input_tokens": 8,
        "output_tokens": 3,
        "cost_usd": None,
    }


async def test_codex_verifier_round_trips_verification_handoff(tmp_path, monkeypatch):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker
    import turn.workers.codex_worker as codex_module

    class MockNativeTransport:
        pass

    async def mock_run_until_result(_transport, _node_id, _command, **kwargs):
        assert kwargs["environment"]["TURN_HANDOFF_KIND"] == "verification"
        assert kwargs["result_path"].name.endswith(".verification.json")
        assert "turn agent verify --stdin" not in _command[-1]
        assert "TURN_CONTEXT" in _command[-1]
        assert "initial_input" not in kwargs
        kwargs["result_path"].write_text(
            '{"decision":"REJECT","summary":"The game is not playable",'
            '"findings":["The launch command fails"],'
            '"required_changes":["Fix the entry point"],"evidence_refs":[]}'
        )
        return TerminalResult(returncode=0, output=b"verification transcript")

    monkeypatch.setattr(codex_module, "LocalPtyTransport", MockNativeTransport)
    monkeypatch.setattr(codex_module, "run_until_result", mock_run_until_result)

    node = Node(
        project_id=uuid.uuid4(),
        objective="Verify the game",
        generated_prompt="Inspect the game and reject it if it cannot launch.",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
            type_id=AgentType.VERIFIER,
            model="free-test",
        ),
    )
    result = await CodexWorker(Settings(codex_binary="codex")).execute(
        NodeExecutionContext(node=node, repo_path=str(tmp_path), terminal=MockNativeTransport())
    )

    assert result.outcome.value == "COMPLETE"
    assert result.verification is not None
    assert result.verification.decision.value == "REJECT"


async def test_codex_worker_reports_missing_result_without_secondary_json_error(tmp_path):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker

    class EmptyTransport:
        async def run(self, _node_id, _command, **_kwargs):
            return TerminalResult(returncode=1, output=b"Codex exited before submitting a result")

    node = Node(
        project_id=uuid.uuid4(),
        objective="Research the product",
        generated_prompt="Research the product and submit the result.",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )
    result = await CodexWorker(Settings(codex_binary="codex")).execute(
        NodeExecutionContext(node=node, repo_path=str(tmp_path), terminal=EmptyTransport())
    )

    assert result.outcome.value == "FAIL"
    assert result.summary == "Codex exited 1"
    assert result.error == "Codex exited with code 1"


class WaitingTransport:
    def __init__(self):
        self.released = asyncio.Event()
        self.stopped = False

    async def run(self, node_id, command, **kwargs):
        await self.released.wait()
        return TerminalResult(returncode=-15, output=b"native")

    async def stop(self, node_id):
        self.stopped = True
        self.released.set()
        return True


class AttachedHerdrHarness:
    supports_inject = True

    def __init__(self):
        self.injected: list[str] = []
        self.inject_events: list[str] = []
        self.closed = False
        self.released = asyncio.Event()
        self.session_done = asyncio.Event()
        self.process_start_path: Path | None = None
        self.process_exit_path: Path | None = None

    def snapshot(self, node_id):
        return {"active": True, "output": "Codex ready"}

    async def foreground_process_names(self, node_id):
        return ("codex",)

    async def close_persistent_session(self, node_id):
        self.closed = True
        return True

    async def ensure_session(self, node_id, **kwargs):
        try:
            await self.released.wait()
            return TerminalResult(returncode=0, output=b"")
        finally:
            self.session_done.set()

    async def inject_command(self, node_id, command, **kwargs):
        self.inject_events.extend(("send-keys:ctrl+c", "pane.run"))
        self.injected.append(command)
        markers = [
            quoted or bare
            for quoted, bare in re.findall(r">\s+(?:'([^']+)'|([^\s;)]+))", command)
            if (quoted or bare).endswith((".started", ".exit"))
        ]
        self.process_start_path = Path(next(path for path in markers if path.endswith(".started")))
        self.process_exit_path = Path(next(path for path in markers if path.endswith(".exit")))
        self.process_start_path.write_text("started")
        return True

    async def detach(self, node_id):
        self.released.set()
        return True

    async def stop(self, node_id):
        self.released.set()
        return True


async def test_active_node_pane_is_replaced_before_launching_new_command(tmp_path):
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = AttachedHerdrHarness()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "--resume", "saved-session", "new prompt"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
        )
    )
    await asyncio.sleep(0.05)
    result_path.write_text('{"outcome":"COMPLETE","summary":"done"}')
    assert transport.process_exit_path is not None
    transport.process_exit_path.write_text("0")
    await task
    transport.released.set()
    await transport.session_done.wait()
    assert transport.closed
    assert len(transport.injected) == 1
    assert "codex --resume saved-session 'new prompt'" in transport.injected[0]
    assert ".started" in transport.injected[0]
    assert ".exit" in transport.injected[0]


async def test_injected_harness_failure_releases_running_attempt(tmp_path):
    """A failed command in a durable shell must not leave Turn RUNNING forever."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = AttachedHerdrHarness()
    original_inject_command = transport.inject_command

    async def fail_injected_command(node_id, command, **kwargs):
        await original_inject_command(node_id, command, **kwargs)
        assert transport.process_exit_path is not None
        transport.process_exit_path.write_text("-32600")
        return True

    transport.inject_command = fail_injected_command
    result = await asyncio.wait_for(
        run_until_result(
            transport,
            node_id,
            ["codex", "resume", "already-active-writer"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
        ),
        timeout=1,
    )

    assert result.returncode == -32600
    assert transport.released.is_set()


async def test_successful_native_handoff_keeps_the_harness_attached(tmp_path):
    """A native TUI may stay open after submitting a successful handoff."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = AttachedHerdrHarness()
    cli_environment = os.environ.copy()
    cli_environment.update(
        {
            "TURN_HANDOFF_FILE": str(result_path),
            "TURN_STATUS_FILE": str(tmp_path / "agent.status.json"),
            "TURN_NODE_ID": str(node_id),
            "TURN_PROJECT_ID": str(uuid.uuid4()),
            "TURN_REPO": str(tmp_path),
        }
    )
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "native-turn"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
            environment=cli_environment,
        )
    )
    await asyncio.sleep(0.05)
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "turn", "agent", "submit", "--kind", "result", "--stdin"],
        input='{"outcome":"COMPLETE","summary":"done"}\n',
        cwd=str(tmp_path),
        env=cli_environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    result = await asyncio.wait_for(task, timeout=2)

    assert result.returncode == 0
    assert not transport.released.is_set()
    transport.released.set()
    await transport.session_done.wait()


async def test_file_handoff_completes_a_file_backed_attempt(tmp_path):
    """The persisted handoff file is the completion state for file-backed Turn."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = AttachedHerdrHarness()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "native-turn"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
            timeout=0.2,
        )
    )
    await asyncio.sleep(0.05)
    result_path.write_text('{"outcome":"COMPLETE","summary":"file-backed state"}')
    assert transport.process_exit_path is not None
    transport.process_exit_path.write_text("0")

    result = await task
    assert result.returncode == 0


async def test_process_handoff_does_not_hide_nonzero_exit_code(tmp_path):
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    exit_path = result_path.with_suffix(".exit")
    command = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            f"Path({str(result_path)!r}).write_text('{{\"outcome\":\"COMPLETE\"}}'); "
            f"Path({str(exit_path)!r}).write_text('7'); "
            "raise SystemExit(7)"
        ),
    ]
    result = await run_until_result(
        LocalPtyTransport(),
        node_id,
        command,
        cwd=str(tmp_path),
        result_path=result_path,
        process_exit_path=exit_path,
    )
    assert result.returncode == 7


async def test_injected_crash_after_process_start_returns_failure_without_exit_marker(tmp_path):
    """A provider control task ending must not leave a started launch polling forever."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    start_path = result_path.with_suffix(".started")

    class CrashedInjectedTransport:
        supports_inject = True

        def snapshot(self, _node_id):
            return {"active": True, "output": "shell ready"}

        async def ensure_session(self, _node_id, **_kwargs):
            start_path.write_text("started")
            return TerminalResult(returncode=17, output=b"harness crashed")

        async def inject_command(self, _node_id, _command, **_kwargs):
            return True

        async def detach(self, _node_id):
            return True

        async def stop(self, _node_id):
            return True

    result = await asyncio.wait_for(
        run_until_result(
            CrashedInjectedTransport(),
            node_id,
            ["crashed-harness"],
            cwd=str(tmp_path),
            result_path=result_path,
            process_start_path=start_path,
        ),
        timeout=1,
    )

    assert result.returncode == 17


async def test_codex_worker_rejects_a_valid_handoff_with_a_nonzero_exit(tmp_path, monkeypatch):
    """A crash after writing a result is still a failed harness attempt."""
    from turn.domain.schemas import AgentConfig, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker
    import turn.workers.codex_worker as codex_module

    async def crashed_run(_transport, _node_id, _command, **kwargs):
        kwargs["result_path"].write_text(
            '{"outcome":"COMPLETE","summary":"reported before crashing"}'
        )
        return TerminalResult(returncode=9, output=b"segmentation fault")

    monkeypatch.setattr(codex_module, "run_until_result", crashed_run)
    node = Node(
        project_id=uuid.uuid4(),
        objective="Crash after result",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )

    class CrashedTransport:
        supports_inject = True

        async def detach(self, _node_id):
            return True

        async def stop(self, _node_id):
            return True

    result = await CodexWorker().execute(
        NodeExecutionContext(node=node, repo_path=str(tmp_path), terminal=CrashedTransport())
    )

    assert result.outcome.value == "FAIL"
    assert "exited 9" in result.summary


async def test_injected_process_waits_for_handoff_after_control_task_ends(tmp_path):
    """A queued pane command must outlive its already-finished control task."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    start_path = result_path.with_suffix(".started")
    exit_path = result_path.with_suffix(".exit")

    class QueuedProcessTransport:
        supports_inject = True

        def __init__(self):
            self.writer = None
            self.detached = False

        def snapshot(self, _node_id):
            return {"active": True, "output": "shell ready"}

        async def ensure_session(self, _node_id, **_kwargs):
            # The provider's control stream can finish while pane.run is still
            # queued in the durable shell.
            return TerminalResult(returncode=0, output=b"")

        async def inject_command(self, _node_id, _command, **_kwargs):
            start_path.write_text("started")

            async def publish():
                await asyncio.sleep(0.05)
                result_path.write_text('{"outcome":"COMPLETE"}')
                await asyncio.sleep(0.05)
                exit_path.write_text("0")

            self.writer = asyncio.create_task(publish())
            return True

        async def detach(self, _node_id):
            self.detached = True
            return True

        async def stop(self, _node_id):
            if self.writer is not None:
                self.writer.cancel()
                await asyncio.gather(self.writer, return_exceptions=True)
            return True

    transport = QueuedProcessTransport()
    result = await run_until_result(
        transport,
        node_id,
        ["mock-launcher"],
        cwd=str(tmp_path),
        result_path=result_path,
        process_start_path=start_path,
        process_exit_path=exit_path,
        keep_attached=False,
        timeout=2,
    )

    assert result.returncode == 0
    assert transport.detached


class ShellAttachedHerdrHarness(AttachedHerdrHarness):
    async def foreground_process_names(self, node_id):
        return ("zsh",)


async def test_shell_pane_is_reused_for_a_first_native_launch(tmp_path):
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = ShellAttachedHerdrHarness()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "initial prompt"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
        )
    )
    await asyncio.sleep(0.05)
    result_path.write_text('{"outcome":"COMPLETE","summary":"done"}')
    assert transport.process_exit_path is not None
    transport.process_exit_path.write_text("0")
    await task
    transport.released.set()
    await transport.session_done.wait()
    assert not transport.closed
    assert len(transport.injected) == 1
    assert "codex 'initial prompt'" in transport.injected[0]


class GatedReadyHerdrHarness(AttachedHerdrHarness):
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()
        self.ready_called = asyncio.Event()

    async def wait_until_ready(self, node_id):
        self.ready_called.set()
        await self.ready.wait()


class TransientControlHerdrHarness(AttachedHerdrHarness):
    """A durable pane survives one controller disconnect before injection."""

    def __init__(self):
        super().__init__()
        self.attachments = 0

    async def has_persistent_session(self, _node_id):
        return True

    async def ensure_session(self, node_id, **kwargs):
        self.attachments += 1
        if self.attachments == 1:
            return TerminalResult(returncode=0, output=b"control handoff")
        return await super().ensure_session(node_id, **kwargs)

    async def wait_until_ready(self, _node_id):
        if self.attachments == 1:
            await asyncio.Event().wait()


async def test_native_launch_recovers_one_pre_injection_control_disconnect(tmp_path):
    """A fresh UI run must not fail when Herdr reconnects to a live pane."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = TransientControlHerdrHarness()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "initial prompt"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
        )
    )

    for _ in range(100):
        if transport.injected:
            break
        await asyncio.sleep(0.01)
    assert transport.attachments == 2
    assert len(transport.injected) == 1

    result_path.write_text('{"outcome":"COMPLETE","summary":"done"}')
    assert transport.process_exit_path is not None
    transport.process_exit_path.write_text("0")
    await task
    transport.released.set()
    await transport.session_done.wait()


async def test_native_launch_waits_for_provider_readiness_before_injection(tmp_path):
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = GatedReadyHerdrHarness()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "initial prompt"],
            cwd=str(tmp_path),
            result_path=result_path,
            harness_name="codex",
        )
    )

    await transport.ready_called.wait()
    assert transport.injected == []
    # This is the regression: the old implementation sent Ctrl-C as soon as
    # the control stream existed, which interrupted a shell before its prompt.
    assert transport.inject_events == []

    transport.ready.set()
    for _ in range(100):
        if transport.injected:
            break
        await asyncio.sleep(0.01)
    assert len(transport.injected) == 1
    assert "codex 'initial prompt'" in transport.injected[0]
    assert transport.inject_events == ["send-keys:ctrl+c", "pane.run"]

    result_path.write_text('{"outcome":"COMPLETE","summary":"done"}')
    assert transport.process_exit_path is not None
    transport.process_exit_path.write_text("0")
    await task
    transport.released.set()
    await transport.session_done.wait()


class RejectingHerdrLaunch:
    supports_inject = True

    def __init__(self):
        self.active = False
        self.stopped = False
        self.released = asyncio.Event()

    def snapshot(self, node_id):
        return {"active": self.active, "output": "shell ready" if self.active else ""}

    async def ensure_session(self, node_id, **kwargs):
        self.active = True
        await self.released.wait()
        return TerminalResult(returncode=0, output=b"")

    async def inject_command(self, node_id, command, **kwargs):
        return False

    async def stop(self, node_id):
        self.stopped = True
        self.active = False
        self.released.set()
        return True


async def test_native_launch_fails_when_herdr_rejects_prompt_injection(tmp_path):
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = RejectingHerdrLaunch()

    try:
        await run_until_result(
            transport,
            node_id,
            ["codex"],
            cwd=str(tmp_path),
            result_path=result_path,
        )
    except RuntimeError as error:
        assert str(error) == "Turn could not inject the harness command into Herdr"
    else:
        raise AssertionError("a rejected Herdr launch must fail instead of waiting forever")

    assert transport.stopped


async def test_injected_prompt_delivery_timeout_is_a_visible_launch_failure(tmp_path):
    """A blocked terminal-control write must not leave a node RUNNING."""
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    started = result_path.with_suffix(".started")

    class HungPromptTransport:
        supports_inject = True

        def __init__(self):
            self.control_task: asyncio.Task | None = None

        def snapshot(self, _node_id):
            return {"active": True, "output": "shell ready"}

        async def ensure_session(self, _node_id, **_kwargs):
            self.control_task = asyncio.current_task()
            await asyncio.Event().wait()
            raise AssertionError("the mocked control session should be cancelled")

        async def wait_until_ready(self, _node_id):
            return None

        async def inject_command(self, _node_id, _command, **_kwargs):
            started.write_text("started")
            return True

        async def foreground_process_names(self, _node_id):
            return ("mock-harness",)

        async def write(self, _node_id, _data):
            await asyncio.Event().wait()
            return True

        async def detach(self, _node_id):
            if self.control_task is not None:
                self.control_task.cancel()
            return True

        async def stop(self, _node_id):
            if self.control_task is not None:
                self.control_task.cancel()
            return True

    transport = HungPromptTransport()
    with pytest.raises(RuntimeError, match="did not acknowledge Turn prompt delivery"):
        await run_until_result(
            transport,
            node_id,
            ["mock-harness", "-"],
            cwd=str(tmp_path),
            result_path=result_path,
            initial_input="TURN_CONTEXT",
            initial_input_mode="stdin",
            input_delivery_timeout=0.01,
        )


async def test_headless_codex_launch_passes_the_prompt_as_an_argument(tmp_path, monkeypatch):
    """A machine transport gets JSONL automatically without a user mode."""
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker
    import turn.workers.codex_worker as codex_module

    captured: dict = {}
    telemetry = []

    async def collect(event):
        telemetry.append(event)

    async def fake_run(_transport, _node_id, command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        kwargs["result_path"].write_text('{"outcome":"COMPLETE","summary":"done"}')
        line = '{"type":"item.completed","item":{"details":{"type":"command_execution","command":"pytest","exit_code":0}}}\n'
        kwargs["machine_output_path"].write_text(line)
        await kwargs["machine_output_handler"](line)
        return TerminalResult(returncode=0, output=b"")

    monkeypatch.setattr(codex_module, "run_until_result", fake_run)

    class HerdrLikeTransport:
        supports_inject = True

    node = Node(
        project_id=uuid.uuid4(),
        objective="Build the app",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
        ),
    )
    result = await CodexWorker(Settings(codex_binary="codex")).execute(
        NodeExecutionContext(
            node=node,
            repo_path=str(tmp_path),
            terminal=HerdrLikeTransport(),
            telemetry=collect,
        )
    )

    assert result.outcome.value == "COMPLETE"
    assert captured["command"][-1] != "-"
    assert "TURN_CONTEXT" in captured["command"][-1]
    assert "initial_input" not in captured["kwargs"]
    assert callable(captured["kwargs"]["machine_output_handler"])
    assert {event.kind.value for event in telemetry} == {"command_end"}


async def test_native_session_stops_only_after_valid_project_file(tmp_path):
    node_id = uuid.uuid4()
    path = prepare_result_file(str(tmp_path), node_id, "result")
    transport = WaitingTransport()
    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex"],
            cwd=str(tmp_path),
            result_path=path,
            timeout=2,
        )
    )
    await asyncio.sleep(0.05)
    path.write_text('{"outcome":"COMPLETE","summary":"done"}')
    result = await task
    assert result.output == b"native"
    assert transport.stopped
    assert read_result_file(path) == {"outcome": "COMPLETE", "summary": "done"}


async def test_native_codex_session_id_is_observed_before_completion(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "14"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    session_id = "019fff3a-2c16-7143-8ec3-1f68bce525e7"
    session_file = session_dir / "rollout-2026-08-14T00-00-00-000000-019fff3a.jsonl"
    transport = WaitingTransport()

    async def run_with_session_file(node_id, command, **kwargs):
        session_file.write_text(json.dumps({
            "type": "session_meta",
            "payload": {"session_id": session_id, "cwd": str(tmp_path)},
        }) + "\n")
        return await WaitingTransport.run(transport, node_id, command, **kwargs)

    transport.run = run_with_session_file
    seen: list[str] = []

    async def record(session: str) -> None:
        seen.append(session)

    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex"],
            cwd=str(tmp_path),
            result_path=result_path,
            session_callback=record,
        )
    )
    await asyncio.sleep(0.1)
    result_path.write_text('{"outcome":"COMPLETE"}')
    await task
    assert seen == [session_id]


async def test_native_resume_keeps_its_known_session_when_another_rollout_appears(tmp_path, monkeypatch):
    """Concurrent workers in one repo must not steal each other's session id."""
    codex_home = tmp_path / "codex-home"
    session_dir = codex_home / "sessions" / "2026" / "08" / "14"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    node_id = uuid.uuid4()
    result_path = prepare_result_file(str(tmp_path), node_id, "result")
    (session_dir / "rollout-other.jsonl").write_text(json.dumps({
        "type": "session_meta",
        "payload": {"session_id": "other-worker-session", "cwd": str(tmp_path)},
    }) + "\n")
    transport = WaitingTransport()
    seen: list[str] = []

    async def record(session: str) -> None:
        seen.append(session)

    task = asyncio.create_task(
        run_until_result(
            transport,
            node_id,
            ["codex", "resume"],
            cwd=str(tmp_path),
            result_path=result_path,
            session_callback=record,
            known_session_id="saved-verifier-session",
        )
    )
    await asyncio.sleep(0.1)
    result_path.write_text('{"outcome":"COMPLETE"}')
    await task

    assert seen == []


async def test_codex_worker_native_path_round_trips_raw_ansi_and_result_file(tmp_path):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker

    binary = tmp_path / "mock-codex"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "root = pathlib.Path.cwd()\n"
        "print('\\x1b[2J\\x1b[HOpenAI Codex mock', flush=True)\n"
        "(root / 'native-smoke.txt').write_text('native PTY OK')\n"
        "result = pathlib.Path(os.environ['TURN_HANDOFF_FILE'])\n"
        "tmp = result.with_suffix('.tmp')\n"
        "tmp.write_text('{\"outcome\":\"COMPLETE\",\"summary\":\"native verified\",\"missing_inputs\":[]}')\n"
        "tmp.replace(result)\n"
        # Deliberately outlive the worker's configured timeout. Native sessions
        # must remain alive until the result file is submitted.
        "time.sleep(30)\n"
    )
    binary.chmod(binary.stat().st_mode | 0o111)
    node = Node(
        project_id=uuid.uuid4(),
        objective="Create a native smoke artifact",
        generated_prompt="Create and verify native-smoke.txt.",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
        ),
    )
    result_path = tmp_path / ".turn" / "interactive" / f"{node.id}.result.json"
    result = await CodexWorker(
        Settings(
            codex_binary=str(binary),
            codex_model="mock",
        )
    ).execute(NodeExecutionContext(node=node, repo_path=str(tmp_path), timeout_seconds=0.01))
    submission = next(a for a in result.artifacts if a.name == "result-submission")
    assert result.outcome.value == "COMPLETE", (result.summary, submission.content)
    assert (tmp_path / "native-smoke.txt").read_text() == "native PTY OK"
    assert "OpenAI Codex mock" not in json.dumps(submission.content)
    assert "\x1b[2J" not in json.dumps(submission.content)


async def test_codex_worker_verifier_normalizes_structured_evidence_refs(tmp_path, monkeypatch):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker
    import turn.workers.codex_worker as codex_module

    async def mock_run_until_result(_transport, _node_id, _command, **kwargs):
        kwargs["result_path"].write_text(json.dumps({
            "decision": "APPROVE",
            "summary": "native verifier accepted",
            "evidence_refs": [{
                "criterion_id": "journey",
                "status": "PASS",
                "summary": "The complete journey passed.",
                "refs": ["README.md"],
            }],
        }))
        return TerminalResult(returncode=0, output=b"")

    monkeypatch.setattr(codex_module, "run_until_result", mock_run_until_result)
    monkeypatch.setattr(codex_module, "schedule_late_sidecar_collection", lambda *_args, **_kwargs: None)
    node = Node(
        project_id=uuid.uuid4(),
        objective="Verify the native result",
        generated_prompt="Submit the verification.",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
            type_id=AgentType.VERIFIER,
        ),
    )

    result = await CodexWorker(Settings(codex_binary="codex", codex_model="mock")).execute(
        NodeExecutionContext(node=node, repo_path=str(tmp_path), terminal=LocalPtyTransport())
    )

    assert result.outcome.value == "COMPLETE"
    assert result.verification is not None
    assert result.verification.evidence[0].criterion_id == "journey"


async def test_herdr_transport_keeps_codex_in_native_interactive_mode(tmp_path, monkeypatch):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, HarnessKind, Node
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker
    import turn.workers.codex_worker as codex_module

    class HerdrLikeTransport(LocalPtyTransport):
        supports_inject = True

    captured: dict[str, object] = {}

    async def mock_run_until_result(_transport, _node_id, command, **kwargs):
        captured["command"] = command
        kwargs["result_path"].write_text(
            '{"outcome":"COMPLETE","summary":"interactive mode verified"}'
        )
        return TerminalResult(returncode=0, output=b"")

    monkeypatch.setattr(codex_module, "run_until_result", mock_run_until_result)
    monkeypatch.setattr(codex_module, "schedule_late_sidecar_collection", lambda *_args, **_kwargs: None)
    node = Node(
        project_id=uuid.uuid4(),
        objective="Keep the terminal interactive",
        generated_prompt="Submit the result.",
        repo_path=str(tmp_path),
        executor="codex",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )

    result = await CodexWorker(
        Settings(codex_binary="codex", codex_model="mock")
    ).execute(
        NodeExecutionContext(
            node=node,
            repo_path=str(tmp_path),
            terminal=HerdrLikeTransport(),
        )
    )

    assert result.outcome.value == "COMPLETE"
    command = captured["command"]
    assert command[:5] == [
        "codex",
        "-m",
        "mock",
        "-c",
        "project_root_markers=[]",
    ]
    assert command[command.index("--no-alt-screen") : command.index("-C")] == [
        "--no-alt-screen",
    ]
    assert command[command.index("-C") + 1] == str(tmp_path)
    assert "Submit the result." in command[-1]
