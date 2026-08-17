"""Mandatory server/DAG E2E coverage through the process-level fake harness."""
from __future__ import annotations

import asyncio
import uuid

import httpx
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.fake_workflows import fake_workflow_definitions, seed_fake_workflows
from turn.server.api import router
from turn.server.runtime import TurnRuntime
from turn.workers.terminal import LocalPtyTransport


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before the timeout")
        await asyncio.sleep(0.01)


async def _wait_for_terminal(queue: asyncio.Queue, node_id: uuid.UUID, needle: str) -> str:
    chunks: list[str] = []
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        while not queue.empty():
            event = queue.get_nowait()
            if event.get("type") == "node.terminal" and event.get("data", {}).get("node_id") == str(node_id):
                chunks.append(event["data"].get("chunk", ""))
        output = "".join(chunks)
        if needle in output:
            return output
        await asyncio.sleep(0.01)
    raise AssertionError(f"terminal output for {node_id} did not contain {needle!r}: {''.join(chunks)!r}")


async def _node(store: Store, node_id: uuid.UUID):
    value = await store.get_node(node_id)
    assert value is not None
    return value


async def _run_and_wait(
    client: httpx.AsyncClient,
    store: Store,
    node_id: uuid.UUID,
    statuses: set[str],
) -> None:
    initial_runs = _run_count(store, node_id)
    response = await client.post(f"/api/nodes/{node_id}/run")
    assert response.status_code == 200, response.text
    assert response.json()["ran"] is not None, response.text
    try:
        await _wait_for(
            lambda: (
                _run_count(store, node_id) > initial_runs
                and _status(store, node_id) in statuses
                and _latest_run_status(store, node_id) != "RUNNING"
            )
        )
    except AssertionError as error:
        found = store._project_for_node(node_id)
        runs = list(store._states[found[0]]["runs"].values()) if found else []
        raise AssertionError(
            f"node {node_id} did not reach {statuses}; status={_status(store, node_id)}, "
            f"runs={[run.model_dump(mode='json') for run in runs if run.node_id == node_id]}"
        ) from error


def _status(store: Store, node_id: uuid.UUID) -> str | None:
    node = store._project_for_node(node_id)
    return node[1].status.value if node else None


def _run_count(store: Store, node_id: uuid.UUID) -> int:
    found = store._project_for_node(node_id)
    if not found:
        return 0
    project_id, _ = found
    return sum(run.node_id == node_id for run in store._states[project_id]["runs"].values())


def _latest_run_status(store: Store, node_id: uuid.UUID) -> str | None:
    found = store._project_for_node(node_id)
    if not found:
        return None
    project_id, _ = found
    runs = [run for run in store._states[project_id]["runs"].values() if run.node_id == node_id]
    return runs[-1].status.value if runs else None


def _project_by_title(projects, title: str):
    return next(project for project in projects if project.project_name == title)


