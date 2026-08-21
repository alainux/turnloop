from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.organization import (
    AcceptanceEvidence,
    EvidenceStatus,
    ManagerDecision,
    ManagerPhase,
    OrganizationContract,
    PlanAuditDecision,
    PlanAuditResult,
    OrganizationScale,
    WorkItemStatus,
)
from turn.domain.schemas import (
    AgentConfig,
    ArtifactKind,
    ManagerResult,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    RunStatus,
    Usage,
    WorkerResult,
)
from turn.runner.events import EventBus
from turn.runner.organization import OrganizationManager
from turn.runner.runner import Runner
from turn.domain.state_machine import present_node
from turn.workers.registry import WorkerRegistry
from turn.tests.mocks import MockTerminalTransport
from turn.testing.mocks import MockHerdrAdapter


class StructuredReviewPlanner:
    name = "structured-review-fixture"

    def __init__(self, responses):
        self.responses = list(responses)
        self.contexts = []

    async def call_structured(self, ctx, _prompt, *, handoff_kind):
        self.contexts.append((ctx, handoff_kind))
        return self.responses.pop(0), Usage(input_tokens=3, output_tokens=5), "review-session"


class ReconcileTrackingTerminal(MockTerminalTransport):
    """Mock the provider inventory repair required before a manager resume."""

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


@pytest.mark.asyncio
async def test_invalid_live_handoff_enters_correction_without_false_failure_or_duplicate_run(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "small correction fixture",
        repo_path=str(tmp_path / "projects" / "correction"),
        agent=AgentConfig(harness="mock"),
    )
    (leaf,) = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="leaf", objective="Accept a corrected handoff", executor="mock")]),
    )
    await store.set_agent_session(leaf.id, "live-provider-session")
    await store.set_status(leaf.id, NodeStatus.RUNNING)
    run = await store.create_run(leaf, "mock")
    terminal = MockTerminalTransport()
    terminal.supports_inject = True
    await terminal.ensure_persistent_shell(leaf.id)
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(data_dir=str(tmp_path / "turn"), projects_dir=str(tmp_path / "projects")),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=terminal,
    )
    repo = Path(root.repo_path or "")
    handoff = repo / ".turn" / "interactive" / f"{leaf.id}.result.json"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    runner._recovered_run_ids[leaf.id] = run.id
    watcher = asyncio.create_task(
        runner._watch_agent_handoffs(leaf.id, root.id, runner._handoff_paths(str(repo), leaf.id), str(repo))
    )
    try:
        handoff.write_text(
            '{"outcome":"COMPLETE","artifacts":[{"kind":"not-a-turn-kind","name":"bad"}]}',
            encoding="utf-8",
        )
        for _ in range(80):
            current = await store.get_node(leaf.id)
            if current and current.agent_state == "correction_required":
                break
            await asyncio.sleep(0.025)
        rejected = await store.get_node(leaf.id)
        assert rejected is not None
        assert rejected.status is NodeStatus.RUNNING
        assert rejected.agent_state == "correction_required"
        presentation = present_node(rejected)
        assert presentation.state.value == "correction_required"
        assert "retry" not in {action.value for action in presentation.actions}
        assert "run" not in {action.value for action in presentation.actions}
        assert terminal.snapshot(leaf.id)["active"] is False
        assert terminal._node(leaf.id)["persistent"] is True

        handoff.write_text('{"outcome":"COMPLETE","summary":"corrected"}', encoding="utf-8")
        for _ in range(100):
            current = await store.get_node(leaf.id)
            if current and current.status is NodeStatus.COMPLETE and current.agent_state is None:
                break
            await asyncio.sleep(0.025)
        completed = await store.get_node(leaf.id)
        assert completed is not None
        assert completed.status is NodeStatus.COMPLETE
        assert completed.agent_state is None
        assert len(await store.get_runs(leaf.id)) == 1
        assert (await store.get_node(root.id)).status is not NodeStatus.FAILED
    finally:
        runner._stop = True
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await store.dispose()


