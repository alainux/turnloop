from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    ArtifactKind,
    ArtifactSpec,
    EdgeType,
    HarnessKind,
    NodeStatus,
    PlanResult,
    NodeSpec,
    VerificationDecision,
    VerificationResult,
    WorkerResult,
    Outcome,
    RunStatus,
    RunPolicy,
)
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.graph.logic import GraphWalker, derive_flow_edges
from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
from turn.__main__ import agent_command, parser
from turn.workers.interactive import format_verification_result
from turn.workers.deterministic_worker import DeterministicWorker
from turn.workers.registry import WorkerRegistry
from turn.workers.terminal import TerminalResult


def test_verifier_contract_allows_sequence_fan_in_and_rejects_cross_boundary_links():
    plan = PlanResult(nodes=[
        NodeSpec(key="design", objective="Design product"),
        NodeSpec(key="implementation", objective="Implement product"),
        NodeSpec(
            key="check", objective="Verify product", agent_type=AgentType.VERIFIER,
            follows=["design", "implementation"],
        ),
    ])
    assert plan.nodes[2].follows == ["design", "implementation"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NodeSpec.model_validate({
            "key": "legacy",
            "objective": "Legacy dependency field",
            "depends_on": ["design"],
        })

    with pytest.raises(ValueError, match="crosses a composition boundary"):
        PlanResult(nodes=[
            NodeSpec(
                key="work", objective="Build product"),
            NodeSpec(
                key="check", objective="Check", agent_type=AgentType.VERIFIER,
                parent_key="work", follows=["work"],
            ),
        ])
    decision = VerificationResult(
        decision=VerificationDecision.REJECT,
        summary="The launch path is broken",
        findings=["The app renders a blank canvas"],
        required_changes=["Mount the authored scene before the first render"],
    )
    assert decision.decision is VerificationDecision.REJECT


def test_sequence_rejects_long_range_shortcuts():
    with pytest.raises(ValueError, match="transitive shortcut"):
        PlanResult(nodes=[
            NodeSpec(key="start", objective="Start"),
            NodeSpec(key="middle", objective="Middle", follows=["start"]),
            NodeSpec(key="finish", objective="Finish", follows=["middle", "start"]),
        ])


def test_verification_artifact_is_the_submitted_result_not_terminal_transcript():
    result = VerificationResult(
        decision=VerificationDecision.APPROVE,
        summary="The launch path is playable",
        findings=["The scene renders"],
        evidence_refs=["tests/test_game.py"],
    )

    rendered = format_verification_result(result)

    assert '"decision": "APPROVE"' in rendered
    assert "tests/test_game.py" in rendered
    assert "\\x1b" not in rendered


def test_verifier_target_is_canonical_workflow_sequence():
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="deterministic"),
        NodeSpec(
            key="check", objective="Verify product", executor="deterministic",
            agent_type=AgentType.VERIFIER, follows=["work"],
        ),
    ])
    assert plan.nodes[1].parent_key is None
    assert plan.nodes[1].follows == ["work"]


