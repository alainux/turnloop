from __future__ import annotations

import pytest

from turn.config import Settings
from turn.server.runtime import TurnRuntime
from turn.testing.mocks import MockHerdrAdapter
from turn.tests.mocks import MockTerminalTransport
from turn.workers.herdr import HerdrResourceNotFound


async def test_offline_runtime_mock_covers_workspace_and_pane_lifecycle():
    adapter = MockHerdrAdapter()
    created = await adapter.create_workspace(cwd="/tmp/project", label="fixture")
    workspace = await adapter.get_workspace(created.workspace_id)
    assert workspace.workspace_id == created.workspace_id
    pane = await adapter.create_tab(
        workspace_id=workspace.workspace_id,
        cwd="/tmp/project",
        label="child",
    )
    assert await adapter.send_keys(pane.pane_id, ("hello",))
    assert await adapter.run_command(pane.pane_id, "true")
    assert await adapter.wait_for_output(pane.pane_id, regex="hello", lines=1)
    assert await adapter.read_pane(pane.pane_id) == ""
    assert len(await adapter.list_workspaces()) == 1
    assert await adapter.close_pane(pane.pane_id)
    assert await adapter.close_workspace(workspace.workspace_id)
    assert not await adapter.close_workspace(workspace.workspace_id)
    with pytest.raises(HerdrResourceNotFound):
        await adapter.get_workspace(workspace.workspace_id)


def test_offline_runtime_mock_rejects_terminal_control_commands():
    adapter = MockHerdrAdapter()
    with pytest.raises(AssertionError):
        adapter.terminal_control_command("pane")


async def test_runtime_cleans_up_services_when_startup_fails(tmp_path, monkeypatch):
    async def fail_seed(_store):
        raise RuntimeError("seed failed")

    monkeypatch.setattr("turn.runtime.harness_capabilities", lambda *_args: [])
    monkeypatch.setattr("turn.runtime.mock_workflows_enabled", lambda: True)
    monkeypatch.setattr("turn.runtime.seed_mock_workflows", fail_seed)
    runtime = TurnRuntime(
        Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
            planner="mock",
            default_executor="mock",
        ),
        test_mode=True,
        terminal_transport=MockTerminalTransport(),
    )

    with pytest.raises(RuntimeError, match="seed failed"):
        await runtime.start()

    assert runtime.runner is None
    assert runtime.triggers._task is None
    assert runtime._started is False
