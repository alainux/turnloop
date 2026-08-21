"""First-class contracts for accountable Turn organizations.

The workgraph still stores nodes and edges, but a planner boundary also owns a
durable charter.  Keeping these records separate from provider-specific agent
configuration makes the manager loop, ticket board, and plan audit usable by
every harness.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationScale(str, Enum):
    FOCUSED = "focused"
    DELIVERY = "delivery"
    ORGANIZATION = "organization"


class OrganizationPhase(str, Enum):
    PLAN = "PLAN"
    EXECUTE_FRONTIER = "EXECUTE_FRONTIER"
    OBSERVE = "OBSERVE"
    REVIEW = "REVIEW"
    REPLAN = "REPLAN"
    ACCEPT_CHARTER = "ACCEPT_CHARTER"
    BLOCKED = "BLOCKED"


class WorkItemStatus(str, Enum):
    BACKLOG = "BACKLOG"
    OPEN = "BACKLOG"
    ACTIVE = "ACTIVE"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"
    DONE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

    @classmethod
    def _missing_(cls, value):
        # Accept the compact brief vocabulary at API boundaries while keeping
        # the persisted names used by existing Turn state files stable.
        return {"OPEN": cls.BACKLOG, "DONE": cls.COMPLETE}.get(value)


class HandoffStatus(str, Enum):
    EXPECTED = "EXPECTED"
    AVAILABLE = "AVAILABLE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class BudgetRequestStatus(str, Enum):
    """Lifecycle of a request to change an organization's hard budget."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class WorkspaceIsolation(str, Enum):
    """Filesystem strategy for concurrent workers in one project."""

    SHARED = "shared"
    WORKTREE = "worktree"


class AcceptanceCriterion(BaseModel):
    """One explicit, evidence-bearing condition for accepting work."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class AcceptanceEvidence(BaseModel):
    """A worker's claim about one criterion, with inspectable references."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1, max_length=120)
    status: EvidenceStatus
    summary: str = Field(min_length=1)
    refs: list[str] = Field(default_factory=list)


class ManagerPhase(str, Enum):
    PLANNING = "PLANNING"
    EXECUTING = "EXECUTING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEWING = "REVIEWING"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"


class ManagerDecision(str, Enum):
    ACCEPT = "ACCEPT"
    CONTINUE = "CONTINUE"
    BLOCK = "BLOCK"


class PlanAuditDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class PlanAuditResult(BaseModel):
    """Provider-neutral structured result for an optional fresh semantic audit."""

    model_config = ConfigDict(extra="forbid")

    decision: PlanAuditDecision
    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)


class WorkItemSpec(BaseModel):
    """A manager's bounded request to add future work to its backlog."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=200)
    instructions: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    agent_type: str = "executor"
    depends_on: list[str] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    organization_contract: "OrganizationContract | None" = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> "WorkItemSpec":
        if self.key in self.depends_on:
            raise ValueError("work item cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("work item dependencies must be unique")
        return self


class WorkspaceRef(BaseModel):
    """Minimal durable identity for a node's isolated Git workspace."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    branch: str = Field(min_length=1)


class OrganizationBudget(BaseModel):
    """Hard limits allocated to one organization boundary."""

    model_config = ConfigDict(extra="forbid")

    # None means that this boundary inherits the project/global scheduler
    # capacity.  A default numeric cap made every organization silently act
    # like a four-worker deployment, regardless of the run policy.
    max_active_workers: int | None = Field(default=None, ge=1, le=10_000)
    max_tokens: int | None = Field(default=None, ge=1)
    max_total_runs: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)