async def test_rejection_notifies_target_and_replays_entire_sequence_chain(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a verified product", repo_path=str(tmp_path / "repo"))
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="deterministic"),
        NodeSpec(
            key="check-one", objective="Verify product", executor="deterministic",
            agent_type=AgentType.VERIFIER, follows=["work"],
        ),
        NodeSpec(
            key="check-two", objective="Verify release", executor="deterministic",
            agent_type=AgentType.VERIFIER, follows=["check-one"],
        ),
    ])
    created = await store.apply_plan(root, plan)
    work, check_one, check_two = created
    work.agent = AgentConfig(harness=HarnessKind.MOCK, session_id="old-session")
    await store._save_node(work)
    terminal = MockTerminalTransport()
    runner = Runner(
        store, events=EventBus(), settings=Settings(),
        herdr_adapter=MockHerdrAdapter(), terminal_transport=terminal,
    )
    run = await store.create_run(check_one, "deterministic")
    await store.set_status(check_one.id, NodeStatus.RUNNING)
    await runner._handle_outcome(
        check_one,
        run,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="rejected",
            verification=VerificationResult(
                decision=VerificationDecision.REJECT,
                summary="The product is not playable",
                findings=["No character controller"],
                required_changes=["Add camera look controls"],
            ),
        ),
    )
    refreshed = {node.id: await store.get_node(node.id) for node in [work, check_one, check_two]}
    assert refreshed[work.id].status is NodeStatus.RUNNABLE
    assert refreshed[work.id].agent.session_id == "old-session"
    assert refreshed[check_one.id].status is NodeStatus.PENDING
    assert refreshed[check_two.id].status is NodeStatus.PENDING
    assert "TURN VERIFICATION REJECTED" in str(terminal.snapshot(work.id)["output"])
    assert "\x03" in str(terminal.snapshot(work.id)["output"])

    _, edges, _ = await store.get_workgraph(root.id)
    assert {edge.type for edge in edges} <= {EdgeType.CONTAINS, EdgeType.FOLLOWS}
    assert sum(
        edge.type is EdgeType.FOLLOWS
        and edge.src == work.id
        and edge.dst == check_one.id
        for edge in edges
    ) == 1
    assert all(
        not (edge.type is EdgeType.CONTAINS and edge.src == work.id and edge.dst == check_one.id)
        for edge in edges
    )

    # A corrected submission from the same parent conversation reopens the
    # verifier as the next ordinary sequence stage.
    await store.set_status(work.id, NodeStatus.RUNNING)
    resubmission = await store.create_run(work, "deterministic")
    assert resubmission.session_id == "old-session"
    await runner._handle_outcome(
        work,
        resubmission,
        root.id,
        WorkerResult(outcome=Outcome.COMPLETE, summary="corrected submission"),
    )
    nodes, edges, _ = await store.get_workgraph(root.id)
    evaluation = GraphWalker(nodes, edges).evaluate()
    assert check_one.id in evaluation.runnable
    await store.dispose()


async def test_rejection_respects_auto_step_and_manual_progression(tmp_path):
    """Verification rejection reopens the target without bypassing mode policy."""

    async def make_runtime(name: str, *, auto_run: bool):
        store = Store(tmp_path / name)
        await store.init()
        root = await store.create_project(
            "Build a verified product",
            repo_path=str(tmp_path / f"{name}-repo"),
            agent=AgentConfig(harness=HarnessKind.MOCK),
            run_policy=RunPolicy(auto_run=auto_run),
        )
        plan = PlanResult(nodes=[
            NodeSpec(key="work", objective="Build product", executor="deterministic"),
            NodeSpec(
                key="check", objective="Verify product", executor="deterministic",
                agent_type=AgentType.VERIFIER, follows=["work"],
            ),
        ])
        work, verifier = await store.apply_plan(root, plan)
        await store.set_status(root.id, NodeStatus.EXPANDED)
        await store.set_status(verifier.id, NodeStatus.RUNNING)
        runner = Runner(
            store,
            registry=WorkerRegistry(),
            events=EventBus(),
            settings=Settings(),
            herdr_adapter=MockHerdrAdapter(),
            terminal_transport=MockTerminalTransport(),
        )
        runner.registry.register(DeterministicWorker())
        # Simulate the verifier being the active member of a Step stage when
        # it rejects; the rejection must clear this stale barrier.
        if name == "step":
            runner._manual_stages[root.id] = {verifier.id}
        run = await store.create_run(verifier, "deterministic")
        await runner._handle_outcome(
            verifier,
            run,
            root.id,
            WorkerResult(
                outcome=Outcome.COMPLETE,
                summary="rejected",
                verification=VerificationResult(
                    decision=VerificationDecision.REJECT,
                    summary="The product is not playable",
                    findings=["No character controller"],
                    required_changes=["Add camera look controls"],
                ),
            ),
        )
        return store, runner, root, work, verifier

    # Auto mode launches only the repaired prerequisite. Its verifier is not
    # relaunched until the repaired submission completes.
    store, runner, root, work, verifier = await make_runtime("auto", auto_run=True)
    await runner._schedule_project(root.id)
    assert set(runner._running) == {work.id}
    await asyncio.gather(*runner._running.values())
    await runner._schedule_project(root.id)
    assert set(runner._running) == {verifier.id}
    await asyncio.gather(*runner._running.values())
    await store.dispose()

    # Step mode advances one sequence frontier at a time. The old rejected
    # verifier cannot leave a stale manual barrier behind.
    store, runner, root, work, verifier = await make_runtime("step", auto_run=False)
    assert await runner.step(root.id) == [work.id]
    await asyncio.gather(*runner._running.values())
    assert await runner.step(root.id) == [verifier.id]
    await asyncio.gather(*runner._running.values())
    await store.dispose()

    # Manual node execution also runs only the selected repaired node; a plain
    # scheduler tick never auto-advances the verification loop.
    store, runner, root, work, verifier = await make_runtime("manual", auto_run=False)
    await runner._schedule_project(root.id)
    assert runner._running == {}
    assert await runner.run_node(work.id) == work.id
    await asyncio.gather(*runner._running.values())
    await runner._schedule_project(root.id)
    assert (await store.get_node(verifier.id)).status is NodeStatus.RUNNABLE
    assert runner._running == {}
    await store.dispose()


