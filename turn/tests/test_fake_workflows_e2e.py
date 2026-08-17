"""Mandatory server/DAG E2E coverage through the process-level fake harness."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import httpx
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, HarnessKind, RunPolicy
from turn.fake_workflows import fake_workflow_definitions, seed_fake_workflows
from turn.server.api import router
from turn.server.runtime import TurnRuntime
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.terminal import LocalPtyTransport
from turn.workers.harness_catalog import HarnessCommandFactory


class InjectableLocalPtyTransport(LocalPtyTransport):
    """Process-level E2E transport with the production editability contract."""

    supports_inject = True

    def __init__(self):
        super().__init__()
        self.closed_nodes = []
        self.close_results = []
        self.persistent_tasks = {}

    async def ensure_session(self, node_id, **kwargs):
        task = asyncio.current_task()
        self.persistent_tasks[node_id] = task
        try:
            return await self.run(node_id, ["sh", "-i"], **kwargs)
        finally:
            self.persistent_tasks.pop(node_id, None)

    async def inject_command(self, node_id, command, *, environment=None):
        del environment  # The shell already received the launch environment.
        return await self.write(node_id, command + "\n")

    async def close_persistent_session(self, node_id):
        self.closed_nodes.append(node_id)
        task = self.persistent_tasks.get(node_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        result = await super().close_persistent_session(node_id)
        self.close_results.append((node_id, result))
        return result


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
            part_a = next(node for node in expanded_nodes if node.objective == "Complete expanded part A")
            part_b = next(node for node in expanded_nodes if node.objective == "Complete expanded part B")
            await _run_and_wait(client, store, part_a.id, {"COMPLETE"})
            await _run_and_wait(client, store, part_b.id, {"COMPLETE"})

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


async def test_process_e2e_revises_plan_rejects_work_and_cleans_project(tmp_path):
    """Exercise the user-facing edit/rejection/delete path in one graph."""
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
    terminal = InjectableLocalPtyTransport()
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

    project_id: uuid.UUID | None = None
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/projects",
                json={
                    "name": "Editable process E2E",
                    "prompt": "Build a tiny reviewable workflow",
                    "working_dir": str(tmp_path / "project"),
                    "agent": {
                        "harness": "fake",
                        "model": "deterministic",
                        "type_id": "planner",
                    },
                    "run_policy": {"auto_run": False},
                },
            )
            assert created.status_code == 200, created.text
            project_id = uuid.UUID(created.json()["project_id"])
            root = await _node(store, project_id)
            assert root.repo_path is not None

            initial_plan_path = Path(root.repo_path) / ".turn" / "fake-plan.json"
            initial_plan_path.write_text(
                json.dumps({
                    "nodes": [
                        {
                            "key": "build",
                            "objective": "Build the reviewable change",
                            "executor": "fake",
                            "generated_prompt": "FAKE_COMPLETE_REVIEWABLE",
                        },
                        {
                            "key": "review",
                            "objective": "Reject the change and return it to build",
                            "executor": "fake",
                            "agent_type": "verifier",
                            "generated_prompt": "FAKE_VERIFY_REJECT",
                            "depends_on": ["build"],
                        },
                    ]
                }),
                encoding="utf-8",
            )
            edited = await client.post(
                f"/api/nodes/{project_id}/edit",
                json={"resource_refs": [str(initial_plan_path)]},
            )
            assert edited.status_code == 200, edited.text

            await _run_and_wait(client, store, project_id, {"EXPANDED"})
            build, review = await store.children_of(project_id)
            assert review.agent is not None
            assert review.agent.type_id.value == "verifier"

            await _run_and_wait(client, store, build.id, {"COMPLETE"})
            await _run_and_wait(client, store, review.id, {"PENDING", "RUNNABLE", "BLOCKED"})
            refreshed_review = await _node(store, review.id)
            assert refreshed_review.verification is not None
            assert refreshed_review.verification.decision.value == "REJECT"
            assert (await _node(store, build.id)).status.value == "RUNNABLE"

            # This is the exact CLI handoff used by a retained planner session,
            # rather than a direct Runner/store call.
            root = await _node(store, project_id)
            assert root.repo_path is not None and root.agent is not None
            plan_path = Path(root.repo_path) / ".turn" / "interactive" / f"{project_id}.plan.json"
            status_path = plan_path.with_name(f"{project_id}.status.json")
            root.agent_state = "failed"
            root.agent_message = "stale rejection from the previous plan"
            await store._save_node(root)
            payload = json.dumps({
                "project_name": "Editable process E2E",
                "nodes": [
                    {
                        "key": "chapter_plan",
                        "objective": "Plan the tiny chapters",
                        "executor": "fake",
                        "agent_type": "planner",
                        "plan": True,
                    },
                    {
                        "key": "chapter_a",
                        "objective": "Write chapter A",
                        "executor": "fake",
                        "depends_on": ["chapter_plan"],
                    },
                    {
                        "key": "chapter_b",
                        "objective": "Write chapter B",
                        "executor": "fake",
                        "depends_on": ["chapter_plan"],
                    },
                    {
                        "key": "verify_chapters",
                        "objective": "Verify both chapters",
                        "executor": "fake",
                        "agent_type": "verifier",
                        "depends_on": ["chapter_a", "chapter_b"],
                    },
                ],
            })
            environment = os.environ.copy()
            environment.update({
                "TURN_NODE_ID": str(project_id),
                "TURN_PROJECT_ID": str(project_id),
                "TURN_REPO": root.repo_path,
                "TURN_HANDOFF_FILE": str(plan_path),
                "TURN_STATUS_FILE": str(status_path),
                "TURN_AGENT_SKILL_IDS": "turn-planning,find-skills,find-mcps,turn-setup",
            })
            submitted = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "turn", "agent", "submit", "--kind", "plan", "--stdin"],
                input=payload,
                text=True,
                cwd=root.repo_path,
                env=environment,
                capture_output=True,
                check=False,
            )
            assert submitted.returncode == 0, submitted.stderr or submitted.stdout

            expected_objectives = {
                "Plan the tiny chapters", "Write chapter A", "Write chapter B", "Verify both chapters"
            }
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                revised = await store.descendants(project_id)
                if {node.objective for node in revised} == expected_objectives:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError(
                        f"planner edit did not persist: {[node.objective for node in revised]}"
                    )
                await asyncio.sleep(0.01)
            revised_root = await _node(store, project_id)
            assert revised_root.status.value == "EXPANDED"
            assert revised_root.agent_state is None
            assert revised_root.agent_message is None

            fan_in = next(node for node in revised if node.objective == "Verify both chapters")
            prerequisites = await store.prerequisites(fan_in.id)
            assert {node.objective for node in prerequisites} == {"Write chapter A", "Write chapter B"}

            deleted = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": False, "delete_conversations": False},
            )
            assert deleted.status_code == 200, deleted.text
            assert await store.get_node(project_id) is None
            assert project_id in terminal.closed_nodes
            assert not await terminal.has_persistent_session(project_id), {
                "sessions": terminal.sessions,
                "close_results": terminal.close_results,
            }
            assert Path(root.repo_path).exists()
            project_id = None
    finally:
        if project_id is not None and await store.get_node(project_id) is not None:
            await components.runner.cancel_project_runs(project_id)
            await components.runner.close_project_workspace(project_id)
            await store.delete_project(project_id)
        await runtime.stop()


async def test_api_delete_e2e_archives_then_tty_deletes_provider_session(tmp_path, monkeypatch):
    """Exercise the destructive conversation cleanup through the API boundary."""
    log_path = tmp_path / "provider-actions.log"
    provider = tmp_path / "codex-fixture"
    provider.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$1\" >> \"$TURN_PROVIDER_LOG\"\n"
        "if [ \"$1\" = delete ]; then\n"
        "  [ -t 0 ] || exit 2\n"
        "  read answer\n"
        "  [ \"$answer\" = y ] || exit 3\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    provider.chmod(0o755)
    monkeypatch.setenv("TURN_PROVIDER_LOG", str(log_path))

    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="codex",
        planner="codex",
    )
    store = Store(settings.data_dir, projects_dir=settings.projects_dir)
    await store.init()
    root = await store.create_project(
        "Provider cleanup E2E",
        repo_path=str(tmp_path / "project"),
        agent=AgentConfig(
            harness=HarnessKind.CODEX,
            session_id="provider-session-1",
        ),
        run_policy=RunPolicy(auto_run=False),
    )
    root = await store.set_agent_session(root.id, "provider-session-1") or root
    runner = Runner(
        store,
        events=EventBus(),
        settings=settings,
        terminal_transport=LocalPtyTransport(),
    )
    runner.harness_commands = HarnessCommandFactory(codex_binary=str(provider))
    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.runner = runner
    app.state.events = runner.events
    app.state.test_mode = True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            deleted = await client.request(
                "DELETE",
                f"/api/projects/{root.id}",
                json={"delete_files": False, "delete_conversations": True},
            )
            assert deleted.status_code == 200, deleted.text
            cleanup = deleted.json()["conversation_cleanup"]
            assert cleanup == {
                "total": 1,
                "deleted": 1,
                "archived": 1,
                "failed": 0,
                "unsupported": 0,
                "errors": [],
            }
            assert log_path.read_text(encoding="utf-8").splitlines() == ["archive", "delete"]
            assert await store.get_node(root.id) is None
            assert Path(root.repo_path).exists()
    finally:
        await runner.stop()
        await store.dispose()