class OrganizationContract(BaseModel):
    """The charter that a planner must preserve while shaping a subtree."""

    model_config = ConfigDict(extra="forbid")

    charter: str = Field(min_length=1)
    scale: OrganizationScale = OrganizationScale.FOCUSED
    deliverables: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    quality_policy: list[str] = Field(default_factory=list)
    decomposition_policy: str = (
        "recurse until every worker owns one cohesive, verifiable leaf contract"
    )
    completion_policy: str = (
        "accept only after exported deliverables and independent evidence are present"
    )
    budget: OrganizationBudget = Field(default_factory=OrganizationBudget)
    min_first_level_production_owners: int = Field(default=1, ge=1)
    require_independent_verification: bool = False
    max_replans: int = Field(default=3, ge=0, le=100)

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def normalize_acceptance_criteria(cls, value):
        """Migrate compact historical string criteria without losing meaning."""
        if value is None:
            return []
        return [
            item
            if isinstance(item, (dict, AcceptanceCriterion))
            else {"id": f"criterion-{index + 1}", "description": str(item)}
            for index, item in enumerate(value)
        ]

    @classmethod
    def from_objective(cls, objective: str) -> "OrganizationContract":
        """Create a conservative charter for old and newly created projects.

        This is a classification hint, not the plan acceptance mechanism.  A
        planner can replace the generated contract with a more precise one in
        its handoff; the independent audit remains the authority.
        """
        clean = " ".join(objective.split())
        lowered = clean.casefold()
        # Bootstrap only a scale hint from language that describes scope or
        # completeness.  The hint is deliberately domain-neutral: the
        # planner's explicit contract and the independent audit remain the
        # authority, so a request is never classified by a canned industry
        # topology or department vocabulary.
        markers = (
            "organization",
            "enterprise",
            "ecosystem",
            "app factory",
            "multi-product",
            "multi product",
            "large-scale",
            "large scale",
            "full-scale",
            "full scale",
            "not a demo",
            "not a poc",
            "entire organization",
            "multiple teams",
            "multiple products",
        )
        delivery_markers = (
            "complete",
            "usable",
            "ship",
            "launch",
            "deliver",
        )
        small_scope = any(
            marker in lowered for marker in ("small", "tiny", "narrow", "focused")
        )
        if any(marker in lowered for marker in markers) and not small_scope:
            scale = OrganizationScale.ORGANIZATION
            owners = 2
            verify = True
        elif any(marker in lowered for marker in delivery_markers) and not small_scope:
            scale = OrganizationScale.DELIVERY
            owners = 1
            verify = True
        else:
            scale = OrganizationScale.FOCUSED
            owners = 1
            verify = False
        return cls(
            charter=clean,
            scale=scale,
            deliverables=[clean],
            acceptance_criteria=[
                AcceptanceCriterion(
                    id="deliverable-usable",
                    description=f"the requested deliverable is usable and demonstrable: {clean}",
                ),
            ],
            min_first_level_production_owners=owners,
            require_independent_verification=verify,
        )


class OrganizationReview(BaseModel):
    """Durable manager-loop observation for one planning boundary."""

    model_config = ConfigDict(extra="forbid")

    phase: OrganizationPhase = OrganizationPhase.PLAN
    revision: int = Field(default=0, ge=0)
    last_reason: str | None = None
    audit: PlanAudit | None = None
    reviewed_at: datetime | None = None
    replan_requested: bool = False
    review_count: int = Field(default=0, ge=0)
    accept_count: int = Field(default=0, ge=0)
    continue_count: int = Field(default=0, ge=0)
    block_count: int = Field(default=0, ge=0)
    last_decision: ManagerDecision | None = None
    # Operational audit information only. These fields intentionally contain
    # findings and requested changes, never provider reasoning traces.
    audit_decision: PlanAuditDecision | None = None
    audit_summary: str | None = None
    audit_findings: list[str] = Field(default_factory=list)
    audit_required_changes: list[str] = Field(default_factory=list)
    audit_correction_count: int = Field(default=0, ge=0)
    audit_updated_at: datetime | None = None
    # A provider/control failure is retryable, but must require an explicit
    # resume. Otherwise an auto-run heartbeat immediately re-enters the same
    # failed review and can launch an unbounded stream of control attempts.
    control_retry_required: bool = False
    control_failure_reason: str | None = None


class PlanAudit(BaseModel):
    """Independent structural acceptance result for a proposed plan."""

    model_config = ConfigDict(extra="forbid")

    accepted: bool
    score: float = Field(ge=0, le=1)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    direct_node_count: int = Field(default=0, ge=0)
    planner_count: int = Field(default=0, ge=0)
    integrator_count: int = Field(default=0, ge=0)
    verifier_count: int = Field(default=0, ge=0)
    production_owner_count: int = Field(default=0, ge=0)
    has_convergence: bool = False
    has_independent_verification: bool = False
    ownership_compression: float = Field(default=0, ge=0, le=1)
    audited_at: datetime = Field(default_factory=_utcnow)