async def test_any_node_can_reject_and_route_to_an_arbitrary_node(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Build a deterministic project with a cross-branch review",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    plan = PlanResult(nodes=[
        NodeSpec(key="foundation", objective="Build the foundation", executor="deterministic"),
        NodeSpec(key="polish", objective="Polish the integration", executor="deterministic"),
        NodeSpec(
            key="review",
            objective="Review the integration",
            executor="deterministic",
            agent_type=AgentType.EXECUTOR,
            follows=["polish"],
        ),
    ], edges=[])
    foundation, polish, review = await store.apply_plan(root, plan)
    await store.set_status(foundation.id, NodeStatus.COMPLETE)
    await store.set_status(polish.id, NodeStatus.COMPLETE)
    await store.set_status(review.id, NodeStatus.RUNNING)

    terminal = MockTerminalTransport()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=terminal,
    )
    run = await store.create_run(review, "deterministic")
    await runner._handle_outcome(
        review,
        run,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="cross-branch review rejected",
            verification=VerificationResult(
                decision=VerificationDecision.REJECT,
                summary="The foundation is incompatible with the integration",
                required_changes=["Repair the foundation before polishing again"],
                target_node_id=foundation.id,
            ),
        ),
    )

    refreshed = {
        node.id: await store.get_node(node.id)
        for node in (foundation, polish, review)
    }
    assert refreshed[foundation.id].status is NodeStatus.RUNNABLE
    assert refreshed[polish.id].status is NodeStatus.COMPLETE
    assert refreshed[review.id].status is NodeStatus.PENDING
    nodes, edges, _ = await store.get_workgraph(root.id)
    flow = derive_flow_edges(
        nodes,
        edges,
        GraphWalker(nodes, edges).evaluate().status,
    )
    assert [(edge.src, edge.dst) for edge in flow] == [(review.id, foundation.id)]
    assert "Repair the foundation" in str(terminal.snapshot(foundation.id)["output"])
    await store.dispose()


async def test_deterministic_server_rejection_does_not_require_a_provider_session(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Run an Deterministic rejection demo",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
        run_policy=RunPolicy(auto_run=False),
    )
    work, review = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="work", objective="Start", executor="deterministic"),
            NodeSpec(
                key="review",
                objective="Review",
                executor="deterministic",
                follows=["work"],
            ),
        ]),
    )
    runner = Runner(
        store,
        registry=WorkerRegistry(),
        settings=Settings(default_executor="deterministic"),
        herdr_adapter=MockHerdrAdapter(),
    )

    await runner._notify_rejection(
        work,
        review,
        VerificationResult(
            decision=VerificationDecision.REJECT,
            summary="Return to Start",
            target_node_id=work.id,
        ),
    )

    assert runner.terminal.snapshot(work.id)["active"] is False
    await store.dispose()


class ActiveHerdrConversation:
    supports_inject = True

    def __init__(self):
        self.writes: list[str | bytes] = []
        self.commands: list[str] = []
        self.closed = False
        self.active = True
        self.persistent = True
        self.injected = asyncio.Event()
        self.released = asyncio.Event()

    def snapshot(self, node_id):
        return {"active": self.active, "output": ""}

    async def foreground_process_names(self, node_id):
        return ("codex",) if self.active else ("zsh",)

    async def close_persistent_session(self, node_id):
        self.closed = True
        self.active = False
        self.persistent = False
        return True

    async def has_persistent_session(self, node_id):
        return self.persistent

    async def ensure_session(self, node_id, **kwargs):
        self.active = True
        self.persistent = True
        await self.released.wait()
        return TerminalResult(returncode=0, output=b"")

    async def inject_command(self, node_id, command, **kwargs):
        self.commands.append(command)
        self.injected.set()
        return True

    async def write(self, node_id, data):
        self.writes.append(data)
        return True


    async def stop(self, node_id):
        self.active = False
        self.released.set()
        return True

    def release(self, node_id):
        return False


