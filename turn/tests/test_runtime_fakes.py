from __future__ import annotations

import pytest

from turn.testing.fakes import FakeHerdrAdapter
from turn.workers.herdr import HerdrResourceNotFound


async def test_offline_runtime_fake_covers_workspace_and_pane_lifecycle():
    adapter = FakeHerdrAdapter()
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


def test_offline_runtime_fake_rejects_terminal_control_commands():
    adapter = FakeHerdrAdapter()
    with pytest.raises(AssertionError):
        adapter.terminal_control_command("pane")
