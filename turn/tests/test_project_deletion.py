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
from turn.domain.schemas import AgentConfig, HarnessKind, Node, Run
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server import api as server_api
from turn.server.api import router
from turn.tests.fakes import FakeHerdrAdapter
from turn.workers.conversations import (
    ConversationCleanup,
    ConversationProgress,
    ConversationRef,
    cleanup_conversations,
    conversation_refs,
)
from turn.workers.echo_worker import EchoWorker
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.planner import HeuristicPlanner
from turn.workers.registry import WorkerRegistry


async def _app(tmp_path: Path) -> tuple[FastAPI, Store, Runner]:
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        default_executor="echo",
        planner="heuristic",
    )
    store = Store(settings.data_dir, projects_dir=settings.projects_dir)
    await store.init()
    registry = WorkerRegistry()
    registry.register(EchoWorker())
    registry.register_planner(HeuristicPlanner("echo"))
    runner = Runner(store, registry, EventBus(), settings, herdr_adapter=FakeHerdrAdapter())
    app = FastAPI()
    app.include_router(router)
    app.state.store = store
    app.state.runner = runner
    app.state.events = runner.events
    app.state.test_mode = True
    return app, store, runner


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
        "codex-test", "delete", "c-1", "--force",
    ]
    assert factory.conversation_archive_command(HarnessKind.CODEX, "c-1") == [
        "codex-test", "archive", "c-1",
    ]
    assert factory.conversation_delete_command(HarnessKind.OPENCODE, "o-1") == [
        "opencode", "session", "delete", "o-1",
    ]
    assert factory.conversation_delete_command(HarnessKind.CLAUDE, "cl-1") is None
    assert factory.conversation_delete_command(HarnessKind.PI, "p-1") is None


async def test_conversation_cleanup_runs_one_by_one_and_falls_back_to_archive():
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
        if len(command) > 1 and command[1] == "delete":
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
        ("codex-test", "delete", "c-1", "--force"),
        ("codex-test", "archive", "c-1"),
        ("opencode", "session", "delete", "o-1"),
    ]
    assert [item.status for item in progress] == [
        "deleting", "archived", "deleting", "deleted", "unsupported",
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
if [ "$1" = "delete" ]; then
  exit 1
fi
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

    assert result.deleted == 1
    assert result.archived == 1
    assert result.failed == 0
    assert log.read_text().splitlines() == [
        "delete c-1 --force",
        "archive c-1",
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

            async def fake_cleanup(refs, *, cwd, commands, on_progress):
                del cwd, commands
                observed.extend((ref.harness, ref.session_id) for ref in refs)
                await on_progress(ConversationProgress(1, len(refs), "codex", "codex-session-1", "deleted", "done"))
                return ConversationCleanup(len(refs), len(refs), 0, 0, 0, ())

            monkeypatch.setattr(server_api, "cleanup_conversations", fake_cleanup)
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


async def test_project_delete_fails_closed_when_a_harness_session_is_untracked(tmp_path, monkeypatch):
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

            async def should_not_cancel(_project_id):
                nonlocal cancelled
                cancelled = True

            monkeypatch.setattr(runner, "cancel_project_runs", should_not_cancel)
            response = await client.request(
                "DELETE",
                f"/api/projects/{project_id}",
                json={"delete_files": True, "delete_conversations": True},
            )

            assert response.status_code == 409
            assert "no stored provider session id" in response.json()["detail"]
            assert not cancelled
            assert await store.get_node(project_id) is not None
            assert Path(created.json()["repo_path"]).exists()
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
                "agent": {"harness": "echo", "type_id": "planner"},
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
                "agent": {"harness": "echo", "type_id": "planner"},
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
                "agent": {"harness": "echo", "type_id": "planner"},
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

            async def fake_cleanup(refs, *, cwd, commands, on_progress):
                del cwd, commands
                await on_progress(ConversationProgress(1, len(refs), "codex", "codex-session-1", "failed", "denied"))
                return ConversationCleanup(1, 0, 0, 1, 0, ("denied",))

            monkeypatch.setattr(server_api, "cleanup_conversations", fake_cleanup)
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