async def test_rejection_relaunches_with_retained_session_and_artifacts(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a verified product", repo_path=str(tmp_path))
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="deterministic"),
        NodeSpec(
            key="check", objective="Verify product", executor="deterministic",
            agent_type=AgentType.VERIFIER, follows=["work"],
        ),
    ])
    work, verifier = await store.apply_plan(root, plan)
    work.agent = AgentConfig(
        harness=HarnessKind.CODEX,
        session_id="node-owned-session",
    )
    await store._save_node(work)
    await store.add_artifacts(
        work.id,
        [ArtifactSpec(kind=ArtifactKind.TEXT, name="existing-result", content="kept")],
    )
    transport = ActiveHerdrConversation()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=transport,
    )
    await runner._notify_rejection(
        work,
        verifier,
        VerificationResult(
            decision=VerificationDecision.REJECT,
            summary="The product is not playable",
            findings=["The launch path is broken"],
            required_changes=["Fix the entry point"],
        ),
    )
    await asyncio.wait_for(transport.injected.wait(), timeout=1)
    try:
        command = shlex.split(transport.commands[-1])
        assert transport.closed
        assert command[:2] == ["codex", "resume"]
        assert "node-owned-session" in command
        assert "TURN VERIFICATION REJECTED" in command[-1]
        refreshed = await store.get_node(work.id)
        assert refreshed.agent.session_id == "node-owned-session"
        artifacts = await store.get_artifacts(work.id)
        assert [artifact.name for artifact in artifacts] == ["existing-result"]

        # A retained correction is an active provider turn.  Auto must leave
        # the scheduler-owned frontier alone until that conversation submits
        # its handoff, otherwise it launches a second `codex resume` into the
        # same durable pane.
        await store.set_status(work.id, NodeStatus.RUNNABLE)
        await runner._schedule_project(root.id)
        assert work.id not in runner._running
    finally:
        await runner.terminal.stop(work.id)
        reconnect = runner._reconnect_tasks.get(work.id)
        if reconnect is not None:
            await asyncio.gather(reconnect, return_exceptions=True)
    await store.dispose()


async def test_corrected_handoff_releases_reconnect_for_later_rejections(tmp_path):
    """A retained follow-up must not block the next verifier rejection."""
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a verified product", repo_path=str(tmp_path))
    work, verifier = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="work", objective="Build product", executor="deterministic"),
            NodeSpec(
                key="check", objective="Verify product", executor="deterministic",
                agent_type=AgentType.VERIFIER, follows=["work"],
            ),
        ]),
    )
    work.agent = AgentConfig(harness=HarnessKind.CODEX, session_id="retained-session")
    await store._save_node(work)
    transport = ActiveHerdrConversation()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=transport,
    )
    rejection = VerificationResult(
        decision=VerificationDecision.REJECT,
        summary="Fix the entry point",
        required_changes=["Repair the launch path"],
    )

    await runner._notify_rejection(work, verifier, rejection)
    await asyncio.wait_for(transport.injected.wait(), timeout=1)
    first_follow_up = runner._reconnect_tasks[work.id]

    await runner._apply_result_revision(
        work.id,
        root.id,
        WorkerResult(outcome=Outcome.COMPLETE, summary="corrected submission"),
    )

    assert first_follow_up.done()
    assert work.id not in runner._reconnect_tasks
    # The persistent pane is retained even though the control task is gone.
    assert transport.snapshot(work.id)["active"] is True

    transport.injected.clear()
    await runner._notify_rejection(work, verifier, rejection)
    await asyncio.wait_for(transport.injected.wait(), timeout=1)
    assert len(transport.commands) == 2

    await runner.terminal.stop(work.id)
    reconnect = runner._reconnect_tasks.get(work.id)
    if reconnect is not None:
        await asyncio.gather(reconnect, return_exceptions=True)
    await runner.stop()
    await store.dispose()


