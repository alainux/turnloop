"""Compact adversarial lifecycle invariants from P0-P1-HARDEN."""
from __future__ import annotations

import uuid

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    HarnessKind,
    NodeStatus,
    Outcome,
    ProcessState,
    WorkerResult,
)
from turn.runner.events import EventBus
from turn.runner.process_supervisor import ProcessSupervisor
from turn.runner.runner import Runner
from turn.server.api import _serialize_graph
from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
from turn.workers.terminal import HerdrPtyTransport


async def _runner(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Lifecycle hardening fixture",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    node = await store.create_node(
        project_id=root.id,
        parent_id=None,
        objective="Execute one attempt",
        executor="mock",
        agent=AgentConfig(harness=HarnessKind.MOCK),
        status=NodeStatus.RUNNING,
    )
    # The test node is intentionally a second root-like node in the same
    # project state; Runner only needs a node/run pair for the authority gate.
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    )
    return store, runner, root, node


@pytest.mark.asyncio
async def test_accepted_submission_wins_over_late_process_exit(tmp_path):
    store, runner, root, node = await _runner(tmp_path)
    run = await store.create_run(node, "mock")
    await runner._handle_outcome(
        node,
        run,
        root.id,
        WorkerResult(outcome=Outcome.COMPLETE, summary="accepted"),
    )
    await store.mark_run_process(run.id, ProcessState.EXITED, exit_code=9)

    saved_node = await store.get_node(node.id)
    saved_run = await store.get_run(run.id)
    assert saved_node is not None and saved_node.status is NodeStatus.COMPLETE
    assert saved_run is not None
    assert saved_run.status.value == "COMPLETE"
    assert saved_run.accepted_submission is True
    assert saved_run.process_exit_code == 9


@pytest.mark.asyncio
async def test_stale_run_cannot_settle_retry(tmp_path):
    store, runner, root, node = await _runner(tmp_path)
    run_a = await store.create_run(node, "mock")
    await store.set_status(node.id, NodeStatus.RUNNING)
    await runner._handle_outcome(
        node,
        run_a,
        root.id,
        WorkerResult(outcome=Outcome.FAIL, summary="retry me", retry_recommended=True),
    )
    await store.set_status(node.id, NodeStatus.RUNNABLE)
    run_b = await store.create_run(node, "mock", attempt=2)
    await store.set_status(node.id, NodeStatus.RUNNING)

    await runner._handle_outcome(
        node,
        run_b,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="late Run A",
            run_id=run_a.id,
        ),
    )

    current_node = await store.get_node(node.id)
    current_run = await store.get_run(run_b.id)
    assert current_node is not None and current_node.status is NodeStatus.RUNNING
    assert current_run is not None and current_run.status.value == "RUNNING"
    assert current_run.accepted_submission is False


@pytest.mark.asyncio
async def test_invalid_submission_is_correction_on_same_run(tmp_path):
    store, runner, root, node = await _runner(tmp_path)
    run = await store.create_run(node, "mock")
    await runner._reject_submission(node, "missing required evidence")

    saved_node = await store.get_node(node.id)
    saved_run = await store.get_run(run.id)
    assert saved_node is not None
    assert saved_node.status is NodeStatus.RUNNING
    assert saved_node.agent_state == "correction_required"
    assert saved_run is not None and saved_run.status.value == "RUNNING"
    assert saved_run.accepted_submission is False


def test_control_plane_context_cannot_select_local_pty():
    source = open("turn/runner/runner.py", encoding="utf-8").read()
    start = source.index("def _review_context")
    end = source.index("async def _run_semantic_plan_audit", start)
    assert '"terminal": None' not in source[start:end]