@pytest.mark.asyncio
async def test_worker_evidence_is_persisted_and_missing_criteria_requires_correction(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "evidence fixture",
        repo_path=str(tmp_path / "projects" / "evidence"),
        agent=AgentConfig(harness="mock"),
    )
    criterion = {"id": "smoke", "description": "The smoke path is exercised."}
    good, bad = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(
                    key="good",
                    objective="Good evidence",
                    executor="deterministic",
                    acceptance_criteria=[criterion],
                ),
                NodeSpec(
                    key="bad",
                    objective="Missing evidence",
                    executor="deterministic",
                    acceptance_criteria=[criterion],
                ),
            ]
        ),
    )
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    )

    good_run = await store.create_run(good, "deterministic")
    await store.set_status(good.id, NodeStatus.RUNNING)
    await runner._handle_outcome(
        good,
        good_run,
        root.id,
        WorkerResult(
            outcome=Outcome.COMPLETE,
            summary="verified",
            evidence=[
                AcceptanceEvidence(
                    criterion_id="smoke",
                    status=EvidenceStatus.PASS,
                    summary="The smoke path passed.",
                    refs=["reports/smoke.txt"],
                )
            ],
        ),
    )
    good_after = await store.get_node(good.id)
    assert good_after.status is NodeStatus.COMPLETE
    assert (await store.get_runs(good.id))[0].status is RunStatus.COMPLETE
    artifacts = (await store.get_graph(root.id)).artifacts
    evidence = [
        artifact
        for artifact in artifacts
        if artifact.node_id == good.id and artifact.kind is ArtifactKind.EVIDENCE
    ]
    assert len(evidence) == 1
    assert evidence[0].content["criterion_id"] == "smoke"
    assert evidence[0].evidence_refs == ["reports/smoke.txt"]

    bad_run = await store.create_run(bad, "deterministic")
    await store.set_status(bad.id, NodeStatus.RUNNING)
    await runner._handle_outcome(
        bad,
        bad_run,
        root.id,
        WorkerResult(outcome=Outcome.COMPLETE, summary="claimed complete"),
    )
    bad_after = await store.get_node(bad.id)
    assert bad_after.status is NodeStatus.RUNNING
    assert bad_after.agent_state == "correction_required"
    bad_saved_run = (await store.get_runs(bad.id))[0]
    assert bad_saved_run.status is RunStatus.RUNNING
    assert bad_saved_run.outcome is None
    assert bad_saved_run.accepted_submission is False
    await store.dispose()


@pytest.mark.asyncio
async def test_worker_evidence_must_be_passing_and_referenced_before_acceptance(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "evidence quality fixture",
        repo_path=str(tmp_path / "projects" / "evidence-quality"),
        agent=AgentConfig(harness="mock"),
    )
    node, = await store.apply_plan(
        root,
        PlanResult(
            nodes=[
                NodeSpec(
                    key="quality",
                    objective="Quality evidence",
                    executor="deterministic",
                    acceptance_criteria=[{"id": "check", "description": "Checked."}],
                )
            ]
        ),
    )
    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    )

    for evidence in (
        AcceptanceEvidence(
            criterion_id="check",
            status=EvidenceStatus.UNVERIFIED,
            summary="The check was not run.",
            refs=["notes.md"],
        ),
        AcceptanceEvidence(
            criterion_id="check",
            status=EvidenceStatus.PASS,
            summary="The check passed but no inspectable reference was supplied.",
            refs=[],
        ),
    ):
        run = await store.create_run(node, "deterministic")
        await store.set_status(node.id, NodeStatus.RUNNING)
        await runner._handle_outcome(
            node,
            run,
            root.id,
            WorkerResult(outcome=Outcome.COMPLETE, summary="claimed complete", evidence=[evidence]),
        )
        saved_run = (await store.get_runs(node.id))[-1]
        assert saved_run.status is RunStatus.RUNNING
        assert saved_run.outcome is None
        saved_node = await store.get_node(node.id)
        assert saved_node.status is NodeStatus.RUNNING
        assert saved_node.agent_state == "correction_required"
        await store.set_status(node.id, NodeStatus.RUNNING)

    await store.dispose()


@pytest.mark.asyncio
async def test_manager_backlog_and_phase_survive_restart(tmp_path):
    data_dir = tmp_path / "turn"
    projects_dir = tmp_path / "projects"
    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "restartable organization",
        repo_path=str(projects_dir / "restartable"),
    )
    decision = await OrganizationManager().apply_result(
        store,
        root.id,
        ManagerResult(
            decision=ManagerDecision.CONTINUE,
            summary="persist the first ticket",
            work_items=[
                {
                    "key": "first",
                    "title": "First ticket",
                    "instructions": "Complete the first ticket.",
                }
            ],
        ),
    )
    assert decision.decision is ManagerDecision.CONTINUE
    await store.dispose()

    restarted = Store(data_dir, projects_dir=projects_dir)
    await restarted.init()
    restored = await restarted.get_node(root.id)
    assert restored.manager_phase.value == "EXECUTING"
    items = await restarted.list_work_items(root.id)
    assert len(items) == 1
    assert items[0].key == "first"
    assert items[0].status is WorkItemStatus.BACKLOG
    materialized = await restarted.materialize_ready_work_items(root.id)
    assert [node.objective for node in materialized] == ["First ticket"]
    await restarted.dispose()


