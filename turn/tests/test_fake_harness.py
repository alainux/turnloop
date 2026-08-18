from __future__ import annotations

import uuid

from turn.config import Settings
from turn.domain.schemas import AgentConfig, AgentType, HarnessKind, Node
from turn.server.runtime import TurnRuntime
from turn.tests.capability_fixtures import load_builtin_capabilities
from turn.tests.fakes import FakeHerdrAdapter
from turn.workers.base import NodeExecutionContext
from turn.workers.fake_harness import FakeHarnessPlanner, FakeHarnessWorker
from turn.workers.terminal import LocalPtyTransport


async def test_process_fake_harness_uses_a_real_process_and_retains_sessions(tmp_path):
    load_builtin_capabilities(tmp_path, ["turn-basics", "turn-executing", "turn-verifying"])
    project_id = uuid.uuid4()
    node = Node(
        id=uuid.uuid4(),
        project_id=project_id,
        objective="Create and verify a greeting",
        agent=AgentConfig(harness=HarnessKind.FAKE),
    )
    output: list[str] = []
    sessions: list[str] = []

    async def stream(_node_id, chunk):
        output.append(chunk)

    async def remember(session_id):
        sessions.append(session_id)

    ctx = NodeExecutionContext(
        node=node,
        repo_path=str(tmp_path),
        terminal=LocalPtyTransport(),
        stream=stream,
        session_callback=remember,
        timeout_seconds=5,
    )
    worker = FakeHarnessWorker(Settings(default_run_timeout_seconds=5))

    first = await worker.execute(ctx)
    assert first.session_id is not None
    assert first.artifacts[0].name == "greeting.txt"
    assert "fake-turn: process started (kind=result)" in "".join(output)

    node.agent = AgentConfig(
        harness=HarnessKind.FAKE,
        type_id=AgentType.VERIFIER,
        session_id=first.session_id,
    )
    second = await worker.execute(ctx)
    assert second.verification is not None
    assert second.verification.decision.value == "REJECT"
    assert second.session_id == first.session_id
    assert sessions[0] == sessions[-1] == first.session_id

    ctx.forbidden_session_id = first.session_id
    third = await worker.execute(ctx)
    assert third.session_id is not None
    assert third.session_id != first.session_id


async def test_fake_planner_loads_project_capability_contract(tmp_path):
    project_id = uuid.uuid4()
    node = Node(
        id=uuid.uuid4(),
        project_id=project_id,
        objective="Create a tiny deterministic plan",
        agent=AgentConfig(harness=HarnessKind.FAKE, type_id=AgentType.PLANNER),
    )
    ctx = NodeExecutionContext(
        node=node,
        repo_path=str(tmp_path),
        terminal=LocalPtyTransport(),
        timeout_seconds=5,
    )

    plan = await FakeHarnessPlanner(Settings(default_run_timeout_seconds=5)).plan(ctx)

    assert plan.nodes
    assert (tmp_path / ".turn" / "capabilities" / "turn-planning" / "plugin.json").is_file()


async def test_fake_capability_is_only_exposed_by_test_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "turn.server.runtime.harness_capabilities",
        lambda *_args: [
            {
                "id": "codex",
                "label": "Codex",
                "binary": "codex",
                "reasoning": ["default"],
                "models": [],
                "available": True,
            }
        ],
    )

    production = TurnRuntime(
        Settings(
            data_dir=str(tmp_path / "production-state"),
            projects_dir=str(tmp_path / "production-projects"),
            planner="codex",
            default_executor="codex",
        ),
        herdr_adapter=FakeHerdrAdapter(),
        test_mode=False,
    )
    await production.start()
    try:
        assert "fake" not in {item["id"] for item in production.capabilities}
    finally:
        await production.stop()

    test_runtime = TurnRuntime(
        Settings(
            data_dir=str(tmp_path / "test-state"),
            projects_dir=str(tmp_path / "test-projects"),
            planner="fake",
            default_executor="fake",
        ),
        herdr_adapter=FakeHerdrAdapter(),
        test_mode=True,
    )
    await test_runtime.start()
    try:
        capabilities = {item["id"]: item for item in test_runtime.capabilities}
        assert capabilities["echo"]["supports_sessions"] is False
        assert capabilities["fake"]["supports_sessions"] is True
    finally:
        await test_runtime.stop()
