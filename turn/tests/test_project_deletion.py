from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import uuid

import httpx
import pytest
from fastapi import FastAPI

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, HarnessKind, Node, NodeSpec, PlanResult, Run, RunPolicy
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server import api as server_api
from turn.server.api import router
from turn.tests.mocks import MockHerdrAdapter
from turn.workers.conversations import (
    ConversationCleanup,
    ConversationProgress,
    ConversationRef,
    _default_command_runner,
    cleanup_conversations,
    conversation_refs,
)
from turn.workers.deterministic_worker import DeterministicWorker
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry
from turn.workers.terminal import HerdrPtyTransport


async def _app(tmp_path: Path) -> tuple[FastAPI, Store, Runner]:
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="deterministic",
        planner="heuristic",
    )
    store = Store(settings.data_dir, projects_dir=settings.projects_dir)
    await store.init()
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    registry.register_planner(HeuristicPlanner("deterministic"))
    runner = Runner(store, registry, EventBus(), settings, herdr_adapter=MockHerdrAdapter())
    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.runner = runner
    app.state.events = runner.events
    app.state.test_mode = True
    return app, store, runner


async def test_runner_stop_closes_all_owned_herdr_workspaces(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="deterministic",
        planner="heuristic",
    )
    store = Store(settings.data_dir, projects_dir=settings.projects_dir)
    await store.init()
    adapter = MockHerdrAdapter()
    runner = Runner(
        store,
        WorkerRegistry(),
        EventBus(),
        settings,
        herdr_adapter=adapter,
    )
    project = await store.create_project(
        "workspace cleanup",
        repo_path=str(tmp_path / "projects" / "workspace-cleanup"),
    )

    try:
        assert await runner.ensure_node_terminal(project.id)
        assert len(adapter.workspaces) == 1

        await runner.stop(close_workspaces=True)

        assert adapter.workspaces == {}
    finally:
        await store.dispose()


async def test_close_project_workspace_closes_node_processes_before_workspace(tmp_path):
    app, store, runner = await _app(tmp_path)
    del app
    project = await store.create_project(
        "close every project process",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    [child] = await store.apply_plan(
        project,
        PlanResult(nodes=[NodeSpec(key="child", objective="Child work", executor="deterministic")]),
    )
    order: list[tuple[str, uuid.UUID | str]] = []

    async def close_node(node_id):
        order.append(("node", node_id))
        return True

    async def close_workspace(project_key):
        order.append(("workspace", project_key))
        return True

    runner.terminal.close_persistent_session = close_node  # type: ignore[method-assign]
    runner.terminal.close_project_workspace = close_workspace  # type: ignore[method-assign]
    try:
        assert await runner.close_project_workspace(project.id)
        assert order == [
            ("node", project.id),
            ("node", child.id),
            ("workspace", str(project.id)),
        ]
    finally:
        await runner.stop()
        await store.dispose()


async def test_close_all_terminals_endpoint_keeps_project_runnable(tmp_path, monkeypatch):
    app, store, runner = await _app(tmp_path)
    project = await store.create_project(
        "close terminals without deleting",
        repo_path=str(tmp_path / "project"),
        run_policy=RunPolicy(auto_run=False),
    )
    closed: list[uuid.UUID] = []

    async def close_project(project_id: uuid.UUID) -> bool:
        closed.append(project_id)
        return True

    monkeypatch.setattr(runner, "close_project_workspace", close_project)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"/api/projects/{project.id}/workspace/close")

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "closed": True}
        assert closed == [project.id]
        assert await store.get_node(project.id) is not None
    finally:
        await runner.stop()
        await store.dispose()