@pytest.mark.asyncio
async def test_manager_continue_accepts_same_response_work_item_dependencies(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "organization with a dependent work wave",
        repo_path=str(tmp_path / "projects" / "dependent-wave"),
    )
    await OrganizationManager().apply_result(
        store,
        root.id,
        ManagerResult(
            decision=ManagerDecision.CONTINUE,
            summary="create an ordered correction wave",
            work_items=[
                {"key": "A", "title": "Prepare input", "instructions": "Prepare the input."},
                {"key": "B", "title": "Use input", "instructions": "Use the prepared input.", "depends_on": ["A"]},
            ],
        ),
    )
    items = await store.list_work_items(root.id, organization_id=root.id)
    by_key = {item.key: item for item in items}
    assert set(by_key) == {"A", "B"}
    assert by_key["B"].depends_on == [by_key["A"].id]
    await store.dispose()


@pytest.mark.asyncio
async def test_control_plane_audit_retries_without_turning_failure_into_rejection(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "organization audit retry",
        repo_path=str(tmp_path / "projects" / "audit-retry"),
        agent=AgentConfig(harness="mock"),
    )
    attempts = 0

    async def audit(_contract, _plan):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("\x1b[2Jtemporary transport failure")
        return PlanAuditResult(
            decision=PlanAuditDecision.APPROVE,
            summary="approved after retry",
        )

    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(data_dir=str(tmp_path / "turn"), projects_dir=str(tmp_path / "projects")),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
        semantic_plan_auditor=audit,
    )
    result = await runner._run_semantic_plan_audit(root, root.organization_contract, PlanResult(nodes=[]))
    assert result.decision is PlanAuditDecision.APPROVE
    assert attempts == 2
    runs = await store.get_runs(root.id)
    assert [run.status for run in runs[-2:]] == [RunStatus.FAILED, RunStatus.COMPLETE]
    assert "\x1b" not in (runs[-2].error or "")
    assert (await store.get_node(root.id)).status is not NodeStatus.FAILED
    await store.dispose()


@pytest.mark.asyncio
async def test_manager_control_failure_stays_review_pending_and_exposes_reviewing(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "organization manager retry",
        repo_path=str(tmp_path / "projects" / "manager-retry"),
        agent=AgentConfig(harness="mock"),
    )
    await store.set_status(root.id, NodeStatus.EXPANDED)
    observed: list[ManagerPhase | None] = []

    async def reviewer(_snapshot):
        observed.append((await store.get_node(root.id)).manager_phase)
        await asyncio.sleep(0.01)
        raise RuntimeError("\x1b[31mprovider unavailable\x1b[0m")

    runner = Runner(
        store,
        events=EventBus(),
        settings=Settings(data_dir=str(tmp_path / "turn"), projects_dir=str(tmp_path / "projects")),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
        manager_reviewer=reviewer,
    )
    decisions = await asyncio.gather(
        *(runner._review_organizations(root.id, boundaries=[root]) for _ in range(3))
    )
    current = await store.get_node(root.id)
    assert decisions == [[], [], []]
    assert observed == [ManagerPhase.REVIEWING]
    assert current.manager_phase is ManagerPhase.REVIEW_PENDING
    assert current.manager_phase is not ManagerPhase.BLOCKED
    assert "\x1b" not in " ".join(current.manager_review_reasons)
    assert current.organization_review is not None
    assert current.organization_review.control_retry_required is True
    # A failed control operation is resumable, not an auto-run trigger. A
    # scheduler heartbeat must not launch another provider attempt by itself.
    await runner._review_safe_organizations(root.id)
    assert observed == [ManagerPhase.REVIEWING]
    await runner._review_organizations(root.id, boundaries=[root])
    assert observed == [ManagerPhase.REVIEWING]
    await store.dispose()


