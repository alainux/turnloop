"""End-to-end user journey with Herdr persistence and deterministic Echo work."""
from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, HarnessKind, NodeStatus, RunPolicy
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.echo_worker import EchoWorker
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry


async def _await_run(runner: Runner, node_id) -> None:
    task = runner._running.get(node_id)
    assert task is not None
    await asyncio.wait_for(task, timeout=10)


async def test_herdr_echo_user_journey_exercises_supported_actions(tmp_path):
    if shutil.which("herdr") is None:
        pytest.skip("Herdr is required for Turn terminal sessions")
    project_root = Path(__file__).resolve().parents[2] / "projects"
    settings = Settings(
        data_dir=f"/private/tmp/turn-echo-journey-{uuid.uuid4().hex[:8]}",
        projects_dir=str(project_root),
        default_executor="echo",
        planner="heuristic",  # explicit deterministic test fixture only
    )
    store = Store(settings.data_dir)
    await store.init()
    registry = WorkerRegistry()
    registry.register(EchoWorker())
    registry.register_planner(HeuristicPlanner("echo"))
    runner = Runner(store, registry, EventBus(), settings)
    agent = AgentConfig(harness=HarnessKind.ECHO, type_id="planner")
    root = await store.create_project(
        "Build an echo-backed puzzle-room demo",
        repo_path=str(project_root / "echo-room"),
        agent=agent,
        run_policy=RunPolicy(auto_run=False),
    )
    try:
        # Creation allocates a durable Herdr shell. Opening, resizing, and
        # detaching it must preserve the same node session.
        assert await runner.ensure_node_terminal(root.id)
        assert await runner.terminal.has_persistent_session(root.id)
        assert await runner.open_shell(root.id)
        assert await runner.shell.resize(root.id, 100, 32)
        assert await runner.detach_shell(root.id)
        assert await runner.terminal.has_persistent_session(root.id)

        # Manual Run in Step mode plans the root, then Run executes an Echo
        # leaf. This covers the same actions exposed by the graph and inspector.
        assert await runner.run_node(root.id) == root.id
        await _await_run(runner, root.id)
        nodes, _, _ = await store.get_workgraph(root.id)
        leaves = [node for node in nodes if node.parent_id == root.id and node.executor == "echo"]
        assert len(leaves) >= 2
        # Planning does not allocate idle panes for every future node. A
        # worker gets a Herdr pane when it actually runs (or when a user opens
        # that node's terminal).
        assert not any(
            await asyncio.gather(
                *(runner.terminal.has_persistent_session(node.id) for node in leaves)
            )
        )

        # Editing a planner's selected harness cascades to its active branch.
        await runner.edit_node(root.id, agent=agent.model_copy(deep=True))
        updated_leaves = await asyncio.gather(*(store.get_node(node.id) for node in leaves))
        assert all(node is not None and node.agent.harness == HarnessKind.ECHO for node in updated_leaves)

        # Step/Auto are reversible policy choices, not hidden execution modes.
        await runner.set_mode(root.id, True)
        assert (await store.get_node(root.id)).run_policy.auto_run is True
        await runner.set_mode(root.id, False)
        assert (await store.get_node(root.id)).run_policy.auto_run is False

        first, second = leaves[:2]
        assert await runner.run_node(first.id) == first.id
        await _await_run(runner, first.id)
        assert (await store.get_node(first.id)).status == NodeStatus.COMPLETE
        assert await runner.terminal.has_persistent_session(first.id)

        # Cancel is explicit and a cancelled node can be Run again.
        await runner.cancel(second.id)
        assert (await store.get_node(second.id)).status == NodeStatus.CANCELLED
        assert await runner.run_node(second.id) == second.id
        await _await_run(runner, second.id)
        assert (await store.get_node(second.id)).status == NodeStatus.COMPLETE
        assert await runner.terminal.has_persistent_session(second.id)

        # Planner "Run again" replaces descendants and closes their removed
        # Herdr panes rather than leaving stray terminals behind.
        old_panes = [runner.terminal.pane_id(node.id) for node in (first, second)]
        result = await runner.regenerate_descendants(root.id, fresh_session=True)
        assert result["created"]
        removed_sessions = await asyncio.gather(
            *(runner.terminal.has_persistent_session(node.id) for node in leaves)
        )
        assert not any(removed_sessions)
        assert all(pane is not None for pane in old_panes)
        root = await store.get_node(root.id)
        root.project_name = "Renamed echo journey"
        root.objective = root.project_name
        await store._save_node(root)
        assert (await store.get_node(root.id)).project_name == "Renamed echo journey"
    finally:
        nodes, _, _ = await store.get_workgraph(root.id)
        for node in nodes:
            await runner.close_shell(node.id)
        await runner.stop()
        await store.delete_project(root.id)
        await store.dispose()