async def test_close_all_terminals_is_scoped_and_reopens_in_the_same_project(tmp_path):
    app, store, runner = await _app(tmp_path)
    first = await store.create_project(
        "first isolated workspace",
        repo_path=str(tmp_path / "first"),
        run_policy=RunPolicy(auto_run=False),
    )
    second = await store.create_project(
        "second isolated workspace",
        repo_path=str(tmp_path / "second"),
        run_policy=RunPolicy(auto_run=False),
    )
    transport = runner.terminal
    assert isinstance(transport, HerdrPtyTransport)
    adapter = transport.adapter
    try:
        assert await runner.ensure_node_terminal(first.id)
        assert await runner.ensure_node_terminal(second.id)
        first_workspace = transport.project_workspace_id(str(first.id))
        second_workspace = transport.project_workspace_id(str(second.id))
        assert first_workspace and second_workspace and first_workspace != second_workspace
        assert set(adapter.workspaces) == {first_workspace, second_workspace}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(f"/api/projects/{first.id}/workspace/close")

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "closed": True}
        assert first_workspace not in adapter.workspaces
        assert second_workspace in adapter.workspaces
        assert transport.project_workspace_id(str(first.id)) is None
        assert transport.project_workspace_id(str(second.id)) == second_workspace

        # Closing the project workspace is reversible: the next allocation
        # recreates only that project's Herdr workspace and pane.
        assert await runner.ensure_node_terminal(first.id)
        recreated = transport.project_workspace_id(str(first.id))
        assert recreated and recreated != first_workspace
        assert set(adapter.workspaces) == {recreated, second_workspace}
    finally:
        for workspace in await adapter.list_workspaces():
            await adapter.close_workspace(workspace.workspace_id)
        await runner.stop()
        await store.dispose()


async def test_project_delete_recovers_a_lost_herdr_workspace_mapping(tmp_path, monkeypatch):
    """Deleting a project must close its Herdr space even if metadata was lost."""
    app, store, runner = await _app(tmp_path)
    transport = runner.terminal
    assert isinstance(transport, HerdrPtyTransport)
    adapter = transport.adapter
    project_path = tmp_path / "projects" / "lost-herdr-mapping"
    try:
        project = await store.create_project(
            "lost Herdr mapping",
            repo_path=str(project_path),
            agent=AgentConfig(harness=HarnessKind.CODEX),
        )
        persisted = await store.get_node(project.id)
        assert persisted is not None and persisted.agent is not None
        persisted.agent.session_id = "c-1"
        await store._save_node(persisted)  # type: ignore[attr-defined]
        assert await runner.ensure_node_terminal(project.id)
        workspace_id = transport.project_workspace_id(str(project.id))
        assert workspace_id is not None
        assert workspace_id in {item.workspace_id for item in await adapter.list_workspaces()}

        # Reproduce the live failure: the workspace survives, but the local
        # mapping is gone, so the old deletion path cannot identify it.
        transport._projects.pop(str(project.id), None)  # type: ignore[attr-defined]

        async def mock_cleanup(refs, *, cwd, commands, on_progress):
            del cwd, commands, on_progress
            assert [ref.session_id for ref in refs] == ["c-1"]
            assert await adapter.list_workspaces() == ()
            return ConversationCleanup(1, 1, 0, 0, 0, ())

        monkeypatch.setattr(server_api, "cleanup_conversations", mock_cleanup)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.request(
                "DELETE",
                f"/api/projects/{project.id}",
                json={"delete_files": True, "delete_conversations": True},
            )

        assert response.status_code == 200, response.text
        assert await store.get_node(project.id) is None
        assert not project_path.exists()
        assert workspace_id not in {item.workspace_id for item in await adapter.list_workspaces()}
    finally:
        for workspace in await adapter.list_workspaces():
            await adapter.close_workspace(workspace.workspace_id)
        await runner.stop()
        await store.dispose()


async def test_conversation_refs_are_deduplicated_across_current_and_historical_sessions():
    project_id = uuid.uuid4()
    node = Node(
        id=project_id,
        project_id=project_id,
        objective="root",
        agent=AgentConfig(harness=HarnessKind.CODEX, session_id="same-session"),
    )
    child = Node(
        project_id=project_id,
        objective="child",
        agent=AgentConfig(harness=HarnessKind.OPENCODE, session_id="other-session"),
    )
    runs = [
        Run(node_id=node.id, worker="codex", session_id="same-session"),
        Run(node_id=child.id, worker="opencode", session_id="other-session"),
    ]

    refs = conversation_refs([node, child], runs)

    assert [(ref.harness, ref.session_id) for ref in refs] == [
        (HarnessKind.CODEX, "same-session"),
        (HarnessKind.OPENCODE, "other-session"),
    ]