async def test_fake_process_workflow_lab_covers_core_state_machine_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TURN_FAKE_WORKFLOWS", "1")
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        planner="fake",
        default_executor="fake",
        runner_tick_seconds=0.01,
        default_run_timeout_seconds=10,
        stall_timeout_seconds=10,
    )
    store = Store(settings.data_dir, projects_dir=settings.projects_dir)
    terminal = LocalPtyTransport()
    runtime = TurnRuntime(
        settings,
        store=store,
        terminal_transport=terminal,
        test_mode=True,
    )
    components = await runtime.start()
    app = FastAPI()
    app.include_router(router)
    app.state.store = components.store
    app.state.runner = components.runner
    app.state.events = components.events
    app.state.test_mode = True
    app.state.capabilities = components.capabilities
    events = components.events.subscribe()

    try:
        created = await seed_fake_workflows(store)
        assert created == []  # runtime startup already loaded the test lab
        assert len(await store.list_projects()) == len(fake_workflow_definitions()) == 6
        projects = await store.list_projects()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            rejection = _project_by_title(projects, "Fake · reject and return")
            rejection_nodes = (await store.get_workgraph(rejection.id))[0]
            work = next(node for node in rejection_nodes if node.objective == "Build the reviewable change")
            review = next(node for node in rejection_nodes if node.objective.startswith("Reject the change"))
            release = next(node for node in rejection_nodes if node.objective == "Publish the accepted change")
            await _run_and_wait(client, store, work.id, {"COMPLETE"})
            work_session = (await _node(store, work.id)).agent.session_id
            assert work_session and work_session.startswith("fake-")
            await _run_and_wait(client, store, review.id, {"PENDING", "RUNNABLE", "BLOCKED"})
            await _wait_for_terminal(events, work.id, "fake-turn: resumed session")
            refreshed = (await client.get(f"/api/projects/{rejection.id}/graph")).json()
            assert next(node for node in refreshed["nodes"] if node["id"] == str(work.id))["status"] == "RUNNABLE"
            release_view = next(node for node in refreshed["nodes"] if node["id"] == str(release.id))
            assert release_view["ui_state"] == "waiting_dependency"
            assert not ({"run", "retry", "regenerate"} & set(release_view["allowed_actions"]))
            assert next(node for node in refreshed["nodes"] if node["id"] == str(work.id))["agent"]["session_id"] == work_session
            assert any(
                edge["src"] == str(review.id) and edge["dst"] == str(work.id)
                for edge in refreshed["flow_edges"]
            )

            expansion = _project_by_title(projects, "Fake · graph expansion")
            expand = (await store.children_of(expansion.id))[0]
            await _run_and_wait(client, store, expand.id, {"EXPANDED"})
            expanded_nodes = await store.descendants(expand.id)
            assert {node.objective for node in expanded_nodes} == {
                "Complete expanded part A",
                "Complete expanded part B",
            }
            expanded_edges = (await store.get_workgraph(expansion.id))[1]
            assert any(edge.src == expanded_nodes[0].id or edge.dst == expanded_nodes[0].id for edge in expanded_edges)

            rerun = _project_by_title(projects, "Fake · rerun replaces outputs")
            reusable = (await store.children_of(rerun.id))[0]
            await _run_and_wait(client, store, reusable.id, {"COMPLETE"})
            assert [artifact.name for artifact in await store.get_artifacts(reusable.id)] == ["first-pass"]
            regenerated = await client.post(f"/api/nodes/{rerun.id}/regenerate")
            assert regenerated.status_code == 200, regenerated.text
            new_children = await store.children_of(rerun.id)
            assert len(new_children) == 1 and new_children[0].id != reusable.id
            assert await store.get_artifacts(new_children[0].id) == []
            await _run_and_wait(client, store, new_children[0].id, {"COMPLETE"})
            assert len(await store.get_artifacts(new_children[0].id)) == 1

            failure = _project_by_title(projects, "Fake · failure and retry")
            retryable = (await store.children_of(failure.id))[0]
            await _run_and_wait(client, store, retryable.id, {"RUNNABLE"})
            assert (await store.get_runs(retryable.id))[0].status.value == "FAILED"
            await _run_and_wait(client, store, retryable.id, {"COMPLETE"})
            assert len(await store.get_runs(retryable.id)) == 2

            blocked = _project_by_title(projects, "Fake · block and provide input")
            decision = (await store.children_of(blocked.id))[0]
            await _run_and_wait(client, store, decision.id, {"BLOCKED"})
            decision = await _node(store, decision.id)
            assert decision.required_inputs and decision.required_inputs[0].id == "choice"
            supplied = await client.post(
                f"/api/nodes/{decision.id}/provide-input",
                json={"input_id": "choice", "value": "path-a"},
            )
            assert supplied.status_code == 200, supplied.text
            await _run_and_wait(client, store, decision.id, {"COMPLETE"})

            cancelled = _project_by_title(projects, "Fake · stop and skip")
            long_task, dependent = await store.children_of(cancelled.id)
            started = await client.post(f"/api/nodes/{long_task.id}/run")
            assert started.status_code == 200, started.text
            await _wait_for(lambda: _status(store, long_task.id) == "RUNNING")
            stopped = await client.post(f"/api/nodes/{long_task.id}/cancel")
            assert stopped.status_code == 200, stopped.text
            await _wait_for(lambda: _status(store, long_task.id) == "CANCELLED")
            assert not await store.get_runs(dependent.id)
    finally:
        components.events.unsubscribe(events)
        await runtime.stop()
