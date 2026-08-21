"""Verifiability for the Herdr launch boundary and retry circuit breaker."""
from __future__ import annotations

import json

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, HarnessKind, NodeUIState
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.server.api import _serialize_graph
from turn.tests.mocks import MockHerdrAdapter
from turn.workers.herdr import (
    HERDR_OPERATOR_WARNING,
    HerdrBoundaryError,
    HerdrCliAdapter,
    HerdrUnavailableError,
)
from turn.workers.terminal import HerdrPtyTransport


@pytest.mark.asyncio
async def test_client_child_does_not_receive_herdr_ownership_marker(monkeypatch):
    captured: dict[str, object] = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return json.dumps({"result": {}}).encode(), b""

    async def create_process(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setattr(
        "turn.workers.herdr.asyncio.create_subprocess_exec", create_process
    )
    adapter = HerdrCliAdapter(herdr_binary="herdr")

    await adapter._run("status", "server")

    assert captured["args"][:2] == ("herdr", "status")
    assert "HERDR_ENV" not in captured["env"]


@pytest.mark.asyncio
async def test_nested_herdr_client_fails_before_creating_a_subprocess(monkeypatch):
    async def forbidden_process(*args, **kwargs):
        raise AssertionError("nested Herdr use must fail before subprocess creation")

    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setattr(
        "turn.workers.herdr.asyncio.create_subprocess_exec", forbidden_process
    )
    adapter = HerdrCliAdapter(herdr_binary="herdr")

    with pytest.raises(HerdrBoundaryError) as raised:
        await adapter._run("status", "server")

    assert raised.value.code == "herdr_nested_invocation"
    assert HERDR_OPERATOR_WARNING in str(raised.value)


def test_missing_herdr_is_a_visible_non_retryable_startup_failure(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.setattr("turn.workers.herdr.shutil.which", lambda _: None)

    adapter = HerdrCliAdapter()
    error = adapter.startup_error

    assert isinstance(error, HerdrUnavailableError)
    assert error.code == "herdr_unavailable"
    assert HERDR_OPERATOR_WARNING in str(error)


@pytest.mark.asyncio
async def test_nested_launch_persists_project_guard_and_suppresses_scheduling(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_ENV", "1")
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Herdr guard fixture",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    terminal = HerdrPtyTransport(
        str(tmp_path / "state"),
        adapter=MockHerdrAdapter(),
    )
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=terminal.adapter,
        terminal_transport=terminal,
    )

    await runner.start()
    guarded = await store.get_node(root.id)
    graph = await _serialize_graph(store, root.id, runner)
    await runner.stop(close_workspaces=True)

    assert guarded is not None
    assert guarded.runtime_guard is not None
    assert guarded.runtime_guard.code == "herdr_nested_invocation"
    assert graph["nodes"][0]["ui_state"] == NodeUIState.RUNTIME_GUARDED.value
    assert graph["nodes"][0]["allowed_actions"] == ["edit"]
    assert HERDR_OPERATOR_WARNING in guarded.runtime_guard.message
