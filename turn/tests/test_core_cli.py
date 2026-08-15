from __future__ import annotations

import io
import json

import pytest

from turn.__main__ import parser
from turn.__main__ import discover_project_state
from turn.config import Settings
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, HarnessKind, RunPolicy


def test_cli_exposes_headless_commands_and_policy_flags():
    parsed = parser().parse_args(
        ["create", "build it", "--harness", "pi", "--reasoning", "high", "--manual", "--run"]
    )
    assert parsed.command == "create"
    assert parsed.harness == "pi" and parsed.reasoning == "high"
    assert parsed.manual and parsed.run
    assert parser().parse_args(["doctor"]).command == "doctor"
    assert parser().parse_args(["serve", "--port", "9000"]).port == 9000


def test_graph_cli_discovers_the_nearest_parent_project_state(tmp_path):
    project = tmp_path / "project"
    nested = project / "src" / "package"
    nested.mkdir(parents=True)
    state = project / ".turn" / "state.json"
    state.parent.mkdir()
    state.write_text("{}")

    assert discover_project_state(nested) == state

    nearer = nested / ".turn" / "state.json"
    nearer.parent.mkdir()
    nearer.write_text("{}")
    assert discover_project_state(nested) == nearer


async def test_headless_run_explicitly_drives_a_manual_project(tmp_path):
    cfg = Settings()
    cfg.data_dir = str(tmp_path / "turn")
    cfg.projects_dir = str(tmp_path / "projects")
    cfg.planner = "heuristic"
    cfg.default_executor = "echo"
    cfg.runner_tick_seconds = 0.001
    async with TurnCore(cfg, test_mode=True) as core:
        project = await core.create_project(
            "Create a compact deterministic demo",
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=False),
        )
        nodes = await core.run_until_settled(project.id, max_rounds=100)
        root = next(node for node in nodes if node.id == project.id)
        assert root.auto_run is True
        assert not any(node.status.value in {"PENDING", "RUNNABLE", "RUNNING"} for node in nodes)
        assert all(node.status.value == "COMPLETE" for node in nodes)


def test_agent_cli_writes_atomic_status_and_result_handoffs(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.result.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node-1")

    args = parser().parse_args([
        "agent", "status", "--state", "working", "--message", "editing files"
    ])
    assert agent_command(args) == 0
    assert json.loads(status.read_text()) == {
        "node_id": "node-1", "state": "working", "message": "editing files"
    }

    args = parser().parse_args([
        "agent", "submit", "--kind", "result",
        "--stdin",
    ])
    monkeypatch.setattr("sys.stdin", io.StringIO('{"outcome":"COMPLETE","summary":"done","artifacts":["src"]}'))
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text()) == {
        "outcome": "COMPLETE", "summary": "done", "artifacts": ["src"]
    }
    assert json.loads(status.read_text())["state"] == "complete"


def test_agent_cli_rejects_json_that_does_not_match_the_turn_contract(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.result.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))

    args = parser().parse_args([
        "agent", "submit", "--kind", "result",
        "--payload", '{"outcome":"COMPLETE","summary":42}',
    ])
    with pytest.raises(SystemExit, match="invalid result submission"):
        agent_command(args)
    assert not handoff.exists()

    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"a","objective":"A","depends_on":["b"]},'
            '{"key":"b","objective":"B","depends_on":["a"]}],"edges":[]}'
        ),
    ])
    with pytest.raises(SystemExit, match="invalid plan submission"):
        agent_command(args)