def test_harness_conversation_commands_use_public_provider_surfaces():
    factory = HarnessCommandFactory(codex_binary="codex-test")

    assert factory.conversation_delete_command(HarnessKind.CODEX, "c-1") == [
        "codex-test", "delete", "c-1",
    ]
    assert factory.conversation_archive_command(HarnessKind.CODEX, "c-1") == [
        "codex-test", "archive", "c-1",
    ]
    assert factory.conversation_delete_command(HarnessKind.OPENCODE, "o-1") == [
        "opencode", "session", "delete", "o-1",
    ]
    assert factory.conversation_delete_command(HarnessKind.CLAUDE, "cl-1") is None
    assert factory.conversation_delete_command(HarnessKind.PI, "p-1") is None


async def test_non_forced_delete_runs_with_a_tty_confirmation(tmp_path):
    command = tmp_path / "requires-tty"
    command.write_text(
        "#!/bin/sh\n"
        "if [ ! -t 0 ]; then exit 2; fi\n"
        "read answer\n"
        "[ \"$answer\" = y ] && printf 'deleted\\n'\n"
    )
    command.chmod(0o755)

    code, output = await _default_command_runner(
        [str(command), "delete", "c-1"], tmp_path
    )

    assert code == 0
    assert "deleted" in output


async def test_codex_cleanup_archives_before_deleting_without_force():
    ref = ConversationRef(HarnessKind.CODEX, "c-1", uuid.uuid4())
    calls: list[tuple[str, ...]] = []

    async def run_command(command: Sequence[str], cwd: Path | None) -> tuple[int, str]:
        del cwd
        calls.append(tuple(command))
        return 0, ""

    result = await cleanup_conversations(
        [ref],
        cwd=Path("/project"),
        commands=HarnessCommandFactory(codex_binary="codex-test"),
        run_command=run_command,
    )

    assert calls == [
        ("codex-test", "archive", "c-1"),
        ("codex-test", "delete", "c-1"),
    ]
    assert result.deleted == 1
    assert result.archived == 1


async def test_conversation_cleanup_archives_codex_before_a_non_forced_delete():
    refs = [
        ConversationRef(HarnessKind.CODEX, "c-1", uuid.uuid4()),
        ConversationRef(HarnessKind.OPENCODE, "o-1", uuid.uuid4()),
        ConversationRef(HarnessKind.CLAUDE, "cl-1", uuid.uuid4()),
    ]
    calls: list[tuple[str, ...]] = []
    progress: list[ConversationProgress] = []

    async def run_command(command: Sequence[str], cwd: Path | None) -> tuple[int, str]:
        del cwd
        calls.append(tuple(command))
        if tuple(command[:2]) == ("codex-test", "delete"):
            return 1, "delete is unavailable"
        return 0, ""

    result = await cleanup_conversations(
        refs,
        cwd=Path("/project"),
        commands=HarnessCommandFactory(codex_binary="codex-test"),
        on_progress=progress.append,
        run_command=run_command,
    )

    assert calls == [
        ("codex-test", "archive", "c-1"),
        ("codex-test", "delete", "c-1"),
        ("opencode", "session", "delete", "o-1"),
    ]
    assert [item.status for item in progress] == [
        "archiving", "archived", "deleting", "failed", "deleting", "deleted", "unsupported",
    ]
    assert result.total == 3
    assert result.deleted == 1
    assert result.archived == 1
    assert result.unsupported == 1
    assert not result.ok


async def test_conversation_cleanup_executes_the_harness_commands(tmp_path, monkeypatch):
    """The command contract must reach a subprocess, not only a mocked list."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    script = """#!/bin/sh
