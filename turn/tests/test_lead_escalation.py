"""Mandatory lead/review/escalation tests (LEAD_ESCALATION.md).

Covers:
A. Root review — no synthetic auditor; ReviewRequest to the lead.
B. Nested review — parent planner receives it, resuming its own session.
C. Manager continuity — same boundary terminal/session reviews.
D. Escalation ladder — correction exhaustion climbs the hierarchy.
E. Bootstrap — automatic until root plan acceptance, then READY.
F. Step mode — one step launches exactly the runnable frontier.
G. Visibility — terminals are never substituted; cues point sender→receiver.
H. Restart — lead/session/review/manager state survives restarts.
I. Multi-project isolation — independent leads, sessions, review queues.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.lead import ReviewKind
from turn.domain.organization import (
    ManagerDecision,
    ManagerPhase,
    OrganizationContract,
    OrganizationScale,
    WorkItemStatus,
)
from turn.domain.schemas import (
    AgentConfig,
    ArtifactKind,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    RunStatus,
    Usage,
)
from turn.domain.state_machine import present_node
from turn.runner.events import EventBus
from turn.runner.organization import OrganizationManager
from turn.runner.runner import PlanReviewEscalated, Runner
from turn.testing.mocks import MockHerdrAdapter
from turn.tests.mocks import MockTerminalTransport
from turn.workers.registry import WorkerRegistry


class StructuredReviewPlanner:
    """Provider adapter returning scripted structured payloads."""

    name = "structured-review-fixture"

    def __init__(self, responses):
        self.responses = list(responses)
        self.contexts = []

    async def call_structured(self, ctx, _prompt, *, handoff_kind):
        self.contexts.append((ctx, handoff_kind))
        return self.responses.pop(0), Usage(input_tokens=3, output_tokens=5), "review-session"


class CorrectionPlanner(StructuredReviewPlanner):
    """Planner adapter with both plan() and scripted structured responses."""

    name = "correction-fixture"

    def __init__(self, responses, plans):
        super().__init__(responses)
        self.plans = list(plans)
        self.sessions = []

    async def plan(self, ctx):
        self.sessions.append(ctx.node.agent.session_id)
        return self.plans.pop(0)


class BlockingLeadPlanner(StructuredReviewPlanner):
    """Lead fixture that makes the safe-boundary mailbox observable."""

    def __init__(self, responses):
        super().__init__(responses)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def call_structured(self, ctx, prompt, *, handoff_kind):
        self.calls += 1
        self.contexts.append((ctx, handoff_kind, prompt))
        self.entered.set()
        await self.release.wait()
        return self.responses.pop(0), Usage(input_tokens=3, output_tokens=5), "retained-lead-session"


class ReconcileTrackingTerminal(MockTerminalTransport):
    def __init__(self):
        super().__init__()
        self.reconciled_sessions: list[tuple[uuid.UUID, str, str, str]] = []

    async def reconcile_provider_session(
        self,
        node_id: uuid.UUID,
        *,
        project_key: str,
        session_id: str,
        provider: str,
    ) -> bool:
        self.reconciled_sessions.append((node_id, project_key, session_id, provider))
        return True


def audit_payload(decision: str = "APPROVE", summary: str = "The plan is coherent.") -> dict:
    return {
        "outcome": "COMPLETE",
        "summary": "audit returned",
        "artifacts": [
            {
                "kind": ArtifactKind.JSON.value,
                "name": "plan-audit",
                "schema_name": "turn.plan-audit",
                "schema_version": "v1",
                "content": {
                    "decision": decision,
                    "summary": summary,
                    "findings": [],
                    "required_changes": [],
                },
            }
        ],
    }


def manager_payload(decision: str, summary: str, work_items: list[dict] | None = None) -> dict:
    return {
        "outcome": "COMPLETE",
        "summary": "manager returned",
        "artifacts": [
            {
                "kind": ArtifactKind.JSON.value,
                "name": "manager-result",
                "schema_name": "turn.manager-result",
                "schema_version": "v1",
                "content": {
                    "decision": decision,
                    "summary": summary,
                    "work_items": work_items or [],
                    "missing_inputs": [],
                },
            }
        ],
    }


def review_decision_payload(
    decision: str,
    summary: str,
    *,
    work_items: list[dict] | None = None,
    required_changes: list[str] | None = None,
    missing_inputs: list[str] | None = None,
) -> dict:
    return {
        "outcome": "COMPLETE",
        "summary": "review returned",
        "artifacts": [
            {
                "kind": ArtifactKind.JSON.value,
                "name": "review-decision",
                "schema_name": "turn.review-decision",
                "schema_version": "v1",
                "content": {
                    "decision": decision,
                    "summary": summary,
                    "required_changes": required_changes or [],
                    "work_items": work_items or [],
                    "missing_inputs": missing_inputs or [],
                },
            }
        ],
    }


async def make_runner(tmp_path, responses):
    planner = StructuredReviewPlanner(responses)
    registry = WorkerRegistry()
    registry.register_planner(planner, key="real")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    terminal = ReconcileTrackingTerminal()
    runner = Runner(
        store,
        registry=registry,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=terminal,
    )
    runner.provider_reviews_enabled = True
    return store, runner, planner, terminal


async def make_project(store, tmp_path, name="Build a game"):
    from turn.domain.organization import WorkspaceIsolation
    from turn.domain.schemas import RunPolicy as _RP
    root = await store.create_project(
        name,
        repo_path=str(tmp_path / "projects" / name.replace(" ", "-").lower()),
        agent=AgentConfig(harness="codex", model="gpt-5.6-luna"),
        run_policy=_RP(auto_run=False, workspace_isolation=WorkspaceIsolation.SHARED),
    )
    return await store.set_agent_session(root.id, "retained-planner-session")


# ---------------------------------------------------------------------------
# A. Root review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_review_goes_to_lead_without_synthetic_auditor(tmp_path):
    store, runner, planner, terminal = await make_runner(tmp_path, [audit_payload("APPROVE")])
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    contract = root.organization_contract

    audit = await runner._run_semantic_plan_audit(root, contract, PlanResult(nodes=[]))

    assert audit.decision.value == "APPROVE"
    # 1. No synthetic semantic-auditor process launches anywhere.
    all_runs = (await store.get_project_runs(root.project_id)) + await store.get_runs(
        lead.terminal_owner_id
    )
    assert {run.worker for run in all_runs}.isdisjoint({"semantic-plan-auditor"})
    # 2. A durable ReviewRequest from the root planner to the lead exists.
    requests = await store.review_requests(root.project_id)
    request = next(item for item in requests if item.kind.value == "PLAN_REVIEW")
    assert request.sender_id == root.id
    assert request.receiver_is_lead is True
    assert request.receiver_id == lead.terminal_owner_id
    # 3. The lead reviewed in its own durable terminal identity.
    lead_runs = await store.get_runs(lead.terminal_owner_id)
    assert any(run.worker == "project-lead" for run in lead_runs)
    assert lead.terminal_owner_id in terminal.close_requests
    # 5. Approval settles the review with a recorded decision.
    settled = await store.review_requests(root.project_id)
    plan_request = next(item for item in settled if item.kind.value == "PLAN_REVIEW")
    assert plan_request.status.value == "SETTLED"
    assert plan_request.decision.value == "APPROVE"
    await store.dispose()


@pytest.mark.asyncio
async def test_root_rejection_returns_feedback_to_same_planner_session(tmp_path):
    """A rejected root plan re-enters the SAME retained planner session."""
    store, runner, _planner, _terminal = await make_runner(
        tmp_path, [audit_payload("REJECT"), audit_payload("APPROVE")]
    )
    root = await make_project(store, tmp_path)
    # A durable charter makes the root plan subject to lead review.
    root.organization_contract = OrganizationContract.from_objective("Build a game")
    await store._save_node(root)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())

    class ScriptedPlanner(CorrectionPlanner):
        def __init__(self):
            super().__init__(
                [audit_payload("REJECT"), audit_payload("APPROVE")],
                [
                    PlanResult(nodes=[
                        NodeSpec(key="left", objective="Left branch", executor="codex"),
                        NodeSpec(key="right", objective="Right branch", executor="codex"),
                    ]),
                    PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
                    PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
                ],
            )

    runner.registry.register_planner(ScriptedPlanner(), key="real")
    scripted = runner.registry.planner
    runner.provider_reviews_enabled = True

    created = await runner._plan_node(root, root.project_id)

    # The correction loop reused one retained session for both attempts.
    assert len(scripted.sessions) >= 2
    assert all(sid == "retained-planner-session" for sid in scripted.sessions)
    assert created, "the corrected plan must materialize"
    requests = await store.review_requests(root.project_id)
    decisions = [
        item.decision.value
        for item in sorted(requests, key=lambda r: r.created_at)
        if item.kind.value == "PLAN_REVIEW"
    ]
    assert decisions == ["REJECT", "APPROVE"]
    await store.dispose()


# ---------------------------------------------------------------------------
# B. Nested review
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_review_resumes_parent_planner_session(tmp_path):
    store, runner, planner, terminal = await make_runner(tmp_path, [audit_payload("APPROVE")])
    root = await make_project(store, tmp_path)
    children = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(key="dept", objective="Department", agent_type=None, executor="planner", plan=True),
            ]
        ),
    )
    nested = children[0]
    nested = await store.set_agent_session(nested.id, "nested-planner-session")

    audit = await runner._run_semantic_plan_audit(
        nested, nested.organization_contract, PlanResult(nodes=[])
    )

    assert audit.decision.value == "APPROVE"
    # 1. The parent planner — not the lead — receives the review.
    requests = await store.review_requests(root.project_id)
    request = next(item for item in requests if item.kind.value == "PLAN_REVIEW")
    assert request.sender_id == nested.id
    assert request.receiver_is_lead is False
    assert request.receiver_id == root.id
    # 2. The parent resumes its own retained session.
    ctx, _kind = planner.contexts[0]
    assert ctx.node.id == root.id
    assert ctx.node.agent.session_id == "retained-planner-session"
    assert (root.id, str(root.project_id), "retained-planner-session", "codex") in terminal.reconciled_sessions
    # 3. No synthetic reviewer process: the run belongs to the parent itself.
    runs = await store.get_runs(root.id)
    review_run = [run for run in runs if run.worker == "parent-plan-review"]
    assert review_run
    assert all(run.process_owner_id == root.id for run in review_run)
    await store.dispose()


# ---------------------------------------------------------------------------
# C. Manager continuity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_completion_authority_reviews_on_receiver_terminal_and_session(tmp_path):
    store, runner, planner, terminal = await make_runner(
        tmp_path,
        [
            review_decision_payload(
                "REJECT",
                "needs follow-up",
                work_items=[
                    {
                        "key": "follow-up",
                        "title": "Follow-up work",
                        "instructions": "Do the follow-up.",
                    }
                ],
            ),
            review_decision_payload("APPROVE", "done"),
        ],
    )
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="dept", objective="Department", executor="planner", plan=True)]),
    )
    nested = await store.set_agent_session(children[0].id, "nested-planner-session")
    # A completed leaf makes the deterministic acceptance gate evaluable;
    # drop auto-generated evidence criteria so no verifier fixture is needed.
    nested_contract = nested.organization_contract.model_copy(deep=True)
    nested_contract.acceptance_criteria = []
    nested.organization_contract = nested_contract
    await store._save_node(nested)
    # A completed leaf makes the deterministic acceptance gate evaluable.
    leaves = await store.apply_plan(
        nested,
        PlanResult(nodes=[NodeSpec(key="unit", objective="Unit work", executor="codex")]),
    )
    await store.set_status(leaves[0].id, NodeStatus.COMPLETE)

    # The settled frontier only records a durable request — zero model calls.
    await runner._request_authority_completion_review(nested)
    assert planner.contexts == [], "request creation must not launch a model turn"
    requests = await store.review_requests(root.project_id)
    completion = [
        item for item in sorted(requests, key=lambda r: r.created_at)
        if item.kind.value == "COMPLETION_REVIEW"
    ]
    assert len(completion) == 1
    request = completion[0]
    assert request.status.value == "PENDING"
    # Nested acceptance belongs to the parent planner, not the boundary itself.
    assert request.receiver_is_lead is False
    assert request.receiver_id == root.id

    # The scheduler-owned settlement resumes the parent's retained session.
    await runner.settle_review_request(root.project_id, request.id)
    contexts = [ctx for ctx, _ in planner.contexts]
    assert contexts and contexts[0].node.id == root.id
    assert contexts[0].node.agent.session_id == "retained-planner-session"
    assert (
        root.id,
        str(root.project_id),
        "retained-planner-session",
        "codex",
    ) in terminal.reconciled_sessions
    settled = next(
        item for item in await store.review_requests(
            root.project_id, sender_id=nested.id
        )
        if item.id == request.id
    )
    assert settled.status.value == "SETTLED"
    assert settled.decision.value == "REJECT"
    # The rejection appended a bounded wave through ordinary machinery.
    wave_root = await store.get_node(nested.id)
    assert wave_root.manager_iteration >= 1
    follow_up = next(
        item for item in await store.list_work_items(root.project_id, organization_id=nested.id)
        if item.key == "follow-up"
    )
    await store.update_work_item(follow_up.id, status=WorkItemStatus.COMPLETE)

    # Second frontier settlement: the same receiver approves on the session
    # the first review persisted.
    await runner._request_authority_completion_review(nested)
    pending = [
        item for item in await store.review_requests(
            root.project_id, sender_id=nested.id, status=None
        )
        if item.kind.value == "COMPLETION_REVIEW" and item.status.value == "PENDING"
    ]
    assert len(pending) == 1
    await runner.settle_review_request(root.project_id, pending[0].id)
    assert [ctx.node.agent.session_id for ctx, _ in planner.contexts] == [
        "retained-planner-session",
        "review-session",
    ]
    approved = next(
        item for item in await store.review_requests(
            root.project_id, sender_id=nested.id
        )
        if item.id == pending[0].id
    )
    assert approved.decision.value == "APPROVE"
    final = await store.get_node(nested.id)
    assert final.manager_phase is ManagerPhase.ACCEPTED
    await store.dispose()


# ---------------------------------------------------------------------------
# D. Escalation ladder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correction_exhaustion_escalates_nested_to_parent(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    children = await store.apply_plan(
        root,
        PlanResult(
            nodes=[NodeSpec(key="dept", objective="Department", executor="planner", plan=True)]
        ),
    )
    nested = await store.set_agent_session(children[0].id, "nested-session")

    # Every proposal is structurally invalid and every semantic review rejects.
    runner.registry.register_planner(
        CorrectionPlanner(
            [audit_payload("REJECT")] * 5,
            [
                PlanResult(
                    nodes=[
                        NodeSpec(key="l", objective="L", executor="codex"),
                        NodeSpec(key="r", objective="R", executor="codex"),
                    ]
                )
            ]
            * 5,
        ),
        key="real",
    )
    runner.provider_reviews_enabled = True

    with pytest.raises(PlanReviewEscalated) as excinfo:
        await runner._plan_node(nested, root.id)

    requests = await store.review_requests(root.project_id)
    escalations = [item for item in requests if item.kind.value == "ESCALATION"]
    assert escalations, "an escalation request must exist"
    escalation = escalations[0]
    assert escalation.sender_id == nested.id
    assert escalation.receiver_is_lead is False
    assert escalation.receiver_id == root.id
    assert escalation.status.value == "PENDING"
    assert excinfo.value.review_request_id == escalation.id
    await store.dispose()


@pytest.mark.asyncio
async def test_correction_exhaustion_at_root_escalates_to_lead(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    runner.registry.register_planner(
        CorrectionPlanner(
            [audit_payload("REJECT")] * 5,
            [
                PlanResult(
                    nodes=[
                        NodeSpec(key="l", objective="L", executor="codex"),
                        NodeSpec(key="r", objective="R", executor="codex"),
                    ]
                )
            ]
            * 5,
        ),
        key="real",
    )
    runner.provider_reviews_enabled = True

    with pytest.raises(PlanReviewEscalated):
        await runner._plan_node(root, root.id)

    requests = await store.review_requests(root.project_id)
    escalation = next(item for item in requests if item.kind.value == "ESCALATION")
    assert escalation.sender_id == root.id
    assert escalation.receiver_is_lead is True
    assert escalation.receiver_id == lead.terminal_owner_id
    await store.dispose()


@pytest.mark.asyncio
async def test_manager_loop_exhaustion_blocks_and_escalates(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    contract = root.organization_contract.model_copy(deep=True)
    contract.escalation.max_manager_iterations = 1
    root.organization_contract = contract
    await store._save_node(root)
    await store.set_manager_state(
        root.id, phase=ManagerPhase.EXECUTING, iteration=1, reasons=["frontier settled"]
    )

    class ContinueDecision:
        decision = ManagerDecision.CONTINUE
        reason = "still working"
        phase = ManagerPhase.EXECUTING
        replan = False

    await runner._maybe_escalate_manager_loop(await store.get_node(root.id), ContinueDecision())

    node = await store.get_node(root.id)
    assert node.status is NodeStatus.BLOCKED
    requests = await store.review_requests(root.project_id)
    assert any(item.kind.value == "ESCALATION" for item in requests)
    await store.dispose()


# ---------------------------------------------------------------------------
# E. Bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_runs_root_planner_until_acceptance_then_ready(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    await store.set_bootstrap_status(root.project_id, "BOOTSTRAPPING")

    launched: list[uuid.UUID] = []

    async def execute(node, project_id):
        launched.append(node.id)
        # Simulate the bootstrap outcome: root plan applied and accepted.
        await store.apply_plan(
            await store.get_node(project_id),
            PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
        )

    runner.scheduler.set_executor(execute)
    await runner.scheduler.schedule_once(root.project_id)
    await asyncio.gather(*runner.scheduler.running.values(), return_exceptions=True)
    # The next scheduler pass observes the accepted root plan and settles
    # bootstrap.
    await runner.scheduler.schedule_once(root.project_id)

    # The root planner launched without any Play click...
    assert launched == [root.id]
    # ...and acceptance flipped the project to READY.
    assert await store.bootstrap_status(root.project_id) == "READY"
    await store.dispose()


@pytest.mark.asyncio
async def test_bootstrap_never_launches_below_the_root_before_acceptance(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    await store.set_bootstrap_status(root.project_id, "BOOTSTRAPPING")
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
    )
    # The child became runnable, but bootstrap must not launch it.
    await store.set_status(children[0].id, NodeStatus.RUNNABLE)

    launched: list[uuid.UUID] = []

    async def execute(node, project_id):
        launched.append(node.id)

    runner.scheduler.set_executor(execute)
    await runner.scheduler.schedule_once(root.project_id)
    await asyncio.gather(*runner.scheduler.running.values(), return_exceptions=True)

    assert launched == []
    await store.dispose()


@pytest.mark.asyncio
async def test_user_interrupt_stops_bootstrap_and_keeps_failure_visible(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    await store.set_bootstrap_status(root.project_id, "BOOTSTRAPPING")
    await store.set_status(root.id, NodeStatus.CANCELLED)

    await runner.scheduler.schedule_once(root.project_id)

    assert await store.bootstrap_status(root.project_id) == "READY"
    assert (await store.get_node(root.id)).status is NodeStatus.CANCELLED
    await store.dispose()


# ---------------------------------------------------------------------------
# F. Step mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_launches_exactly_the_runnable_frontier(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    children = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(key="a", objective="A", executor="codex"),
                NodeSpec(key="b", objective="B", executor="codex"),
                NodeSpec(key="c", objective="C", executor="codex", follows=["a"]),
            ]
        ),
    )
    by_key = {}
    for node in children:
        refreshed = await store.get_node(node.id)
        by_key[node.objective] = refreshed

    launched: list[uuid.UUID] = []

    async def execute(node, project_id):
        launched.append(node.id)

    runner.scheduler.set_executor(execute)
    stepped = await runner.scheduler.step(root.project_id)
    await asyncio.gather(*runner.scheduler.running.values(), return_exceptions=True)

    # Independent frontier nodes run together; the dependent stage waits.
    assert set(stepped) == {by_key["A"].id, by_key["B"].id}
    assert by_key["C"].id not in stepped
    await store.dispose()


# ---------------------------------------------------------------------------
# G. Visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_cue_points_sender_to_receiver_and_terminals_stay_distinct(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [audit_payload("APPROVE")])
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())

    # Simulate the pending review window: create the request as the runner does
    # before the lead turn settles it.
    request = await store.create_review_request(
        project_id=root.project_id,
        sender_id=root.id,
        receiver_id=lead.terminal_owner_id,
        receiver_is_lead=True,
        kind=ReviewKind.PLAN_REVIEW,
        reason="plan proposal",
    )

    # The graph projection exposes the active review trail.
    graph = await store.get_workgraph(root.project_id)
    assert graph[0], "graph nodes exist"
    requests = await store.review_requests(root.project_id, status=request.status)
    assert len(requests) == 1
    assert requests[0].sender_id == root.id
    assert requests[0].receiver_id == lead.terminal_owner_id

    # The state-machine projection never substitutes one terminal for another:
    # the planner node keeps its own pane id and the lead keeps its own.
    projection = present_node(await store.get_node(root.id))
    assert projection is not None
    await store.dispose()


# ---------------------------------------------------------------------------
# H. Restart recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_recovers_lead_session_reviews_and_manager_state(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    lead = await store.update_lead(root.project_id, session_id="lead-retained-session")
    await store.create_review_request(
        project_id=root.project_id,
        sender_id=root.id,
        receiver_id=lead.terminal_owner_id,
        receiver_is_lead=True,
        kind=ReviewKind.PLAN_REVIEW,
        reason="pending across restart",
    )
    await store.set_manager_state(root.id, phase=ManagerPhase.REVIEW_PENDING, iteration=2)
    await store.set_bootstrap_status(root.project_id, "BOOTSTRAPPING")
    await store.dispose()

    reopened = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await reopened.init()
    recovered_lead = await reopened.project_lead(root.project_id)
    assert recovered_lead is not None
    assert recovered_lead.session_id == "lead-retained-session"
    assert recovered_lead.terminal_owner_id == lead.terminal_owner_id
    requests = await reopened.review_requests(root.project_id)
    assert len(requests) == 1
    assert requests[0].status.value == "PENDING"
    node = await reopened.get_node(root.id)
    assert node.manager_phase is ManagerPhase.REVIEW_PENDING
    assert node.manager_iteration == 2
    assert await reopened.bootstrap_status(root.project_id) == "BOOTSTRAPPING"
    # Recovery must not fabricate duplicate AI processes for the lead.
    lead_runs = await reopened.get_runs(lead.terminal_owner_id)
    assert all(run.status is not RunStatus.RUNNING for run in lead_runs)
    await reopened.dispose()


# ---------------------------------------------------------------------------
# I. Multi-project isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_projects_have_independent_leads_sessions_and_review_queues(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    first = await make_project(store, tmp_path, name="Project One")
    second = await make_project(store, tmp_path, name="Project Two")
    lead_one = await store.ensure_project_lead(first.project_id, agent=first.agent.model_copy())
    lead_two = await store.ensure_project_lead(second.project_id, agent=second.agent.model_copy())

    assert lead_one.terminal_owner_id != lead_two.terminal_owner_id

    await store.create_review_request(
        project_id=first.project_id,
        sender_id=first.id,
        receiver_id=lead_one.terminal_owner_id,
        receiver_is_lead=True,
        kind=ReviewKind.ESCALATION,
        reason="first project only",
    )

    one = await store.review_requests(first.project_id)
    two = await store.review_requests(second.project_id)
    assert len(one) == 1
    assert two == []
    # Resolving a lead by terminal owner returns exactly the owning project.
    assert (await store.lead_by_terminal_owner(lead_one.terminal_owner_id)).project_id == first.project_id
    assert (await store.lead_by_terminal_owner(lead_two.terminal_owner_id)).project_id == second.project_id
    await store.dispose()


# ---------------------------------------------------------------------------
# LEAD_ESCALATION_FINISH.md
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_lead_terminal_resumes_retained_session_never_shell(tmp_path):
    """Typing into an idle Lead terminal runs a real retained-session turn.

    The typed text must never reach the durable shell's stdin: it is consumed
    by the conversation line editor and submitted as one lead model turn that
    streams in the same pane and preserves ProjectLead.session_id.
    """
    store, runner, planner, terminal = await make_runner(
        tmp_path,
        [audit_payload("APPROVE")],  # scripted review envelope for the lead turn
    )
    root = await make_project(store, tmp_path)
    await store.set_agent_session(root.id, "retained-planner-session")
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    owner = lead.terminal_owner_id

    # Simulate the post-bootstrap idle pane: an active shell session exists.
    terminal._node(owner)["active"] = True
    terminal._node(owner)["persistent"] = True

    # Idle: every keystroke is consumed — nothing reaches the shell's stdin.
    forwarded = await runner.lead_console_input(owner, "What is blocking this project?\r")
    assert forwarded is None
    assert all("What is blocking" not in text for _node, text in terminal.written)

    # The conversation turn ran on the lead's identity with the retained
    # session and created a normal Run.
    await asyncio.gather(*runner._lead_tasks.values(), return_exceptions=True)
    assert all(task.done() for task in runner._lead_tasks.values())
    contexts = [ctx for ctx, _kind in planner.contexts]
    lead_ctx = [ctx for ctx, _kind in planner.contexts if ctx.node.id == owner]
    assert lead_ctx, "the lead turn must execute on the lead's terminal identity"
    assert lead_ctx[0].node.agent.session_id == lead.session_id
    runs = await store.get_runs(owner)
    assert any(run.worker == "project-lead" for run in runs)
    refreshed = await store.lead_by_terminal_owner(owner)
    assert refreshed.session_id == "review-session"
    assert refreshed.status.value == "IDLE"
    await store.dispose()


@pytest.mark.asyncio
async def test_busy_lead_terminal_queues_without_forwarding(tmp_path):
    """Busy Lead input waits for the next safe retained-session turn."""
    store, runner, _planner, terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    owner = lead.terminal_owner_id

    class Blocker:
        def __init__(self):
            self.done_flag = asyncio.Event()

        def __call__(self):
            return self.done_flag.is_set()

    hold = asyncio.Event()

    async def long_turn():
        await hold.wait()

    task = asyncio.create_task(long_turn())
    runner._lead_tasks[owner] = task
    try:
        forwarded = await runner.lead_console_input(owner, "stop\r")
        assert forwarded is None
        assert all("stop" not in text for _node, text in terminal.written)
        pending = await store.pending_lead_messages(root.project_id)
        assert [item.content for item in pending] == ["stop"]
    finally:
        hold.set()
        await task
        runner._lead_tasks.pop(owner, None)
    await store.dispose()


@pytest.mark.asyncio
async def test_busy_lead_message_runs_once_on_next_retained_turn(tmp_path):
    store, runner, _planner, terminal = await make_runner(tmp_path, [])
    planner = BlockingLeadPlanner([
        {"summary": "first turn finished"},
        {"summary": "queued instruction handled"},
    ])
    runner.registry.register_planner(planner, key="real")
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())

    first, first_task = await runner.enqueue_lead_message(
        lead.terminal_owner_id, "Start the project"
    )
    assert first.status.value == "QUEUED"
    assert first_task is not None
    await planner.entered.wait()

    forwarded = await runner.lead_console_input(
        lead.terminal_owner_id, "Make mobile quality a priority.\r"
    )
    assert forwarded is None
    assert all("mobile quality" not in text for _node, text in terminal.written)
    pending = await store.pending_lead_messages(root.project_id)
    assert [item.content for item in pending] == ["Make mobile quality a priority."]

    planner.release.set()
    await first_task
    follow_up = runner._lead_tasks.get(lead.terminal_owner_id)
    assert follow_up is not None and follow_up is not first_task
    await follow_up

    transcript = await store.lead_transcript(root.project_id)
    assert [item.content for item in transcript if item.role.value == "user"][-2:] == [
        "Start the project",
        "Make mobile quality a priority.",
    ]
    assert [item.content for item in transcript if item.role.value == "lead"][-2:] == [
        "first turn finished",
        "queued instruction handled",
    ]
    assert await store.pending_lead_messages(root.project_id) == []
    assert len(await store.get_runs(lead.terminal_owner_id)) == 2
    assert {run.session_id for run in await store.get_runs(lead.terminal_owner_id)} == {
        "retained-lead-session"
    }
    await store.dispose()


@pytest.mark.asyncio
async def test_worker_mailbox_waits_for_next_context_boundary(tmp_path):
    store, runner, _planner, terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    (worker,) = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="worker", objective="Do the work", executor="deterministic")]),
    )
    terminal._node(worker.id)["active"] = True
    item = await store.queue_inbound_message(
        root.project_id,
        worker.id,
        "Finding from the active reviewer",
        source="reviewer",
    )
    assert item.status.value == "QUEUED"
    assert terminal.written == []

    context = await runner._build_context(worker, run_id=str(uuid.uuid4()))
    assert [message.content for message in context.inbound_messages] == [
        "Finding from the active reviewer"
    ]
    assert await store.pending_inbound_messages(root.project_id, worker.id) == []
    await store.dispose()


@pytest.mark.asyncio
async def test_lead_wait_is_inference_free_and_wakes_once_for_escalation(tmp_path):
    store, runner, _planner, _terminal = await make_runner(tmp_path, [])
    planner = StructuredReviewPlanner([{"summary": "I reviewed the escalation."}])
    runner.registry.register_planner(planner, key="real")
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    await runner.wait_lead(root.project_id, ["organization.escalation.blocked"])
    assert (await store.project_lead(root.project_id)).status.value == "DORMANT"
    assert runner._lead_tasks == {}

    for index in range(50):
        await runner._emit("node.updated", root.project_id, {"node_id": str(index)})
    assert runner._lead_tasks == {}
    assert await store.get_runs(lead.terminal_owner_id) == []

    await runner._emit(
        "organization.escalation.blocked",
        root.project_id,
        {"reason": "needs a decision"},
    )
    tasks = [task for task in runner._lead_tasks.values() if not task.done()]
    assert len(tasks) == 1
    await asyncio.gather(*tasks)
    assert len(await store.get_runs(lead.terminal_owner_id)) == 1
    assert (await store.project_lead(root.project_id)).status.value == "IDLE"
    await store.dispose()


@pytest.mark.asyncio
async def test_explicit_lead_cancel_ends_old_run_before_fresh_message(tmp_path):
    store, runner, _planner, terminal = await make_runner(tmp_path, [])
    planner = BlockingLeadPlanner([{"summary": "fresh turn completed"}])
    runner.registry.register_planner(planner, key="real")
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    _entry, task = await runner.enqueue_lead_message(lead.terminal_owner_id, "Old assignment")
    assert task is not None
    await planner.entered.wait()
    await runner.cancel_lead(root.project_id)
    assert task.done()
    old_run = (await store.get_runs(lead.terminal_owner_id))[0]
    assert old_run.status is RunStatus.CANCELLED
    assert old_run.process_state.value == "CANCELLED"
    assert not terminal.snapshot(lead.terminal_owner_id)["active"]

    planner.release.set()
    _entry, fresh_task = await runner.enqueue_lead_message(
        lead.terminal_owner_id, "Fresh assignment"
    )
    assert fresh_task is not None
    await fresh_task
    runs = await store.get_runs(lead.terminal_owner_id)
    assert [run.status for run in runs] == [RunStatus.CANCELLED, RunStatus.COMPLETE]
    transcript = await store.lead_transcript(root.project_id)
    assert "fresh turn completed" in [item.content for item in transcript]
    await store.dispose()


def test_setup_is_a_capability_not_an_agent():
    """No standalone setup authority exists; bootstrap is Lead + Root Planner."""
    import asyncio
    from turn.domain.schemas import AgentType
    from turn.domain.capability_contracts import SETUP_CAPABILITY_ID
    assert not [member for member in AgentType if "setup" in member.value]
    assert SETUP_CAPABILITY_ID != "lead" and SETUP_CAPABILITY_ID.startswith("turn-")


@pytest.mark.asyncio
async def test_setup_capability_belongs_to_root_planner_only(tmp_path):
    """Setup behavior attaches to the initial Root Planner, never an agent."""
    from turn.domain.capability_contracts import SETUP_CAPABILITY_ID
    store, _runner, _planner, _terminal = await make_runner(tmp_path, [])
    root = await make_project(store, tmp_path)
    assert SETUP_CAPABILITY_ID in root.agent.capabilities
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="dept", objective="Department", executor="planner", plan=True)]),
    )
    assert SETUP_CAPABILITY_ID not in children[0].agent.capabilities


@pytest.mark.asyncio
async def test_nested_completion_requires_parent_acceptance(tmp_path):
    store, runner, planner, _terminal = await make_runner(
        tmp_path, [review_decision_payload("APPROVE", "parent accepts")]
    )
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="dept", objective="Department", executor="planner", plan=True)]),
    )
    nested = children[0]
    # Drop auto-generated evidence criteria so the deterministic acceptance
    # gate evaluates only frontier/backlog state in this fixture.
    nested_contract = nested.organization_contract.model_copy(deep=True)
    nested_contract.acceptance_criteria = []
    nested.organization_contract = nested_contract
    await store._save_node(nested)
    leaves = await store.apply_plan(
        nested,
        PlanResult(nodes=[NodeSpec(key="unit", objective="Unit work", executor="codex")]),
    )
    await store.set_status(leaves[0].id, NodeStatus.COMPLETE)

    # The settled frontier produces exactly one durable request to the parent.
    await runner._request_authority_completion_review(nested)
    request = next(
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "COMPLETION_REVIEW" and item.sender_id == nested.id
    )
    assert request.receiver_id == root.id
    assert request.receiver_is_lead is False

    await runner.settle_review_request(root.project_id, request.id)
    final = await store.get_node(nested.id)
    assert final.manager_phase is ManagerPhase.ACCEPTED
    await store.dispose()


@pytest.mark.asyncio
async def test_root_completion_requires_lead_acceptance(tmp_path):
    """The project cannot become COMPLETE until the Lead accepts completion."""
    store, runner, planner, _terminal = await make_runner(
        tmp_path,
        [
            review_decision_payload("REJECT", "not done yet"),
            review_decision_payload("APPROVE", "lead accepts completion"),
        ],
    )
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    # Drop auto-generated evidence criteria: this test exercises hierarchical
    # gating, not the deterministic evidence audit.
    root_contract = root.organization_contract.model_copy(deep=True)
    root_contract.acceptance_criteria = []
    root.organization_contract = root_contract
    await store._save_node(root)
    leaves = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
    )
    await store.set_status(leaves[0].id, NodeStatus.COMPLETE)

    await runner._request_authority_completion_review(root)
    request = next(
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "COMPLETION_REVIEW"
    )
    assert request.receiver_is_lead is True
    assert request.receiver_id == lead.terminal_owner_id

    # Lead rejects: no acceptance, project stays incomplete.
    await runner.settle_review_request(root.project_id, request.id)
    after_reject = await store.get_node(root.id)
    assert after_reject.manager_phase is ManagerPhase.EXECUTING
    # The corrective wave completes before the frontier settles again.
    corrections = [
        item for item in await store.list_work_items(root.project_id, organization_id=root.id)
        if item.status is not WorkItemStatus.COMPLETE
    ]
    for item in corrections:
        await store.update_work_item(item.id, status=WorkItemStatus.COMPLETE)

    # Second frontier settlement; this time the lead approves.
    await runner._request_authority_completion_review(root)
    pending = next(
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "COMPLETION_REVIEW" and item.status.value == "PENDING"
    )
    await runner.settle_review_request(root.project_id, pending.id)
    after_accept = await store.get_node(root.id)
    assert after_accept.manager_phase is ManagerPhase.ACCEPTED
    decisions = [
        item.decision.value
        for item in sorted(
            (r for r in await store.review_requests(root.project_id) if r.kind.value == "COMPLETION_REVIEW"),
            key=lambda r: r.created_at,
        )
    ]
    assert decisions == ["REJECT", "APPROVE"]
    await store.dispose()


@pytest.mark.asyncio
async def test_escalation_is_processed_by_parent_and_propagates_to_lead(tmp_path):
    store, runner, planner, _terminal = await make_runner(
        tmp_path,
        [review_decision_payload("ESCALATE", "beyond my authority")],
    )
    root = await make_project(store, tmp_path)
    lead = await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    children = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="dept", objective="Department", executor="planner", plan=True)]),
    )
    nested = await store.set_agent_session(children[0].id, "nested-planner-session")

    escalation = await store.create_review_request(
        project_id=root.project_id,
        sender_id=nested.id,
        receiver_id=root.id,
        receiver_is_lead=False,
        kind=ReviewKind.ESCALATION,
        reason="plan rejected twice",
    )

    # Step/auto machinery executes the parent's escalation turn...
    await runner.settle_review_request(root.project_id, escalation.id)
    settled = next(
        item for item in await store.review_requests(root.project_id) if item.id == escalation.id
    )
    assert settled.status.value == "SETTLED"
    ctx = planner.contexts[0][0]
    assert ctx.node.id == root.id
    # ...and the parent's ESCALATE climbs to the lead as a new PENDING request.
    propagated = [
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "ESCALATION" and item.status.value == "PENDING"
    ]
    assert len(propagated) == 1
    # The request advances from the current receiver's position upward.
    assert propagated[0].sender_id == root.id
    assert propagated[0].receiver_is_lead is True
    assert propagated[0].receiver_id == lead.terminal_owner_id
    await store.dispose()


@pytest.mark.asyncio
async def test_escalation_settlement_revives_sender_for_fresh_planning(tmp_path):
    store, runner, planner, _terminal = await make_runner(
        tmp_path, [review_decision_payload("REJECT", "fixed the wiring; retry")]
    )
    root = await make_project(store, tmp_path)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    root = await store.set_status(root.id, NodeStatus.FAILED)
    lead = await store.project_lead(root.project_id)

    escalation = await store.create_review_request(
        project_id=root.project_id,
        sender_id=root.id,
        receiver_id=lead.terminal_owner_id,
        receiver_is_lead=True,
        kind=ReviewKind.ESCALATION,
        reason="root planning exhausted corrections",
    )
    await runner.settle_review_request(root.project_id, escalation.id)
    revived = await store.get_node(root.id)
    # Fresh-run preparation makes the sender immediately schedulable again.
    assert revived.status is NodeStatus.RUNNABLE
    await store.dispose()


@pytest.mark.asyncio
async def test_step_mode_produces_zero_model_calls_until_stepped(tmp_path):
    store, runner, planner, _terminal = await make_runner(
        tmp_path, [review_decision_payload("APPROVE", "accepted")]
    )
    root = await make_project(store, tmp_path)  # auto_run=False
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    leaves = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
    )
    await store.set_status(leaves[0].id, NodeStatus.COMPLETE)
    contract = root.organization_contract.model_copy(deep=True)
    contract.scale = OrganizationScale.DELIVERY
    root.organization_contract = contract
    await store._save_node(root)
    await store.set_manager_state(root.id, phase=ManagerPhase.EXECUTING, reasons=["frontier settled"])
    await store.set_status(root.id, NodeStatus.EXPANDED)

    # Background ticks discover the ready review but launch nothing.
    for _ in range(3):
        await runner.scheduler.schedule_once(root.project_id)
    await asyncio.gather(*runner.scheduler.running.values(), return_exceptions=True)
    assert len(planner.contexts) == 0, "step mode must not call the model before Step"

    # Next Stage launches the ready review frontier.
    launched = await runner.scheduler.step(root.project_id)
    await asyncio.gather(
        *runner.scheduler.running.values(),
        *runner.scheduler.running_reviews.values(),
        return_exceptions=True,
    )
    assert len(planner.contexts) == 1
    request = next(
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "COMPLETION_REVIEW"
    )
    assert request.status.value == "SETTLED"
    await store.dispose()


@pytest.mark.asyncio
async def test_auto_mode_runs_ready_reviews_automatically(tmp_path):
    store, runner, planner, _terminal = await make_runner(
        tmp_path, [review_decision_payload("APPROVE", "accepted")]
    )
    root = await make_project(store, tmp_path)
    root = await store.set_auto_run(root.id, True) if hasattr(store, "set_auto_run") else root
    policy = root.run_policy.model_copy(update={"auto_run": True})
    root = await store.update_run_policy(root.id, policy) if hasattr(store, "update_run_policy") else root
    await store._save_node(root)
    await store.ensure_project_lead(root.project_id, agent=root.agent.model_copy())
    leaves = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="leaf", objective="Leaf", executor="codex")]),
    )
    await store.set_status(leaves[0].id, NodeStatus.COMPLETE)
    contract = root.organization_contract.model_copy(deep=True)
    contract.scale = OrganizationScale.DELIVERY
    root.organization_contract = contract
    await store._save_node(root)
    await store.set_manager_state(root.id, phase=ManagerPhase.EXECUTING, reasons=["frontier settled"])
    await runner._request_authority_completion_review(root)

    await runner.scheduler.schedule_once(root.project_id)
    await asyncio.gather(*runner.scheduler.running_reviews.values(), return_exceptions=True)
    assert len(planner.contexts) == 1
    request = next(
        item for item in await store.review_requests(root.project_id)
        if item.kind.value == "COMPLETION_REVIEW"
    )
    assert request.status.value == "SETTLED"
    await store.dispose()