@pytest.mark.asyncio
async def test_provider_review_adapters_require_typed_artifacts_and_session_boundaries(tmp_path):
    """Root plan review runs on the project lead; manager review resumes the
    boundary's own session. No synthetic reviewer process owner exists."""
    audit_payload = {
        "outcome": "COMPLETE",
        "summary": "audit returned",
        "artifacts": [
            {
                "kind": ArtifactKind.JSON.value,
                "name": "plan-audit",
                "schema_name": "turn.plan-audit",
                "schema_version": "v1",
                "content": {
                    "decision": "APPROVE",
                    "summary": "The plan is coherent.",
                    "findings": [],
                    "required_changes": [],
                },
            }
        ],
    }
    manager_payload = {
        "outcome": "COMPLETE",
        "summary": "review returned",
        "artifacts": [
            {
                "kind": ArtifactKind.JSON.value,
                "name": "review-decision",
                "schema_name": "turn.review-decision",
                "schema_version": "v1",
                "content": {
                    "decision": "ESCALATE",
                    "summary": "Need a human decision.",
                    "required_changes": [],
                    "work_items": [],
                    "missing_inputs": ["choose a game engine"],
                },
            }
        ],
    }
    planner = StructuredReviewPlanner([audit_payload, manager_payload])
    registry = WorkerRegistry()
    registry.register_planner(planner, key="real")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "Build a game",
        repo_path=str(tmp_path / "projects" / "game"),
        agent=AgentConfig(
            harness="codex",
            model="gpt-5.6-luna",
        ),
    )
    root = await store.set_agent_session(root.id, "retained-planner-session")
    lead_agent = AgentConfig(harness="codex", model="gpt-5.6-luna")
    lead = await store.ensure_project_lead(root.project_id, agent=lead_agent)
    terminal = ReconcileTrackingTerminal()
    runner = Runner(
        store,
        registry=registry,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
        terminal_transport=terminal,
    )
    runner.provider_reviews_enabled = True
    contract = root.organization_contract
    assert contract is not None
    audit = await runner._run_semantic_plan_audit(
        root,
        contract,
        PlanResult(nodes=[]),
    )
    assert audit.decision.value == "APPROVE"
    # The lead turn starts from the lead's own retained session (none yet).
    assert planner.contexts[0][0].node.agent.session_id is None
    assert planner.contexts[0][0].node.id == lead.terminal_owner_id
    # The lead's harness session is retained on the lead record.
    retained_lead = await store.project_lead(root.project_id)
    assert retained_lead.session_id == "review-session"
    # Root completion acceptance belongs to the lead. Recording the durable
    # request launches nothing; settling it runs one bounded lead turn.
    await runner._request_authority_completion_review(root)
    assert len(planner.contexts) == 1
    pending = next(
        request for request in await store.review_requests(root.project_id)
        if request.kind.value == "COMPLETION_REVIEW"
        and request.status.value == "PENDING"
    )
    assert pending.receiver_is_lead is True
    assert pending.receiver_id == lead.terminal_owner_id

    await runner.settle_review_request(root.project_id, pending.id)
    # The lead's review turn ran on the lead's own identity and pane.
    assert planner.contexts[1][0].node.id == lead.terminal_owner_id
    assert lead.terminal_owner_id in terminal.close_requests
    runs = await store.get_runs(lead.terminal_owner_id)
    assert {run.worker for run in runs} == {"project-lead"}
    assert all(run.process_owner_id == lead.terminal_owner_id for run in runs)
    assert all(run.process_state.value == "EXITED" for run in runs)
    # ESCALATE at the top of the hierarchy blocks on explicit user input,
    # visibly: the boundary is BLOCKED and the trail says so.
    refreshed = await store.get_node(root.id)
    assert refreshed.status.value == "BLOCKED"
    requests = await store.review_requests(root.project_id)
    assert {request.kind.value for request in requests} == {
        "PLAN_REVIEW",
        "COMPLETION_REVIEW",
    }
    plan_request = next(
        request for request in requests if request.kind.value == "PLAN_REVIEW"
    )
    assert plan_request.receiver_is_lead is True
    assert plan_request.decision.value == "APPROVE"
    assert plan_request.status.value == "SETTLED"
    completion = next(
        request for request in requests if request.kind.value == "COMPLETION_REVIEW"
    )
    assert completion.status.value == "SETTLED"
    assert completion.summary.startswith("blocked on user input")
    await store.dispose()


def test_manager_result_normalizes_provider_completion_report():
    normalized = Runner._normalize_manager_result_payload(
        {
            "decision": "ACCEPT",
            "summary": "The architecture is ready.",
            "plan": {
                "completed_nodes": ["foundation", "compose"],
                "evidence_refs": ["ARCHITECTURE_CONTRACT.md"],
                "exported_handoff": "project-management-architecture@1",
                "status": "ACCEPT",
            },
        }
    )

    result = ManagerResult.model_validate(normalized)
    assert result.decision.value == "ACCEPT"
    assert result.summary == "The architecture is ready."
    assert result.plan is None


