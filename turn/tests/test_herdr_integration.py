"""Real Herdr contract test for Turn's project-space lifecycle."""
from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server.api import router
from turn.workers.echo_worker import EchoWorker
from turn.workers.herdr import HerdrResourceNotFound
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry


async def _wait_for(predicate, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true before the timeout")
        await asyncio.sleep(0.05)


@pytest.mark.integration
async def test_herdr_project_space_contract(tmp_path: Path):
    """Exercise creation, durable panes, UI deletion, and external deletion."""
    if shutil.which("herdr") is None:
        pytest.skip("Herdr is required for the integration contract")

    # The live Herdr daemon is permissioned to the repository's project root;
    # the test intentionally exercises that real boundary rather than using a
    # fake path the daemon cannot open.
    project_root = Path(__file__).resolve().parents[2] / "projects"
    # Herdr is permissioned to this repository-owned root, so keep the real
    # daemon boundary while isolating every project path to this test run.
    project_paths = [
        project_root / f".turn-test-herdr-contract-{uuid.uuid4().hex}",
        project_root / f".turn-test-herdr-external-{uuid.uuid4().hex}",
    ]
    settings = Settings(
        data_dir=str(tmp_path / "turn-state"),
        projects_dir=str(project_root),
        default_executor="echo",
        planner="heuristic",
    )
    store = Store(settings.data_dir)
    await store.init()
    registry = WorkerRegistry()
    registry.register(EchoWorker())
    registry.register_planner(HeuristicPlanner("echo"))
    runner = Runner(store, registry, EventBus(), settings)
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
            created = await client.post(
                "/api/projects",
                json={
                    "name": "Herdr contract project",
                    "prompt": "Exercise the Herdr project-space contract",
                    "agent": {"harness": "echo", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                    "working_dir": str(project_paths[0]),
                },
            )
            assert created.status_code == 200, created.text
            project_id = uuid.UUID(created.json()["project_id"])
            root = await store.get_node(project_id)
            assert root is not None and root.repo_path is not None

            # Project creation owns exactly one visible Herdr workspace and
            # the root node receives one durable pane in that workspace.
            workspace_id = runner.terminal.project_workspace_id(str(project_id))
            assert workspace_id is not None
            workspace = await runner.terminal.adapter.get_workspace(workspace_id)
            assert workspace.workspace_id == workspace_id
            pane_id = runner.terminal.pane_id(project_id)
            assert pane_id is not None
            assert (await runner.terminal.adapter.get_pane(pane_id)).pane_id == pane_id

            # Browser/provider control streams are temporary: input and resize
            # work, detaching preserves the Herdr pane, and no second pane is
            # allocated on reconnect.
            assert await runner.open_shell(project_id)
            assert await runner.shell.resize(project_id, 100, 32)
            await _wait_for(lambda: bool(runner.shell.snapshot(project_id)["output"]))
            assert await runner.shell.write(project_id, "printf HERDR_CONTRACT_OK\\n")
            await _wait_for(
                lambda: "HERDR_CONTRACT_OK" in runner.shell.snapshot(project_id)["output"]
            )
            assert await runner.detach_shell(project_id)
            assert await runner.terminal.has_persistent_session(project_id)
            assert runner.terminal.pane_id(project_id) == pane_id

            # UI/API deletion closes the Herdr workspace before deleting the
            # Turn project and leaves no stale mapping behind.
            deleted = await client.delete(f"/api/projects/{project_id}")
            assert deleted.status_code == 200, deleted.text
            assert await store.get_node(project_id) is None
            with pytest.raises(HerdrResourceNotFound):
                await runner.terminal.adapter.get_workspace(workspace_id)
            metadata = json.loads(
                (Path(settings.data_dir) / "herdr-workspaces.json").read_text()
            )
            assert str(project_id) not in metadata

            # An external Herdr deletion is reflected back into Turn on the
            # server reconciliation path, including cleanup of its mapping.
            created_again = await client.post(
                "/api/projects",
                json={
                    "name": "Externally deleted Herdr project",
                    "prompt": "Verify reverse lifecycle reconciliation",
                    "agent": {"harness": "echo", "type_id": "executor"},
                    "run_policy": {"auto_run": False},
                    "working_dir": str(project_paths[1]),
                },
            )
            assert created_again.status_code == 200, created_again.text
            external_id = uuid.UUID(created_again.json()["project_id"])
            external_workspace_id = runner.terminal.project_workspace_id(str(external_id))
            assert external_workspace_id is not None
            assert await runner.terminal.adapter.close_workspace(external_workspace_id)

            await runner._reconcile_project_workspaces(await store.list_projects())
            assert await store.get_node(external_id) is None
            assert (await client.get("/api/projects")).json()["projects"] == []
            metadata = json.loads(
                (Path(settings.data_dir) / "herdr-workspaces.json").read_text()
            )
            assert str(external_id) not in metadata
    finally:
        try:
            for project in await store.list_projects():
                await runner.close_project_workspace(project.id)
        finally:
            try:
                await runner.stop()
            finally:
                try:
                    await store.dispose()
                finally:
                    for project_path in project_paths:
                        if project_path.exists():
                            shutil.rmtree(project_path)
                        assert not project_path.exists()