class OrganizationMetrics(BaseModel):
    """Materialized organization-shape signals for the quality dashboard."""

    model_config = ConfigDict(extra="forbid")

    boundary_count: int = Field(default=0, ge=0)
    planner_count: int = Field(default=0, ge=0)
    max_depth: int = Field(default=0, ge=0)
    production_leaf_count: int = Field(default=0, ge=0)
    planner_to_leaf_ratio: float = Field(default=0, ge=0)
    max_ownership_compression: float = Field(default=0, ge=0, le=1)
    average_ownership_compression: float = Field(default=0, ge=0, le=1)
    converged_boundary_count: int = Field(default=0, ge=0)
    verified_boundary_count: int = Field(default=0, ge=0)
    orphan_production_branches: int = Field(default=0, ge=0)
    fanout_boundary_count: int = Field(default=0, ge=0)
    convergence_boundary_count: int = Field(default=0, ge=0)
    fanout_to_fanin_ratio: float = Field(default=0, ge=0, le=1)
    replan_count: int = Field(default=0, ge=0)
    work_item_count: int = Field(default=0, ge=0)
    completed_work_item_count: int = Field(default=0, ge=0)
    handoff_count: int = Field(default=0, ge=0)
    accepted_handoff_count: int = Field(default=0, ge=0)
    budget_spent_usd: float = Field(default=0, ge=0)
    manager_iteration_count: int = Field(default=0, ge=0)
    manager_accept_count: int = Field(default=0, ge=0)
    manager_continue_count: int = Field(default=0, ge=0)
    manager_block_count: int = Field(default=0, ge=0)
    verifier_rejection_count: int = Field(default=0, ge=0)
    open_work_item_count: int = Field(default=0, ge=0)
    active_work_item_count: int = Field(default=0, ge=0)
    peak_concurrency: int = Field(default=0, ge=0)


class BudgetRequest(BaseModel):
    """A durable, reviewable request to expand a hard organization budget."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: uuid.UUID
    organization_id: uuid.UUID
    requested_budget: OrganizationBudget
    reason: str = Field(min_length=1)
    status: BudgetRequestStatus = BudgetRequestStatus.PENDING
    decision_reason: str | None = None
    requested_at: datetime = Field(default_factory=_utcnow)
    reviewed_at: datetime | None = None


class HandoffContract(BaseModel):
    """Typed data contract exported by one node or required by another."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    schema_name: str = Field(min_length=1, max_length=200)
    version: str = Field(default="1", min_length=1, max_length=40)
    required: bool = True
    evidence_required: bool = True


class Handoff(BaseModel):
    """Durable producer-to-consumer acceptance record."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: uuid.UUID
    producer_node_id: uuid.UUID
    consumer_node_id: uuid.UUID
    contract: HandoffContract
    artifact_id: uuid.UUID | None = None
    status: HandoffStatus = HandoffStatus.EXPECTED
    evidence_refs: list[str] = Field(default_factory=list)
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class WorkItem(BaseModel):
    """A ticket that can be delegated independently of the whole graph."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    project_id: uuid.UUID
    organization_id: uuid.UUID
    node_id: uuid.UUID | None = None
    key: str = Field(default="", max_length=120)
    agent_type: str = "executor"
    organization_contract: OrganizationContract | None = None
    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    status: WorkItemStatus = WorkItemStatus.BACKLOG
    depends_on: list[uuid.UUID] = Field(default_factory=list)
    artifact_refs: list[uuid.UUID] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    claimed_by: uuid.UUID | None = None
    rejection_reason: str | None = None
    budget_request_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("acceptance_criteria", mode="before")
    @classmethod
    def normalize_acceptance_criteria(cls, value):
        if value is None:
            return []
        return [
            item
            if isinstance(item, (dict, AcceptanceCriterion))
            else {"id": f"criterion-{index + 1}", "description": str(item)}
            for index, item in enumerate(value)
        ]

    @model_validator(mode="after")
    def validate_dependencies(self) -> "WorkItem":
        if self.id in self.depends_on:
            raise ValueError("work item cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("work item dependencies must be unique")
        return self


WorkItemSpec.model_rebuild()
WorkItem.model_rebuild()
OrganizationReview.model_rebuild()
BudgetRequest.model_rebuild()
