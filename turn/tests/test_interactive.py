from __future__ import annotations

import asyncio
import base64
import json
import uuid

from turn.workers.interactive import (
    prepare_result_file,
    read_result_file,
    result_handoff,
    run_until_result,
)
from turn.tests.fakes import FakeHerdrAdapter
from turn.workers.terminal import HerdrPtyTransport, TerminalResult


async def test_injected_command_clears_partial_shell_input(tmp_path, monkeypatch):
    transport = HerdrPtyTransport(str(tmp_path), adapter=FakeHerdrAdapter())
    sent: list[dict] = []

    async def capture(_node_id, command):
        sent.append(command)
        return True

    monkeypatch.setattr(transport, "_send_control", capture)
    await transport.inject_command(
        uuid.uuid4(), "codex --model test", environment={"TURN_PROJECT_ID": "project"}
    )

    raw = base64.b64decode(sent[0]["bytes"])
    assert raw == b"\x03\renv TURN_PROJECT_ID=project codex --model test\r"


def test_agent_handoff_prompt_uses_only_cli_payload_submission():
    for plan in (False, True):
        prompt = result_handoff(plan=plan)
        assert "agent submit" in prompt
        assert "--payload '<JSON_OBJECT>'" in prompt
        assert "filesystem output as a protocol" in prompt
        assert "TURN_HANDOFF_FILE" not in prompt
        assert "TURN_STATUS_FILE" not in prompt
        assert "--file" not in prompt
        assert "--stdin" not in prompt
        assert ".turn/" not in prompt


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


async def test_codex_worker_native_path_round_trips_raw_ansi_and_result_file(tmp_path):
    from turn.config import Settings
    from turn.domain.schemas import AgentConfig, HarnessKind, Node, PermissionMode
    from turn.workers.base import NodeExecutionContext
    from turn.workers.codex_worker import CodexWorker

    binary = tmp_path / "fake-codex"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        "root = pathlib.Path.cwd()\n"
        "print('\\x1b[2J\\x1b[HOpenAI Codex fake', flush=True)\n"
        "(root / 'native-smoke.txt').write_text('native PTY OK')\n"
        "result = pathlib.Path(next(token for token in sys.argv if token.endswith('.result.json')))\n"
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
            permission=PermissionMode.FULL,
        ),
    )
    result_path = tmp_path / ".turn" / "interactive" / f"{node.id}.result.json"
    result = await CodexWorker(
        Settings(
            codex_binary=str(binary),
            codex_model="fake",
            codex_args=[str(result_path)],
        )
    ).execute(NodeExecutionContext(node=node, repo_path=str(tmp_path), timeout_seconds=0.01))
    transcript = next(a.content for a in result.artifacts if a.name == "transcript")
    assert result.outcome.value == "COMPLETE", (result.summary, transcript)
    assert (tmp_path / "native-smoke.txt").read_text() == "native PTY OK"
    assert "OpenAI Codex fake" in transcript and "\x1b[2J" in transcript
