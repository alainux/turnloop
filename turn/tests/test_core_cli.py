from __future__ import annotations

from turn.__main__ import parser
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


async def test_headless_run_explicitly_drives_a_manual_project(tmp_path):
    cfg = Settings()
    cfg.database_url = f"sqlite+aiosqlite:///{tmp_path / 'core.db'}"
    cfg.projects_dir = str(tmp_path / "projects")
    cfg.planner = "heuristic"
    cfg.default_executor = "echo"
    cfg.runner_tick_seconds = 0.001
    async with TurnCore(cfg) as core:
        project = await core.create_project(
            "Create a compact deterministic demo",
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=False),
        )
        nodes = await core.run_until_settled(project.id, max_rounds=100)
        root = next(node for node in nodes if node.id == project.id)
        assert root.auto_run is True
        assert not any(node.status.value in {"PENDING", "RUNNABLE", "RUNNING"} for node in nodes)
        assert any(node.status.value == "BLOCKED" for node in nodes)