@pytest.mark.asyncio
async def test_control_run_persists_and_projects_its_interactable_terminal_owner(tmp_path):
    store, runner, root, _ = await _runner(tmp_path)
    terminal_owner = uuid.uuid4()
    run = await store.create_run(
        root,
        "organization-manager",
        process_owner_id=terminal_owner,
    )
    await store.mark_run_process(run.id, ProcessState.RUNNING, pane_id="w-control:p1")

    saved = await store.get_run(run.id)
    assert saved is not None and saved.process_owner_id == terminal_owner

    graph = await _serialize_graph(store, root.id, runner)
    activity = next(item for item in graph["nodes"] if item["id"] == str(root.id))["control_activity"]
    assert activity["run_id"] == str(run.id)
    assert activity["terminal_node_id"] == str(terminal_owner)


@pytest.mark.asyncio
async def test_process_inventory_tracks_control_owner_without_a_graph_node(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Control inventory fixture",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    terminal = HerdrPtyTransport(
        str(tmp_path / "state"),
        adapter=MockHerdrAdapter(),
    )
    owner = uuid.uuid4()
    await terminal.ensure_persistent_shell(
        owner,
        cwd=str(tmp_path / "repo"),
        environment={"TURN_PROJECT_ID": str(root.id)},
    )
    run = await store.create_run(
        root,
        "organization-manager",
        process_owner_id=owner,
    )
    await store.mark_run_process(run.id, ProcessState.RUNNING)

    records = await ProcessSupervisor(store, terminal).inventory(root.id)
    control = next(record for record in records if record.node_id == owner)
    assert control.run_id == run.id
    assert control.provider == "mock"
    assert control.live is False

    assert await ProcessSupervisor(store, terminal).close_all(root.id) >= 1


@pytest.mark.asyncio
async def test_project_inventory_and_cleanup_never_touch_another_projects_control_pane(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    first = await store.create_project(
        "First project",
        repo_path=str(tmp_path / "first"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    second = await store.create_project(
        "Second project",
        repo_path=str(tmp_path / "second"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    terminal = HerdrPtyTransport(str(tmp_path / "state"), adapter=MockHerdrAdapter())
    first_owner, second_owner = uuid.uuid4(), uuid.uuid4()
    for project, owner in ((first, first_owner), (second, second_owner)):
        await terminal.ensure_persistent_shell(
            owner,
            cwd=project.repo_path or str(tmp_path),
            environment={"TURN_PROJECT_ID": str(project.id)},
        )
        run = await store.create_run(
            project,
            "organization-manager",
            process_owner_id=owner,
        )
        await store.mark_run_process(run.id, ProcessState.RUNNING)

    supervisor = ProcessSupervisor(store, terminal)
    first_records = await supervisor.inventory(first.id)
    assert first_owner in {record.node_id for record in first_records}
    assert second_owner not in {record.node_id for record in first_records}

    await supervisor.close_all(first.id)
    assert terminal.pane_id(first_owner) is None
    assert terminal.pane_id(second_owner) is not None


@pytest.mark.asyncio
async def test_cancelling_control_run_stops_its_synthetic_owner_before_cancelled_is_visible(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Control cancellation fixture",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    await store.set_status(root.id, NodeStatus.EXPANDED)
    adapter = MockHerdrAdapter()
    terminal = HerdrPtyTransport(str(tmp_path / "state"), adapter=adapter)
    owner = uuid.uuid4()
    await terminal.ensure_persistent_shell(
        owner,
        cwd=str(tmp_path / "repo"),
        environment={"TURN_PROJECT_ID": str(root.id)},
    )
    pane = terminal.pane_id(owner)
    run = await store.create_run(root, "organization-manager", process_owner_id=owner)
    await store.mark_run_process(run.id, ProcessState.RUNNING, pane_id=pane)
    await store.set_status(root.id, NodeStatus.RUNNING)
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(data_dir=str(tmp_path / "state")),
        terminal_transport=terminal,
    )

    await runner.cancel(root.id)

    saved_run = await store.get_run(run.id)
    saved_node = await store.get_node(root.id)
    assert saved_run is not None and saved_run.status.value == "CANCELLED"
    assert saved_node is not None and saved_node.status is NodeStatus.CANCELLED
    assert pane not in adapter.panes
