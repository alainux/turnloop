from __future__ import annotations

import uuid

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, NodeStatus, RunStatus
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
from turn.workers.herdr import HerdrAgent
from turn.workers.terminal import HerdrPtyTransport


class LiveAgentAdapter(MockHerdrAdapter):
    def __init__(self):
        super().__init__()
        self.live_agents: list[HerdrAgent] = []

    async def list_agents(self) -> tuple[HerdrAgent, ...]:
        return tuple(self.live_agents)


@pytest.mark.asyncio
async def test_stale_pane_map_repairs_to_live_native_session_without_relaunch(tmp_path):
    adapter = LiveAgentAdapter()
    transport = HerdrPtyTransport(str(tmp_path / "turn"), adapter=adapter)
    node_id = uuid.uuid4()
    project_id = "project-recovery"
    await transport.ensure_persistent_shell(
        node_id,
        cwd=str(tmp_path),
        environment={"TURN_PROJECT_ID": project_id},
    )
    workspace_id = transport.project_workspace_id(project_id)
    assert workspace_id is not None
    old_pane = transport.pane_id(node_id)
    new_pane = (await adapter.create_tab(workspace_id=workspace_id, cwd=str(tmp_path), label="recovered")).pane_id
    adapter.panes.pop(old_pane, None)
    adapter.live_agents = [
        HerdrAgent("agent-target", pane_id=new_pane, agent_session="session-S", provider="codex")
    ]

    assert await transport.reconcile_provider_session(
        node_id,
        project_key=project_id,
        session_id="session-S",
        provider="codex",
    )
    assert transport.pane_id(node_id) == new_pane
    assert await transport.has_persistent_session(node_id)


@pytest.mark.asyncio
async def test_runner_recovery_preserves_same_run_after_stale_pane(tmp_path):
    data_dir = tmp_path / "turn"
    projects_dir = tmp_path / "projects"
    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "live restart recovery",
        repo_path=str(projects_dir / "recovery"),
        agent=AgentConfig(harness="codex"),
    )
    await store.set_agent_session(root.id, "session-S")
    await store.set_status(root.id, NodeStatus.RUNNING)
    run = await store.create_run(root, "codex")
    adapter = LiveAgentAdapter()
    transport = HerdrPtyTransport(str(data_dir), adapter=adapter)
    await transport.ensure_persistent_shell(
        root.id,
        cwd=str(projects_dir / "recovery"),
        environment={"TURN_PROJECT_ID": str(root.id)},
    )
    workspace_id = transport.project_workspace_id(str(root.id))
    old_pane = transport.pane_id(root.id)
    new_pane = (await adapter.create_tab(workspace_id=workspace_id, cwd=str(projects_dir / "recovery"), label="restart")).pane_id
    adapter.panes.pop(old_pane, None)
    adapter.live_agents = [HerdrAgent("root", pane_id=new_pane, agent_session="session-S", provider="codex")]

    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(data_dir=str(data_dir), projects_dir=str(projects_dir)),
        herdr_adapter=adapter,
        terminal_transport=transport,
    )
    await runner._recover_external_runs()
    assert runner._recovered_run_ids[root.id] == run.id
    assert (await store.get_runs(root.id))[0].status is RunStatus.RUNNING
    assert (await store.get_node(root.id)).status is NodeStatus.RUNNING
    await store.dispose()
