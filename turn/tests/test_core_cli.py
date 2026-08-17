from __future__ import annotations

import io
import json

import pytest

from turn.__main__ import parser
from turn.__main__ import discover_project_state
from turn.config import Settings
from turn.core import TurnCore
from turn.domain.schemas import AgentConfig, HarnessKind, RunPolicy
from turn.tests.fakes import FakeHerdrAdapter, FakeTerminalTransport


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
    async with TurnCore(
        cfg,
        test_mode=True,
        herdr_adapter=FakeHerdrAdapter(),
        terminal_transport=FakeTerminalTransport(),
    ) as core:
        project = await core.create_project(
            "Create a compact deterministic demo",
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=False),
        )
        from turn.skills.library import SKILLS, install_builtin_skill

        for skill_id in SKILLS:
            install_builtin_skill(skill_id, project.repo_path)
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


def test_agent_cli_rejects_a_plan_skill_missing_from_the_project(tmp_path, monkeypatch):
    from turn.__main__ import agent_command

    handoff = tmp_path / "node.plan.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"work","objective":"Work",'
            '"skills":["project:visual-qa"]}]}'
        ),
    ])

    with pytest.raises(SystemExit, match="project:visual-qa.*not installed"):
        agent_command(args)
    assert not handoff.exists()


def test_agent_cli_requires_builtin_skill_files_in_the_project(tmp_path, monkeypatch):
    from turn.__main__ import agent_command
    from turn.skills.library import install_builtin_skill

    handoff = tmp_path / "node.plan.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args([
        "agent", "submit", "--kind", "plan",
        "--payload", (
            '{"nodes":[{"key":"work","objective":"Work",'
            '"skills":["imagegen"]}]}'
        ),
    ])

    install_builtin_skill("imagegen", tmp_path)
    install_builtin_skill("turn-executing", tmp_path)
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text())["nodes"][0]["skills"] == ["imagegen"]


async def test_skills_cli_installs_a_builtin_into_turn_repo(tmp_path, monkeypatch):
    from turn.__main__ import async_main

    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    args = parser().parse_args(["skills", "install", "imagegen"])

    assert await async_main(args) == 0
    assert (tmp_path / ".turn" / "skills" / "imagegen" / "SKILL.md").is_file()