def test_manager_result_normalizes_provider_work_item_aliases():
    normalized = Runner._normalize_manager_result_payload(
        {
            "decision": "CONTINUE",
            "summary": "Reconcile the remaining acceptance evidence.",
            "work_items": [
                {
                    "key": "reconcile-evidence",
                    "objective": "Reconcile architecture acceptance evidence",
                    "acceptance_criteria": ["the evidence is complete"],
                    "dependencies": ["compose"],
                }
            ],
        }
    )

    result = ManagerResult.model_validate(normalized)
    item = result.work_items[0]
    assert item.title == "Reconcile architecture acceptance evidence"
    assert item.instructions == "Reconcile architecture acceptance evidence"
    assert item.depends_on == ["compose"]
    assert item.acceptance_criteria[0].description == "the evidence is complete"


@pytest.mark.asyncio
async def test_resume_materialized_organization_review_preserves_graph(tmp_path):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "resume the broad organization",
        repo_path=str(tmp_path / "projects" / "resume"),
        agent=AgentConfig(harness="mock"),
    )
    root.organization_contract = OrganizationContract(
        charter="resume the organization without replanning",
        scale=OrganizationScale.ORGANIZATION,
        acceptance_criteria=["the retained frontier remains executable"],
    )
    await store._save_node(root)
    child = await store.create_node(
        project_id=root.id,
        parent_id=root.id,
        objective="Retained frontier",
        executor="deterministic",
        status=NodeStatus.COMPLETE,
    )
    await store.set_status(root.id, NodeStatus.FAILED)
    await store.set_manager_state(
        root.id,
        phase=ManagerPhase.REVIEW_PENDING,
        reasons=["provider review failed"],
    )

    runner = Runner(
        store,
        settings=Settings(
            data_dir=str(tmp_path / "turn"),
            projects_dir=str(tmp_path / "projects"),
        ),
        terminal_transport=MockTerminalTransport(),
    )
    resumed = await runner.resume_organization_review(root.id)

    assert resumed.status is NodeStatus.EXPANDED
    assert resumed.manager_phase is ManagerPhase.REVIEW_PENDING
    assert resumed.organization_review.last_reason == (
        "organization review resumed after provider failure"
    )
    assert (await store.get_node(child.id)).status is NodeStatus.COMPLETE
    await store.dispose()


def test_provider_review_artifact_requires_exact_schema_identity():
    with pytest.raises(RuntimeError, match="no 'plan-audit' artifact"):
        Runner._structured_artifact_payload(
            {
                "outcome": "COMPLETE",
                "artifacts": [
                    {
                        "kind": ArtifactKind.JSON.value,
                        "name": "plan-audit",
                        "schema_name": "turn.other-audit",
                        "schema_version": "v1",
                        "content": {},
                    }
                ],
            },
            schema_name="turn.plan-audit",
            artifact_name="plan-audit",
        )

    with pytest.raises(RuntimeError, match="no 'plan-audit' artifact"):
        Runner._structured_artifact_payload(
            {
                "outcome": "COMPLETE",
                "artifacts": [
                    {
                        "kind": ArtifactKind.JSON.value,
                        "name": "plan-audit",
                        "schema_name": "turn.plan-audit",
                        "schema_version": "v2",
                        "content": {},
                    }
                ],
            },
            schema_name="turn.plan-audit",
            artifact_name="plan-audit",
        )


def test_plan_audit_normalizes_structured_provider_findings():
    normalized = Runner._normalize_plan_audit_payload(
        {
            "decision": "REJECT",
            "summary": "The plan needs a correction.",
            "findings": [
                {
                    "area": "ownership",
                    "finding": "A material boundary has no contract.",
                    "severity": "error",
                }
            ],
            "required_changes": [
                {"area": "contracts", "description": "Add the missing charter."}
            ],
        }
    )

    assert normalized["findings"] == [
        "ownership: error: A material boundary has no contract."
    ]
    assert normalized["required_changes"] == [
        "contracts: Add the missing charter."
    ]


def test_nested_planner_keeps_an_explicit_broader_contract_for_materialization():
    node = Node(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        objective="Plan architecture",
        organization_contract=OrganizationContract(
            charter="architecture",
            scale=OrganizationScale.DELIVERY,
            acceptance_criteria=["architecture is consumable"],
        ),
    )
    plan = PlanResult(
        organization_contract=OrganizationContract(
            charter="architecture organization",
            scale=OrganizationScale.ORGANIZATION,
            acceptance_criteria=["architecture is consumable"],
        ),
        nodes=[],
    )

    resolved = Runner._organization_contract_for_plan(node, plan)

    assert resolved is plan.organization_contract
    assert resolved.scale is OrganizationScale.ORGANIZATION