printf '%s\\n' "$*" >> "$TURN_TEST_COMMAND_LOG"
exit 0
"""
    for name in ("codex", "opencode"):
        binary = bin_dir / name
        binary.write_text(script)
        binary.chmod(0o755)
    monkeypatch.setenv("TURN_TEST_COMMAND_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    result = await cleanup_conversations(
        [
            ConversationRef(HarnessKind.CODEX, "c-1", uuid.uuid4()),
            ConversationRef(HarnessKind.OPENCODE, "o-1", uuid.uuid4()),
        ],
        cwd=tmp_path,
        commands=HarnessCommandFactory(),
    )

    assert result.deleted == 2
    assert result.archived == 1
    assert result.failed == 0
    assert log.read_text().splitlines() == [
        "archive c-1",
        "delete c-1",
        "session delete o-1",
    ]


async def test_project_delete_captures_session_ids_before_cancelling_runs(tmp_path, monkeypatch):
    app, store, runner = await _app(tmp_path)
    observed: list[tuple[HarnessKind, str]] = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Preserve the provider id during deletion",
                "agent": {
                    "harness": "codex",
                    "type_id": "planner",
                    "session_id": "codex-session-1",
                },
                "working_dir": str(tmp_path / "capture-session"),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = uuid.UUID(created.json()["project_id"])
            node = await store.get_node(project_id)
            assert node is not None and node.agent is not None
            node.agent.session_id = "codex-session-1"
            await store._save_node(node)  # type: ignore[attr-defined]

            async def clear_session_before_cleanup(project_id):
                node = await store.get_node(project_id)
                assert node is not None and node.agent is not None
                node.agent.session_id = None
                await store._save_node(node)  # type: ignore[attr-defined]

            monkeypatch.setattr(runner, "cancel_project_runs", clear_session_before_cleanup)

            async def mock_cleanup(refs, *, cwd, commands, on_progress):
                del cwd, commands
                observed.extend((ref.harness, ref.session_id) for ref in refs)
                await on_progress(ConversationProgress(1, len(refs), "codex", "codex-session-1", "deleted", "done"))
                return ConversationCleanup(len(refs), len(refs), 0, 0, 0, ())

            monkeypatch.setattr(server_api, "cleanup_conversations", mock_cleanup)
            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": False, "delete_conversations": True},
            )

            assert response.status_code == 200, response.text
            assert observed == [(HarnessKind.CODEX, "codex-session-1")]
    finally:
        await runner.stop()
        await store.dispose()


async def test_project_delete_closes_workspace_before_conversation_cleanup(tmp_path, monkeypatch):
    app, store, runner = await _app(tmp_path)
    order: list[str] = []
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Close the project workspace before deleting its conversation",
                "agent": {
                    "harness": "codex",
                    "type_id": "planner",
                    "session_id": "codex-session-1",
                },
                "working_dir": str(tmp_path / "close-before-cleanup"),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = uuid.UUID(created.json()["project_id"])

            async def record_cancel(_project_id):
                order.append("cancel")

            async def record_close(_project_id):
                order.append("workspace")
                return True

            async def record_cleanup(refs, *, cwd, commands, on_progress):
                del cwd, commands, on_progress
                order.append("cleanup")
                return ConversationCleanup(len(refs), len(refs), 0, 0, 0, ())

            monkeypatch.setattr(runner, "cancel_project_runs", record_cancel)
            monkeypatch.setattr(runner, "close_project_workspace", record_close)
            monkeypatch.setattr(server_api, "cleanup_conversations", record_cleanup)

            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": False, "delete_conversations": True},
            )

            assert response.status_code == 200, response.text
            assert order == ["cancel", "workspace", "cleanup"]
    finally:
        await runner.stop()
        await store.dispose()


async def test_project_delete_ignores_runs_without_provider_sessions(tmp_path, monkeypatch):
    app, store, runner = await _app(tmp_path)
    cancelled = False
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Do not silently orphan a conversation",
                "agent": {"harness": "codex", "type_id": "planner"},
                "working_dir": str(tmp_path / "untracked-session"),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = uuid.UUID(created.json()["project_id"])
            node = await store.get_node(project_id)
            assert node is not None
            await store.create_run(node, "planner")

            async def record_cancel(_project_id):
                nonlocal cancelled
                cancelled = True

            monkeypatch.setattr(runner, "cancel_project_runs", record_cancel)
            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": True, "delete_conversations": True},
            )

            assert response.status_code == 200, response.text
            assert cancelled
            assert await store.get_node(project_id) is None
            assert not Path(created.json()["repo_path"]).exists()
    finally:
        await runner.stop()
        await store.dispose()


@pytest.mark.parametrize("payload", [None, {"delete_files": False, "delete_conversations": False}])
async def test_project_delete_keeps_files_when_disk_deletion_is_not_opted_in(tmp_path, payload):
    app, store, runner = await _app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Keep this project directory",
                "agent": {"harness": "mock", "type_id": "planner"},
                "working_dir": str(tmp_path / "keep-me"),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = created.json()["project_id"]
            repo = Path(created.json()["repo_path"])
            marker = repo / "do-not-delete.txt"
            marker.write_text("preserved")

            response = await client.request(
                "DELETE", f"/api/projects/{project_id}", json=payload,
            ) if payload is not None else await client.delete(f"/api/projects/{project_id}")

            assert response.status_code == 200, response.text
            assert marker.read_text() == "preserved"
            assert await store.get_node(uuid.UUID(project_id)) is None
    finally:
        await runner.stop()
        await store.dispose()


async def test_project_delete_files_removes_the_entire_project_directory(tmp_path):
    app, store, runner = await _app(tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Remove this project directory",
                "agent": {"harness": "mock", "type_id": "planner"},
                "working_dir": str(tmp_path / "remove-me"),
                "run_policy": {"auto_run": False},
            })
            project_id = created.json()["project_id"]
            repo = Path(created.json()["repo_path"])
            (repo / "nested").mkdir()
            (repo / "nested" / "file.txt").write_text("gone")

            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": True, "delete_conversations": False},
            )

            assert response.status_code == 200, response.text
            assert not repo.exists()
            assert await store.get_node(uuid.UUID(project_id)) is None
    finally:
        await runner.stop()
        await store.dispose()


async def test_project_delete_files_refuses_protected_store_directories(tmp_path):
    app, store, runner = await _app(tmp_path)
    try:
        protected = tmp_path / "projects"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Do not remove the projects store",
                "agent": {"harness": "mock", "type_id": "planner"},
                "working_dir": str(protected),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = created.json()["project_id"]

            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": True},
            )

            assert response.status_code == 409
            assert protected.exists()
            assert await store.get_node(uuid.UUID(project_id)) is not None
    finally:
        await runner.stop()
        await store.dispose()


async def test_failed_conversation_cleanup_reports_progress_and_preserves_project(tmp_path, monkeypatch):
    app, store, runner = await _app(tmp_path)
    events = runner.events.subscribe()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/projects", json={
                "prompt": "Keep state when conversation cleanup fails",
                "agent": {
                    "harness": "codex",
                    "type_id": "planner",
                    "session_id": "codex-session-1",
                },
                "working_dir": str(tmp_path / "failed-cleanup"),
                "run_policy": {"auto_run": False},
            })
            assert created.status_code == 200, created.text
            project_id = created.json()["project_id"]
            repo = Path(created.json()["repo_path"])

            async def mock_cleanup(refs, *, cwd, commands, on_progress):
                del cwd, commands
                await on_progress(ConversationProgress(1, len(refs), "codex", "codex-session-1", "failed", "denied"))
                return ConversationCleanup(1, 0, 0, 1, 0, ("denied",))

            monkeypatch.setattr(server_api, "cleanup_conversations", mock_cleanup)
            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": True, "delete_conversations": True},
            )

            assert response.status_code == 409
            assert await store.get_node(uuid.UUID(project_id)) is not None
            assert repo.exists()
            seen = []
            while not events.empty():
                seen.append(events.get_nowait())
            assert any(event["type"] == "project.deletion_progress" for event in seen)
            assert any(event["type"] == "project.deletion_failed" for event in seen)
    finally:
        runner.events.unsubscribe(events)
        await runner.stop()
        await store.dispose()
