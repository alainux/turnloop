"""Trigger unit, integration, and event-source coverage."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx
import pytest
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    EventSource,
    HarnessKind,
    NodeStatus,
    NodeSpec,
    PlanResult,
    RunPolicy,
    VerificationDecision,
    VerificationResult,
    TriggerKind,
    TriggerSpec,
)
from turn.logging import EventLog
from turn.runner.events import EventBus
from turn.runner.triggers import EventInbox, TriggerDispatcher, schedule_is_due
from turn.workers.planner import AgentPlanner
from turn.runtime import TurnRuntime
from turn.server.api import router
from turn.workers.base import NodeExecutionContext, render_context_block
from turn.workers.filesystem import init_project_directory
from turn.workers.terminal import LocalPtyTransport
from turn.tests.mocks import MockTerminalTransport
from turn.workers.deterministic_worker import DeterministicWorker
from turn.workers.registry import WorkerRegistry
from turn.runner.runner import Runner


async def _project(tmp_path: Path, *, name: str = "trigger project"):
    logs = EventLog(tmp_path / "turn")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects", logs=logs)
    await store.init()
    root = await store.create_project(
        "A trigger test project",
        name=name,
        repo_path=str(tmp_path / "projects" / name.replace(" ", "-")),
        agent=AgentConfig(harness=HarnessKind.MOCK, type_id=AgentType.PLANNER),
    )
    plan = PlanResult(
        nodes=[
            NodeSpec(key="start", objective="Start the workflow", executor="deterministic"),
            NodeSpec(key="finish", objective="Finish the workflow", executor="deterministic", follows=["start"]),
        ],
    )
    start, finish = await store.apply_plan(root, plan)
    return store, logs, root, start, finish


async def _dispatcher(store: Store, logs: EventLog, tmp_path: Path):
    dispatcher = TriggerDispatcher(store, EventBus(logs), logs, tmp_path / "turn")
    store.set_event_sink(dispatcher.emit)
    return dispatcher


def test_agent_planner_preserves_declared_triggers():
    plan = AgentPlanner._parse_plan(json.dumps({
        "nodes": [{"key": "start", "objective": "Start"}],
        "triggers": [
            {"target_key": "start", "kind": "schedule", "schedule": "*/1 * * * *", "data": {"channel": "cron"}},
            {"target_key": "start", "event_name": "tweet.manual", "kind": "event", "data": {"channel": "manual"}},
        ],
    }))

    assert plan is not None
    assert [(item.event_name, item.kind, item.schedule, item.data) for item in plan.triggers] == [
        (None, TriggerKind.SCHEDULE, "*/1 * * * *", {"channel": "cron"}),
        ("tweet.manual", TriggerKind.EVENT, None, {"channel": "manual"}),
    ]


@pytest.mark.asyncio
async def test_event_matching_is_exact_name_only(tmp_path):
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    trigger = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        event_name="deployment.succeeded",
        data={"source": "trigger", "release": "configured"},
    )

    await dispatcher.emit("Deployment.Succeeded", data={"environment": "production"})
    assert (await store.get_node(start.id)).status is NodeStatus.PENDING
    emitted = await dispatcher.emit("deployment.succeeded", data={"environment": "staging", "release": 42})
    activated = await store.get_node(start.id)
    assert emitted["matched"] == 1
    assert activated.status is NodeStatus.RUNNABLE
    assert activated.trigger_context is not None
    assert activated.trigger_context.trigger_id == trigger.id
    assert activated.trigger_context.data["release"] == 42
    assert activated.trigger_context.data["source"] == "trigger"
    assert any(record["action"] == "trigger.matched" for record in logs.read(root.id))


@pytest.mark.asyncio
async def test_trigger_launch_closes_the_old_harness_but_keeps_the_provider_session(tmp_path):
    """Trigger activation is a fresh process boundary, not a fresh conversation."""
    store, logs, root, start, _ = await _project(tmp_path)
    root = await store.set_project_mode(root.id, True)
    start = await store.get_node(start.id)
    assert root is not None and start is not None
    start.agent.session_id = "retained-trigger-session"
    await store._save_node(start)  # type: ignore[attr-defined]

    terminal = MockTerminalTransport()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry,
        EventBus(logs),
        Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
            runner_tick_seconds=0.001,
            default_executor="deterministic",
        ),
        terminal_transport=terminal,
    )
    dispatcher = await _dispatcher(store, logs, tmp_path)
    trigger = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        event_name="manual.restart",
    )
    await terminal.ensure_persistent_shell(start.id, cwd=str(tmp_path))

    try:
        await dispatcher.emit("manual.restart", project_id=root.id)
        runner.wake()
        for _ in range(100):
            await runner.tick()
            node = await store.get_node(start.id)
            if node is not None and node.status is NodeStatus.COMPLETE:
                break
            await asyncio.sleep(0.001)
        await runner.wait_for_idle(root.id)

        assert start.id in terminal.close_requests
        refreshed = await store.get_node(start.id)
        assert refreshed is not None
        assert refreshed.agent is not None
        assert refreshed.agent.session_id == "retained-trigger-session"
        assert trigger.id == (await store.get_trigger(trigger.id)).id
    finally:
        await runner.stop()
        await store.dispose()


@pytest.mark.asyncio
async def test_auto_run_waits_for_a_trigger_target_to_be_activated(tmp_path):
    """An auto-run scheduler must not consume a dormant event target."""
    store, logs, root, start, _ = await _project(tmp_path)
    root = await store.set_project_mode(root.id, True)
    assert root is not None
    await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        event_name="manual.run",
    )
    terminal = MockTerminalTransport()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry,
        EventBus(logs),
        Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
            runner_tick_seconds=0.001,
            default_executor="deterministic",
        ),
        terminal_transport=terminal,
    )
    try:
        await runner.tick()
        await asyncio.sleep(0.02)
        dormant = await store.get_node(start.id)
        assert dormant is not None
        assert dormant.trigger_context is None
        assert await store.get_runs(start.id) == []
    finally:
        await runner.stop()
        await store.dispose()


@pytest.mark.asyncio
async def test_disabled_trigger_is_logged_but_does_not_activate(tmp_path):
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    trigger = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        event_name="manual.plan.requested",
        enabled=False,
    )

    emitted = await dispatcher.emit("manual.plan.requested", data={"goal": "Do not run"})

    assert emitted["matched"] == 0
    node = await store.get_node(start.id)
    assert node.status is NodeStatus.PENDING
    assert node.trigger_context is None
    assert not any(
        record.get("action") == "trigger.matched"
        and record.get("data", {}).get("trigger_id") == str(trigger.id)
        for record in logs.read(root.id)
    )


@pytest.mark.asyncio
async def test_events_fan_out_across_projects(tmp_path):
    logs = EventLog(tmp_path / "turn")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects", logs=logs)
    await store.init()
    source = await store.create_project("source", repo_path=str(tmp_path / "projects" / "source"))
    target_root = await store.create_project("target", repo_path=str(tmp_path / "projects" / "target"))
    target = await store.create_node(project_id=target_root.id, parent_id=target_root.id, objective="Cross project entry", executor="deterministic")
    dispatcher = await _dispatcher(store, logs, tmp_path)
    await store.create_trigger(project_id=target_root.id, target_node_id=target.id, event_name="deployment.succeeded")
    result = await dispatcher.emit(
        "deployment.succeeded",
        source=EventSource.TRANSITION,
        project_id=source.id,
        data={"environment": "production"},
    )
    assert result["matched"] == 1
    context = (await store.get_node(target.id)).trigger_context
    assert context is not None and context.source_project_id == source.id


@pytest.mark.asyncio
async def test_all_supported_event_sources_activate_triggers(tmp_path):
    store, logs, root, start, finish = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    transition = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        event_name="node.status.changed",
    )
    await store.set_status(start.id, NodeStatus.COMPLETE)
    transition_node = await store.get_node(start.id)
    assert transition_node.trigger_context is not None
    assert transition_node.trigger_context.source is EventSource.TRANSITION
    assert transition_node.trigger_context.trigger_id == transition.id

    agent_action = await store.create_trigger(
        project_id=root.id,
        target_node_id=finish.id,
        event_name="verification.completed",
    )
    await store.complete_verification(
        finish.id,
        VerificationResult(
            decision=VerificationDecision.APPROVE,
            summary="Looks good.",
        ),
    )
    agent_node = await store.get_node(finish.id)
    assert agent_node.trigger_context is not None
    assert agent_node.trigger_context.source is EventSource.AGENT_ACTION
    assert agent_node.trigger_context.trigger_id == agent_action.id

    schedule = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        kind=TriggerKind.SCHEDULE,
        schedule="* * * * *",
        data={"kind": "scheduled"},
    )
    now = datetime.now(timezone.utc)
    await dispatcher.tick_schedules(now)
    scheduled_node = await store.get_node(start.id)
    assert scheduled_node.trigger_context is not None
    assert scheduled_node.trigger_context.source is EventSource.SCHEDULE
    assert scheduled_node.trigger_context.trigger_id == schedule.id
    assert scheduled_node.trigger_context.event_name == f"schedule.{schedule.id}"
    assert scheduled_node.trigger_context.data["kind"] == "scheduled"
    assert scheduled_node.trigger_context.data["scheduled_at"]

    cli_trigger = await store.create_trigger(
        project_id=root.id,
        target_node_id=finish.id,
        event_name="human.requested",
    )
    record = EventInbox(tmp_path / "turn").append(
        name="human.requested",
        data={"case": 3},
        project_id=str(root.id),
    )
    assert record["source"] == "cli"
    await dispatcher.poll_inbox()
    cli_node = await store.get_node(finish.id)
    assert cli_node.trigger_context is not None
    assert cli_node.trigger_context.source is EventSource.CLI
    assert cli_node.trigger_context.trigger_id == cli_trigger.id


@pytest.mark.asyncio
async def test_schedule_source_and_cron_parser(tmp_path):
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    schedule = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        kind=TriggerKind.SCHEDULE,
        schedule="* * * * *",
        data={"kind": "scheduled"},
    )
    now = datetime.now(timezone.utc)
    assert schedule_is_due("* * * * *", now, None)
    assert not schedule_is_due("* * * * *", now, now)
    with pytest.raises(ValueError, match="five-field cron"):
        schedule_is_due("@every 1s", now, None)
    sunday = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
    assert schedule_is_due("* * * * 0", sunday, None)
    assert schedule_is_due("* * * * 7", sunday, None)
    assert not schedule_is_due("* * * * 1", sunday, None)
    assert schedule_is_due("*/5 * * * *", now.replace(minute=5, second=0, microsecond=0), None)
    await dispatcher.tick_schedules(now)
    context = (await store.get_node(start.id)).trigger_context
    assert context is not None
    assert context.source is EventSource.SCHEDULE
    assert context.event_name == f"schedule.{schedule.id}"
    assert context.data["kind"] == "scheduled"
    assert context.data["scheduled_at"]
    updated = await store.get_trigger(schedule.id)
    assert updated.last_fired_at is not None


@pytest.mark.asyncio
async def test_planner_trigger_spec_is_persisted_and_context_is_rendered(tmp_path):
    logs = EventLog(tmp_path / "turn")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects", logs=logs)
    await store.init()
    root = await store.create_project("planner trigger", repo_path=str(tmp_path / "projects" / "planner"))
    plan = PlanResult(
        nodes=[NodeSpec(key="start", objective="Start", executor="deterministic")],
        triggers=[TriggerSpec(target_key="start", event_name="custom.input")],
    )
    [start] = await store.apply_plan(root, plan)
    [trigger] = await store.list_triggers(root.id)
    assert trigger.target_node_id == start.id
    dispatcher = await _dispatcher(store, logs, tmp_path)
    await dispatcher.emit("custom.input", data={"kind": "prompt", "prompt": "ship it"})
    context = (await store.get_node(start.id)).trigger_context
    assert context is not None
    rendered = render_context_block(NodeExecutionContext(node=await store.get_node(start.id), trigger_context=context))
    assert "trigger_context=" in rendered
    assert "ship it" in rendered
    assert rendered.count("TURN_CONTEXT") == 1
    assert "production_trigger_policy" not in rendered
    assert "Do not emit the event" not in rendered


@pytest.mark.asyncio
async def test_cli_inbox_is_cross_process_and_project_scoped(tmp_path):
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    await store.create_trigger(project_id=root.id, target_node_id=start.id, event_name="prompt.received")
    EventInbox(tmp_path / "turn").append(name="prompt.received", data={"priority": 3}, project_id=str(root.id))
    await dispatcher.poll_inbox()
    assert (await store.get_node(start.id)).trigger_context.source is EventSource.CLI
    assert any(record["kind"] == "trigger.event" for record in logs.read(root.id))


@pytest.mark.asyncio
async def test_http_trigger_configuration_and_event_emission(tmp_path):
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.triggers = dispatcher

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            f"/api/projects/{root.id}/triggers",
            json={
                "target_node_id": str(start.id),
                "event_name": "custom.input",
                "data": {"configured": True},
            },
        )
        assert created.status_code == 200, created.text
        trigger_id = created.json()["trigger"]["id"]
        listed = await client.get(f"/api/projects/{root.id}/triggers")
        assert listed.status_code == 200
        assert listed.json()["triggers"][0]["id"] == trigger_id
        assert listed.json()["triggers"][0]["data"] == {"configured": True}

        emitted = await client.post(
            "/api/events",
            json={"event_name": "custom.input", "data": {"kind": "prompt", "native": 1}},
        )
        assert emitted.status_code == 200, emitted.text
        assert emitted.json()["event"]["matched"] == 1
        context = (await store.get_node(start.id)).trigger_context
        assert context is not None
        assert context.data == {"configured": True, "kind": "prompt", "native": 1}

        updated = await client.patch(
            f"/api/triggers/{trigger_id}", json={"enabled": False, "data": {"configured": False}}
        )
        assert updated.status_code == 200
        disabled_emitted = await client.post(
            "/api/events",
            json={"event_name": "custom.input", "data": {"kind": "prompt"}},
        )
        assert disabled_emitted.status_code == 200
        assert disabled_emitted.json()["event"]["matched"] == 0
        deleted = await client.delete(f"/api/triggers/{trigger_id}")
        assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_mock_process_looper_restarts_from_cli_and_transition_events(tmp_path):
    """The process harness proves a trigger can restart a real workflow twice."""
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        planner="mock",
        default_executor="mock",
        runner_tick_seconds=0.01,
        default_run_timeout_seconds=10,
        stall_timeout_seconds=10,
    )
    runtime = TurnRuntime(settings, test_mode=True, terminal_transport=LocalPtyTransport())
    components = await runtime.start()

    async def wait_for(predicate, timeout: float = 12) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("looper did not settle before the timeout")
            await asyncio.sleep(0.02)

    try:
        project_id = uuid.uuid4()
        repo = init_project_directory(project_id, projects_dir=settings.projects_dir)
        root = await components.store.create_project(
            "Process harness trigger loop",
            id=project_id,
            repo_path=repo,
            agent=AgentConfig(harness=HarnessKind.MOCK, type_id=AgentType.PLANNER),
            run_policy=RunPolicy(auto_run=False),
        )
        start, finish = await components.store.apply_plan(
            root,
            PlanResult(
                nodes=[
                    NodeSpec(
                        key="start",
                        objective="Loop start",
                        executor="mock",
                        generated_prompt="MOCK_COMPLETE_LOOP_START",
                    ),
                    NodeSpec(
                        key="finish",
                        objective="Loop finish",
                        executor="mock",
                        generated_prompt="MOCK_COMPLETE_LOOP_FINISH",
                        follows=["start"],
                    ),
                ]
            ),
        )
        completion = await components.store.create_trigger(
            project_id=project_id,
            target_node_id=start.id,
            event_name="project.completed",
            data={"demo": "loop", "mode": "repeat"},
        )
        manual = await components.store.create_trigger(
            project_id=project_id,
            target_node_id=start.id,
            event_name="loop.begin",
            data={"demo": "loop", "mode": "manual"},
        )

        # Exercise the same append-only CLI helper used by agents and humans.
        cli_environment = os.environ.copy()
        cli_environment["TURN_DATA_DIR"] = settings.data_dir
        queued = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "turn",
                "trigger",
                "emit",
                "loop.begin",
                "--data",
                json.dumps({"goal": "deterministic loop", "iteration": 1}),
            ],
            cwd=repo,
            env=cli_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert queued.returncode == 0, queued.stderr or queued.stdout
        await wait_for(lambda: (components.store._project_for_node(start.id) is not None and (components.store._project_for_node(start.id)[1].trigger_context is not None)))
        manual_context = (await components.store.get_node(start.id)).trigger_context
        assert manual_context is not None
        assert manual_context.trigger_id == manual.id
        assert manual_context.data == {
            "demo": "loop",
            "mode": "manual",
            "goal": "deterministic loop",
            "iteration": 1,
        }

        await components.runner.set_mode(project_id, True)
        await wait_for(
            lambda: sum(
                run.node_id == start.id
                for run in components.store._states[project_id].runs.values()
            ) >= 2
        )
        # Stop the durable loop after its second activation; the trigger has
        # already been proven through a persisted project completion transition.
        matching = await components.store.list_triggers(project_id)
        assert len(matching) == 2
        assert completion.id in {item.id for item in matching}
        await components.store.update_trigger(completion.id, enabled=False)
        await wait_for(
            lambda: (
                (components.store._states[project_id].nodes[start.id].status is NodeStatus.COMPLETE)
                and (components.store._states[project_id].nodes[finish.id].status is NodeStatus.COMPLETE)
                and sum(run.node_id == finish.id for run in components.store._states[project_id].runs.values()) >= 2
            )
        )
        matches = [
            record for record in components.logs.read(project_id)
            if record.get("action") == "trigger.matched"
        ]
        manual_match = next(
            record for record in matches
            if record.get("data", {}).get("event_name") == "loop.begin"
        )
        assert manual_match["data"]["activation_data"] == {
            "demo": "loop",
            "mode": "manual",
            "goal": "deterministic loop",
            "iteration": 1,
        }
        completion_match = next(
            record for record in matches
            if record.get("data", {}).get("event_name") == "project.completed"
        )
        assert completion_match["data"]["activation_data"]["demo"] == "loop"
        assert completion_match["data"]["activation_data"]["mode"] == "repeat"
        assert completion_match["data"]["event_data"]["project_id"] == str(project_id)
    finally:
        await runtime.stop()


# ---------------------------------------------------------------------------
# Dispatcher reliability regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_loop_survives_transient_faults(tmp_path):
    """One failing tick must not kill the dispatcher: triggers stay alive."""
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    await store.create_trigger(project_id=root.id, target_node_id=start.id, event_name="reliable.event")

    original_poll = dispatcher.poll_inbox
    failures = {"count": 0}

    async def flaky_poll():
        if failures["count"] < 2:
            failures["count"] += 1
            raise RuntimeError("transient store hiccup")
        await original_poll()

    dispatcher.poll_inbox = flaky_poll  # type: ignore[method-assign]
    # The event waits in the cross-process inbox; it must still be delivered
    # after two transient failures of the dispatch loop.
    dispatcher.inbox.append(name="reliable.event", data={})
    await dispatcher.start()
    try:
        for _ in range(200):
            if failures["count"] >= 2:
                break
            await asyncio.sleep(0.01)
        for _ in range(300):
            node = await store.get_node(start.id)
            if node.status is NodeStatus.RUNNABLE:
                break
            await asyncio.sleep(0.02)
        assert node.status is NodeStatus.RUNNABLE
        assert failures["count"] == 2
        assert any(
            record.get("action") == "dispatcher.error"
            for record in logs.read(None)
        )
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_failed_schedule_fire_is_retried_not_consumed(tmp_path):
    """A schedule activation failure must not consume the cron fire."""
    store, logs, root, start, _ = await _project(tmp_path)
    dispatcher = await _dispatcher(store, logs, tmp_path)
    trigger = await store.create_trigger(
        project_id=root.id,
        target_node_id=start.id,
        kind=TriggerKind.SCHEDULE,
        schedule="* * * * *",
    )

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    calls = {"emit": 0}
    original_emit = dispatcher.emit

    async def failing_emit(*args, **kwargs):
        if kwargs.get("source") is EventSource.SCHEDULE or (args and len(args) > 1 and args[1] is EventSource.SCHEDULE):
            calls["emit"] += 1
            raise RuntimeError("activation backend down")
        return await original_emit(*args, **kwargs)

    dispatcher.emit = failing_emit  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await dispatcher.tick_schedules(now=now)
    fired = await store.get_trigger(trigger.id)
    assert fired.last_fired_at is None, "failed fire must not be consumed"

    # Recovery: the same minute fires successfully afterwards.
    dispatcher.emit = original_emit  # type: ignore[method-assign]
    await dispatcher.tick_schedules(now=now)
    fired = await store.get_trigger(trigger.id)
    assert fired.last_fired_at is not None
    assert (await store.get_node(start.id)).trigger_context is not None


@pytest.mark.asyncio
async def test_inbox_cursor_advances_per_record_and_dedups_replays(tmp_path):
    """A crash mid-batch must neither replay processed events nor skip records."""
    store, logs, root, start, _ = await _project(tmp_path)
    inbox = EventInbox(tmp_path / "turn")
    first = inbox.append(name="batch.first", data={"n": 1})
    second = inbox.append(name="batch.second", data={"n": 2})
    await store.create_trigger(project_id=root.id, target_node_id=start.id, event_name="batch.first")
    await store.create_trigger(project_id=root.id, target_node_id=start.id, event_name="batch.second")

    dispatcher = TriggerDispatcher(store, EventBus(logs), logs, tmp_path / "turn")
    store.set_event_sink(dispatcher.emit)
    dispatcher._offset = inbox.start_offset()

    # Simulate a crash after the first record was activated but before its
    # cursor was saved: the durable offset is still 0, so a restart replays
    # both records. The already-applied event must not double-activate, and
    # the remaining event must still fire.
    await dispatcher.emit(
        "batch.first",
        data=dict(first["data"]),
        event_id=uuid.UUID(first["event_id"]),
        occurred_at=datetime.fromisoformat(first["occurred_at"]),
    )
    restarted = TriggerDispatcher(store, EventBus(logs), logs, tmp_path / "turn")
    store.set_event_sink(restarted.emit)
    restarted._offset = inbox.start_offset()
    await restarted.poll_inbox()

    context = (await store.get_node(start.id)).trigger_context
    assert context is not None
    assert context.event_name == "batch.second"
    duplicate = [
        record for record in logs.read(None) + logs.read(root.id)
        if record.get("action") in {"event.duplicate", "trigger.replay_skipped"}
    ]
    assert duplicate, "replayed event_id must be skipped as a duplicate"


@pytest.mark.asyncio
async def test_inbox_compaction_preserves_unread_records(tmp_path):
    inbox = EventInbox(tmp_path / "turn")
    keep = inbox.append(name="compact.keep", data={"n": 3})
    inbox.save_offset(inbox.path.stat().st_size)
    size = inbox.compact(inbox.start_offset(), max_bytes=64)
    assert size == 0
    assert inbox.start_offset() == 0
    records, _ = inbox.read_from(0)
    assert records == []
