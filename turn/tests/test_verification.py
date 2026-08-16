from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
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
from turn.graph.logic import GraphWalker
from turn.tests.fakes import FakeHerdrAdapter, FakeTerminalTransport
from turn.__main__ import agent_command, parser
from turn.workers.interactive import format_verification_result
from turn.workers.echo_worker import EchoWorker
from turn.workers.registry import WorkerRegistry


def test_verifier_contract_requires_one_dependency_target_and_is_strict():
    with pytest.raises(ValueError, match="exactly one target"):
        PlanResult(nodes=[NodeSpec(key="check", objective="Check", agent_type=AgentType.VERIFIER)])
    with pytest.raises(ValueError, match="must use depends_on"):
        PlanResult(nodes=[
            NodeSpec(
                key="work", objective="Build product"),
            NodeSpec(
                key="check", objective="Check", agent_type=AgentType.VERIFIER,
                parent_key="work", depends_on=["work"],
            ),
        ])
    decision = VerificationResult(
        decision=VerificationDecision.REJECT,
        summary="The launch path is broken",
        findings=["The app renders a blank canvas"],
        required_changes=["Mount the authored scene before the first render"],
    )
    assert decision.decision is VerificationDecision.REJECT


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


def test_verifier_target_is_canonical_workflow_dependency():
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="echo"),
        NodeSpec(
            key="check", objective="Verify product", executor="echo",
            agent_type=AgentType.VERIFIER, depends_on=["work"],
        ),
    ])
    assert plan.nodes[1].parent_key is None
    assert plan.nodes[1].depends_on == ["work"]


async def test_rejection_notifies_target_and_replays_entire_dependency_chain(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a verified product", repo_path=str(tmp_path / "repo"))
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="echo"),
        NodeSpec(
            key="check-one", objective="Verify product", executor="echo",
            agent_type=AgentType.VERIFIER, depends_on=["work"],
        ),
        NodeSpec(
            key="check-two", objective="Verify release", executor="echo",
            agent_type=AgentType.VERIFIER, depends_on=["check-one"],
        ),
    ])
    created = await store.apply_plan(root, plan)
    work, check_one, check_two = created
    work.agent = AgentConfig(harness=HarnessKind.ECHO, session_id="old-session")
    await store._save_node(work)
    terminal = FakeTerminalTransport()
    runner = Runner(
        store, events=EventBus(), settings=Settings(),
        herdr_adapter=FakeHerdrAdapter(), terminal_transport=terminal,
    )
    run = await store.create_run(check_one, "echo")
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
    assert {edge.type for edge in edges} <= {EdgeType.CONTAINS, EdgeType.DEPENDS_ON}
    assert sum(
        edge.type is EdgeType.DEPENDS_ON
        and edge.src == work.id
        and edge.dst == check_one.id
        for edge in edges
    ) == 1
    assert all(
        not (edge.type is EdgeType.CONTAINS and edge.src == work.id and edge.dst == check_one.id)
        for edge in edges
    )

    # A corrected submission from the same parent conversation reopens the
    # verifier as the next ordinary dependency stage.
    await store.set_status(work.id, NodeStatus.RUNNING)
    resubmission = await store.create_run(work, "echo")
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
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=auto_run),
        )
        plan = PlanResult(nodes=[
            NodeSpec(key="work", objective="Build product", executor="echo"),
            NodeSpec(
                key="check", objective="Verify product", executor="echo",
                agent_type=AgentType.VERIFIER, depends_on=["work"],
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
            herdr_adapter=FakeHerdrAdapter(),
            terminal_transport=FakeTerminalTransport(),
        )
        runner.registry.register(EchoWorker())
        # Simulate the verifier being the active member of a Step stage when
        # it rejects; the rejection must clear this stale barrier.
        if name == "step":
            runner._manual_stages[root.id] = {verifier.id}
        run = await store.create_run(verifier, "echo")
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

    # Step mode advances one dependency frontier at a time. The old rejected
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


class ActiveHerdrConversation:
    supports_inject = True

    def __init__(self):
        self.writes: list[str | bytes] = []

    def snapshot(self, node_id):
        return {"active": True, "output": ""}

    async def foreground_process_names(self, node_id):
        return ("codex",)

    async def write(self, node_id, data):
        self.writes.append(data)
        return True


async def test_rejection_is_pasted_into_the_target_node_session(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Build a verified product", repo_path=str(tmp_path))
    plan = PlanResult(nodes=[
        NodeSpec(key="work", objective="Build product", executor="echo"),
        NodeSpec(
            key="check", objective="Verify product", executor="echo",
            agent_type=AgentType.VERIFIER, depends_on=["work"],
        ),
    ])
    work, verifier = await store.apply_plan(root, plan)
    work.agent = AgentConfig(
        harness=HarnessKind.CODEX,
        session_id="node-owned-session",
    )
    await store._save_node(work)
    transport = ActiveHerdrConversation()
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(),
        herdr_adapter=FakeHerdrAdapter(),
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
    combined = "".join(value.decode() if isinstance(value, bytes) else value for value in transport.writes)
    assert "\x03" not in combined
    assert "\x1b[200~TURN VERIFICATION REJECTED" in combined
    assert transport.writes[-1] == "\r"
    await store.dispose()


def test_verifier_cli_is_the_only_verification_handoff(tmp_path, monkeypatch):
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