async def test_retained_verifier_can_change_a_rejection_after_submission(tmp_path, monkeypatch):
    """A user-directed verifier follow-up can revise both reason and decision."""
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project(
        "Reconsider a verification",
        repo_path=str(tmp_path / "repo"),
        agent=AgentConfig(harness=HarnessKind.MOCK),
        run_policy=RunPolicy(auto_run=False),
    )
    work, verifier = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="work", objective="Build product", executor="deterministic"),
            NodeSpec(
                key="verify", objective="Verify product", executor="deterministic",
                agent_type=AgentType.VERIFIER, follows=["work"],
            ),
        ]),
    )
    await store.set_status(work.id, NodeStatus.COMPLETE)
    verifier.agent = AgentConfig(
        harness=HarnessKind.CODEX,
        type_id=AgentType.VERIFIER,
        session_id="verifier-session",
    )
    await store._save_node(verifier)
    await store.set_status(verifier.id, NodeStatus.RUNNING)
    settings = Settings(default_executor="deterministic")
    terminal = MockTerminalTransport()
    terminal.supports_inject = True
    terminal.backend_name = "herdr"
    runner = Runner(
        store,
        registry=WorkerRegistry(),
        events=EventBus(),
        settings=settings,
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=terminal,
    )
    runner.registry.register(DeterministicWorker())

    initial_run = await store.create_run(verifier, "codex")
    await runner._handle_outcome(
        verifier,
        initial_run,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            session_id="verifier-session",
            verification=VerificationResult(
                decision=VerificationDecision.REJECT,
                summary="The launch path is broken",
                findings=["The entry point is not mounted"],
                required_changes=["Mount the entry point"],
                target_node_id=work.id,
            ),
        ),
    )

    # Verifier submissions may use the shared result handoff as well as the
    # dedicated verification path; both are part of the CLI contract.
    handoff = Path(root.repo_path) / ".turn" / "interactive" / f"{verifier.id}.result.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(handoff.parent / f"{verifier.id}.status.json"))
    monkeypatch.setenv("TURN_NODE_ID", str(verifier.id))
    args = parser().parse_args(["agent", "verify", "--stdin"])
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "decision": "REJECT",
            "summary": "The entry point needs a clearer handoff",
            "findings": ["The user-facing launch instruction is ambiguous"],
            "required_changes": ["Document the launch instruction"],
            "target_node_id": str(work.id),
        })),
    )
    assert agent_command(args) == 0

    for _ in range(100):
        current = await store.get_node(verifier.id)
        if current and current.verification and current.verification.summary == "The entry point needs a clearer handoff":
            break
        await asyncio.sleep(0.01)
    assert current is not None
    assert current.verification.summary == "The entry point needs a clearer handoff"

    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "decision": "APPROVE",
            "summary": "The revised handoff is acceptable",
            "findings": [],
            "required_changes": [],
        })),
    )
    assert agent_command(args) == 0
    for _ in range(100):
        current = await store.get_node(verifier.id)
        if current and current.verification and current.verification.decision is VerificationDecision.APPROVE:
            break
        await asyncio.sleep(0.01)
    assert current is not None
    assert current.verification.decision is VerificationDecision.APPROVE
    assert len(await store.get_runs(verifier.id)) == 3
    await runner.stop()
    await store.dispose()


def test_verification_cli_writes_the_verification_handoff(tmp_path, monkeypatch):
    handoff = tmp_path / "node.verification.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", str(uuid.uuid4()))
    args = parser().parse_args(["agent", "verify", "--stdin"])
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "decision": "REJECT",
            "summary": "Visual inspection failed",
            "findings": ["Scene is empty"],
            "required_changes": ["Add authored geometry"],
        })),
    )
    assert agent_command(args) == 0
    assert json.loads(handoff.read_text())["decision"] == "REJECT"
    assert json.loads(status.read_text())["state"] == "complete"


def test_any_node_can_submit_a_review_through_the_result_handoff(tmp_path, monkeypatch):
    handoff = tmp_path / "node.result.json"
    status = tmp_path / "node.status.json"
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", str(uuid.uuid4()))
    target = uuid.uuid4()
    args = parser().parse_args(["agent", "verify", "--stdin"])
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(json.dumps({
            "decision": "REJECT",
            "summary": "An earlier node needs correction",
            "target_node_id": str(target),
        })),
    )

    assert agent_command(args) == 0
    assert json.loads(handoff.read_text())["target_node_id"] == str(target)
