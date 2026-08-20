"""Strict schemas for the four persistent primitives and the two operations.

Primitives
----------
* Node      — a unit of intent (objective, parent, executor, status, inputs, resources, artifacts, lineage)
* Edge      — CONTAINS (composition ownership) or FOLLOWS (sequence/fan-out/fan-in)
* Run       — one execution attempt for one node
* Artifact  — any persistent input/output

Operations
----------
* Plan        -> PlanResult   (schema-valid nodes + edges)
* Execute     -> WorkerResult (exactly one of COMPLETE / EXPAND / BLOCK / FAIL)

A WorkGraph is versioned Nodes + Edges. A project is a root node and everything
descended from it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from turn.domain.capability_contracts import (
    BUILTIN_CAPABILITY_IDS,
    capability_ids_for_agent_type,
    validate_capability_id,
)
from turn.metrics import BehaviorExpectations
from turn.domain.organization import (
    AcceptanceCriterion,
    AcceptanceEvidence,
    BudgetRequest,
    Handoff,
    HandoffContract,
    ManagerDecision,
    ManagerPhase,
    OrganizationContract,
    OrganizationReview,
    WorkItem,
    WorkItemSpec,
    WorkspaceRef,
    WorkspaceIsolation,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


NODE_OBJECTIVE_MAX_LENGTH = 72


def concise_node_title(value: str, limit: int = NODE_OBJECTIVE_MAX_LENGTH) -> str:
    """Turn a prompt-shaped label into readable graph navigation copy."""
    clean = " ".join(value.split())
    if len(clean) <= limit:
        return clean
    shortened = clean[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}…"

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class NodeStatus(str, Enum):
    """Lifecycle state of a node, maintained by the runner."""

    PENDING = "PENDING"      # created, not yet evaluated
    BLOCKED = "BLOCKED"      # incomplete sequence or required input
    RUNNABLE = "RUNNABLE"    # ready to execute now
    RUNNING = "RUNNING"      # a Run is in flight
    EXPANDED = "EXPANDED"    # a container; progress derived from descendants
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeUIState(str, Enum):
    """Server-projected state presented by the web client."""

    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    PREPARING = "preparing"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    CORRECTION_REQUIRED = "correction_required"
    WAITING_SEQUENCE = "waiting_sequence"
    COMPLETE = "complete"
    CONTAINER = "container"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeAction(str, Enum):
    """Action authorized by the server for a projected node."""

    RUN = "run"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RETRY = "retry"
    EDIT = "edit"
    REGENERATE = "regenerate"
    PROVIDE_INPUT = "provide_input"


class EdgeType(str, Enum):
    """The two relationships in a structured workflow graph."""

    CONTAINS = "CONTAINS"  # composition ownership / fan-out anchor
    FOLLOWS = "FOLLOWS"    # sequence, fan-out, or fan-in within one boundary


class FlowEdgeType(str, Enum):
    """Transient workflow direction shown in addition to persistent edges."""

    RETURN = "RETURN"


class Outcome(str, Enum):
    """The four-outcome execution contract. Every worker returns exactly one."""

    COMPLETE = "COMPLETE"  # produced output artifacts
    EXPAND = "EXPAND"      # returned child nodes + edges (decomposition)
    BLOCK = "BLOCK"        # returned explicit missing requirements
    FAIL = "FAIL"          # returned an error + whether retry is appropriate


class ArtifactKind(str, Enum):
    TEXT = "text"
    JSON = "json"
    FILE = "file"
    LINK = "link"
    CREDENTIAL_REF = "credential_ref"
    CODE_DIFF = "code_diff"
    LOG = "log"
    EVIDENCE = "evidence"
    USER_INPUT = "user_input"


class InputKind(str, Enum):
    TEXT = "text"
    FILE = "file"
    DECISION = "decision"
    CREDENTIAL = "credential"
    ACCOUNT = "account"
    APPROVAL = "approval"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class HarnessKind(str, Enum):
    """Supported coding-agent command harnesses.

    The graph only stores this stable adapter key; harness-specific command
    construction stays in ``turn.workers``.
    """

    CODEX = "codex"
    CLAUDE = "claude"
    OPENCODE = "opencode"
    PI = "pi"
    MOCK = "mock"
    SHELL = "shell"


class ReasoningLevel(str, Enum):
    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class AgentType(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    INTEGRATOR = "integrator"
    VERIFIER = "verifier"


class VerificationDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class TriggerKind(str, Enum):
    """How a trigger is represented in the graph and activated."""

    EVENT = "event"
    SCHEDULE = "schedule"


class EventSource(str, Enum):
    """Origin of an event delivered to the workspace trigger dispatcher."""

    TRANSITION = "transition"
    AGENT_ACTION = "agent_action"
    SCHEDULE = "schedule"
    CLI = "cli"


class TriggerContext(BaseModel):
    """The exact event envelope that activated a node."""

    model_config = ConfigDict(extra="forbid")

    trigger_id: uuid.UUID
    event_id: uuid.UUID
    event_name: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)
    source: EventSource
    source_project_id: Optional[uuid.UUID] = None
    source_node_id: Optional[uuid.UUID] = None
    occurred_at: datetime = Field(default_factory=_utcnow)


class Trigger(BaseModel):
    """An event or schedule subscription that activates one graph node."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: uuid.UUID = Field(default_factory=_new_id)
    project_id: uuid.UUID
    target_node_id: uuid.UUID
    event_name: Optional[str] = Field(default=None, max_length=200)
    kind: TriggerKind = TriggerKind.EVENT
    schedule: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_fired_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def validate_schedule(self) -> "Trigger":
        if self.kind is TriggerKind.EVENT and not self.event_name:
            raise ValueError("event triggers require an event name")
        if self.kind is TriggerKind.SCHEDULE:
            if not self.schedule:
                raise ValueError("schedule triggers require a schedule expression")
            if len(self.schedule.split()) != 5:
                raise ValueError("schedule triggers require a five-field cron expression")
            if self.event_name is not None:
                raise ValueError("schedule triggers cannot define an event name")
        if self.kind is TriggerKind.EVENT and self.schedule is not None:
            raise ValueError("event triggers cannot define a schedule expression")
        return self


class VerificationResult(BaseModel):
    """Evidence-backed review decision emitted by a node through the CLI.

    ``target_node_id`` is optional for compatibility with the original QA
    contract. When omitted, the runner returns a rejection to the reviewer's
    only preceding workflow item when there is one. A reviewer may name any other node in the same
    workgraph explicitly when that is the node that needs correction.
    """

    decision: VerificationDecision
    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    evidence: list[AcceptanceEvidence] = Field(default_factory=list)
    target_node_id: Optional[uuid.UUID] = None


class Agent(BaseModel):
    """Top-level domain object describing one executable agent.

    ``type_id`` names the agent specialization. Role defaults are expressed as
    capability plugin ids and are resolved by the capability catalog at launch.
    """

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: uuid.UUID = Field(default_factory=_new_id)
    type_id: AgentType = AgentType.EXECUTOR
    harness: HarnessKind = HarnessKind.CODEX
    model: Optional[str] = None
    reasoning: ReasoningLevel = ReasoningLevel.DEFAULT
    tools: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None

    @model_validator(mode="after")
    def attach_type_capabilities(self) -> "Agent":
        required = capability_ids_for_agent_type(self.type_id)
        references = list(dict.fromkeys([
            *required,
            *self.capabilities,
        ]))
        for capability_id in references:
            validate_capability_id(capability_id)
        object.__setattr__(self, "capabilities", references)
        return self

    def as_type(self, agent_type: AgentType | str) -> "Agent":
        """Return this agent as a new specialization with exact capabilities."""
        target = AgentType(agent_type)
        custom_capabilities = [
            capability_id
            for capability_id in self.capabilities
            if capability_id not in BUILTIN_CAPABILITY_IDS
        ]
        agent_model = {
            AgentType.PLANNER: Planner,
            AgentType.INTEGRATOR: Integrator,
            AgentType.VERIFIER: Verifier,
            AgentType.EXECUTOR: Executor,
        }[target]
        return agent_model.model_validate(
            {
                **self.model_dump(mode="python"),
                "type_id": target,
                "capabilities": [*capability_ids_for_agent_type(target), *custom_capabilities],
            }
        )


class AgentConfig(Agent):
    """Request boundary for creating or updating an :class:`Agent`."""


class CapabilityStatus(BaseModel):
    """Project/harness deployment state for one graph-assigned capability."""

    capability_id: str
    skills: int = Field(ge=0)
    mcps: int = Field(ge=0)
    loaded: bool
    installed: bool


class Planner(Agent):
    """Specialized agent type carrying the turn-planning capability."""

    type_id: AgentType = AgentType.PLANNER


class Executor(Agent):
    """Specialized agent type carrying the turn-executing capability."""

    type_id: AgentType = AgentType.EXECUTOR


class Integrator(Agent):
    """Specialized agent type carrying the turn-integrating capability."""

    type_id: AgentType = AgentType.INTEGRATOR


class Verifier(Agent):
    """Specialized agent that approves or rejects work in the workgraph."""

    type_id: AgentType = AgentType.VERIFIER


class RunPolicy(BaseModel):
    """Project execution and recovery policy."""

    auto_run: bool = True
    delay_between_jobs_ms: int = Field(default=0, ge=0, le=600_000)
    timeout_seconds: float = Field(default=600, gt=0, le=86_400)
    # Inter-output watchdog. Unlike the whole-run timeout this detects a
    # provider process that is still alive but has stopped producing bytes.
    stall_timeout_seconds: float = Field(default=90, gt=0, le=3_600)
    max_retries: int = Field(default=1, ge=0, le=20)
    retry_backoff_ms: int = Field(default=750, ge=0, le=600_000)
    retry_choked_models: bool = True
    compact_on_context_pressure: bool = True
    behavior_expectations: BehaviorExpectations | None = None
    # Capacity is explicit because recursive organizations must not assume
    # provider processes or tokens are infinite.
    max_parallel_agents: int = Field(default=4, ge=1, le=10_000)
    max_total_runs: int | None = Field(default=None, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)
    max_wall_time_seconds: float | None = Field(default=None, gt=0)
    workspace_isolation: WorkspaceIsolation = WorkspaceIsolation.WORKTREE

class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Optional[float] = Field(default=None, ge=0)


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------


class InputSpec(BaseModel):
    """One explicitly-requested input for a node."""

    model_config = ConfigDict(extra="forbid")

    id: str
    label: str
    kind: InputKind = InputKind.TEXT
    description: Optional[str] = None
    # id of the Artifact that satisfied this input (set when supplied)
    satisfied_by: Optional[uuid.UUID] = None


class DocumentRef(BaseModel):
    """A dynamic project-relative or web document reference.

    References deliberately identify files without embedding their contents in
    graph state.  This keeps the graph composable and lets the file remain an
    independently editable source of truth.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    title: Optional[str] = None
    media_type: Optional[str] = None
    imports: list["DocumentRef"] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_reference(self) -> "DocumentRef":
        parsed = urlsplit(self.ref)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("document references only support relative paths or http(s) URLs")
        if parsed.scheme:
            if not parsed.netloc:
                raise ValueError("http(s) document references must include a host")
            return self
        if parsed.netloc:
            raise ValueError("document references cannot use network-path URLs")
        if not parsed.scheme:
            path = parsed.path.replace("\\", "/")
            if path.startswith("/") or Path(path).is_absolute() or PureWindowsPath(path).is_absolute():
                raise ValueError("document references must be project-relative")
            if any(part == ".." for part in path.split("/")):
                raise ValueError("document references cannot escape the project")
            if not path or path == ".":
                raise ValueError("document references must identify a file")
        return self


class SubgraphRef(BaseModel):
    """A project-relative JSON source for a composed graph boundary.

    The server stores this identity on the composition anchor. It does not
    read the referenced file while projecting graph state; importing a source
    is an explicit planner/CLI operation.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    title: Optional[str] = None
    media_type: Optional[str] = "application/json"
    # Turn-generated handoff sources are still visible/editable links, but
    # replacing the same planner boundary does not require --force merely to
    # rotate that managed source. User-authored composition links remain
    # guarded by the replacement policy.
    managed: bool = False

    @model_validator(mode="after")
    def validate_reference(self) -> "SubgraphRef":
        parsed = urlsplit(self.ref)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError("subgraph references only support relative paths or http(s) URLs")
        if parsed.scheme:
            if not parsed.netloc:
                raise ValueError("http(s) subgraph references must include a host")
            return self
        if parsed.netloc:
            raise ValueError("subgraph references cannot use network-path URLs")
        path = parsed.path.replace("\\", "/")
        if path.startswith("/") or Path(path).is_absolute() or PureWindowsPath(path).is_absolute():
            raise ValueError("subgraph references must be project-relative")
        if any(part == ".." for part in path.split("/")):
            raise ValueError("subgraph references cannot escape the project")
        if not path or path == ".":
            raise ValueError("subgraph references must identify a JSON file")
        if Path(path).suffix.lower() != ".json":
            raise ValueError("subgraph references must point to .json files")
        return self


def flatten_document_refs(refs: list[DocumentRef]) -> list[DocumentRef]:
    """Return a stable depth-first view of nested document imports."""
    flattened: list[DocumentRef] = []
    for ref in refs:
        flattened.append(ref)
        flattened.extend(flatten_document_refs(ref.imports))
    return flattened


class Node(BaseModel):
    """A unit of intent. Persisted; workers are temporary."""

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    project_id: uuid.UUID  # root ancestor id
    parent_id: Optional[uuid.UUID] = None  # CONTAINS parent

    # The graph label is intentionally compact. Detailed instructions belong
    # in generated_prompt, documents, or artifacts rather than on the card.
    objective: str = Field(min_length=1, max_length=NODE_OBJECTIVE_MAX_LENGTH)
    project_name: Optional[str] = None  # concise root-only navigation identity
    generated_prompt: Optional[str] = None  # prompt handed to the worker
    # --- repo (per-project working directory) ---------------------------
    # The absolute path assigned to every worker in THIS project (the directory
    # the user chose or Turn created). Non-root nodes leave this null and
    # inherit it from the root.
    repo_path: Optional[str] = None
    # A node may receive a private workspace when the project opts into
    # worktree isolation. ``repo_path`` remains the project root; this field
    # is the actual cwd for this node's provider turn.
    workspace_path: Optional[str] = None
    workspace_commit: Optional[str] = None
    workspace: WorkspaceRef | None = None
    output_branch: str | None = None

    executor: Optional[str] = None  # worker name (e.g. "codex", "planner")
    agent: Optional[Agent] = None
    verification: Optional[VerificationResult] = None
    trigger_context: Optional[TriggerContext] = None
    status: NodeStatus = NodeStatus.PENDING
    paused: bool = False
    # Project-level execution mode: True = auto-run ready nodes (default);
    # False = manual "step" mode where the runner plans but waits for an
    # explicit step()/run_node() before executing. Effective only on the
    # project root node (id == project_id).
    auto_run: bool = True
    # Project-only policy. Descendants inherit it from the root at runtime.
    run_policy: Optional[RunPolicy] = None

    # Planner boundaries own a charter and a durable manager-loop observation.
    # Old state files may omit both fields; the runtime treats that as a
    # focused, one-shot boundary until the next explicit planner handoff.
    organization_contract: OrganizationContract | None = None
    organization_review: OrganizationReview | None = None
    manager_phase: ManagerPhase | None = None
    manager_iteration: int = Field(default=0, ge=0)
    manager_review_reasons: list[str] = Field(default_factory=list)
    work_item_id: uuid.UUID | None = None
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    exported_handoffs: list[HandoffContract] = Field(default_factory=list)
    required_handoffs: list[HandoffContract] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-100_000, le=100_000)

    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    subgraph_refs: list[SubgraphRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subgraph_refs", "graph_refs", "graph_files", "graph_ref", "graph_file"),
    )
    artifact_refs: list[uuid.UUID] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # --- derived, never persisted -------------------------------------
    progress: Optional[float] = None  # 0..1 for containers, set by graph logic

    # Live agent protocol status. The agent publishes these through
    # `turn agent status`; the runner mirrors them onto the graph so every
    # surface can present the same working message without parsing terminal
    # output.
    agent_state: Optional[str] = None
    agent_message: Optional[str] = None


# --------------------------------------------------------------------------
# Edge
# --------------------------------------------------------------------------


class Edge(BaseModel):
    """A relationship between two nodes in the structured workflow."""

    id: uuid.UUID = Field(default_factory=_new_id)
    # For CONTAINS: src is the composition anchor, dst is an owned node.
    # For FOLLOWS: src is an earlier workflow item, dst is the next item.
    src: uuid.UUID
    dst: uuid.UUID
    type: EdgeType
    created_at: datetime = Field(default_factory=_utcnow)


class FlowEdge(BaseModel):
    """A derived, non-persistent edge describing the current flow direction."""

    id: uuid.UUID
    src: uuid.UUID
    dst: uuid.UUID
    type: FlowEdgeType


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------


class Run(BaseModel):
    """One execution attempt for one node."""

    id: uuid.UUID = Field(default_factory=_new_id)
    node_id: uuid.UUID
    worker: str

    started_at: datetime = Field(default_factory=_utcnow)
    ended_at: Optional[datetime] = None

    status: RunStatus = RunStatus.RUNNING
    outcome: Optional[Outcome] = None
    summary: Optional[str] = None
    logs: str = ""
    error: Optional[str] = None
    retry_recommended: bool = False

    attempt: int = 1
    usage: Usage = Field(default_factory=Usage)
    session_id: Optional[str] = None


# --------------------------------------------------------------------------
# Artifact
# --------------------------------------------------------------------------


class Artifact(BaseModel):
    """Any persistent input or output."""

    id: uuid.UUID = Field(default_factory=_new_id)
    node_id: Optional[uuid.UUID] = None  # producer node
    kind: ArtifactKind = ArtifactKind.TEXT
    name: str
    content: Optional[Any] = None  # text/json/structured data
    ref: Optional[str] = None       # file path / external id / url
    schema_name: Optional[str] = None
    schema_version: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)


class ArtifactRef(BaseModel):
    id: uuid.UUID
    node_id: Optional[uuid.UUID] = None
    kind: ArtifactKind
    name: str
    ref: Optional[str] = None


class Graph(BaseModel):
    """The extensible workgraph aggregate owned by the server."""

    project_id: uuid.UUID
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    work_items: list[WorkItem] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    budget_requests: list[BudgetRequest] = Field(default_factory=list)


class ControlActivity(BaseModel):
    """A view-only projection of an active control-plane Run."""

    kind: Literal["plan_audit", "manager_review"]
    status: Literal["running"] = "running"
    started_at: datetime
    attempt: int = 1


class GraphNodeView(Node):
    """A node enriched with the server-owned UI projection."""

    ui_state: NodeUIState
    allowed_actions: list[NodeAction]
    state_reason: Optional[str] = None
    generation_active: bool = False
    capability_status: list[CapabilityStatus] = Field(default_factory=list)
    control_activity: Optional[ControlActivity] = None


class GraphView(BaseModel):
    """Serialized graph returned to the web client."""

    project_id: uuid.UUID
    nodes: list[GraphNodeView] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    flow_edges: list[FlowEdge] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    triggers: list[Trigger] = Field(default_factory=list)
    work_items: list[WorkItem] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    budget_requests: list[BudgetRequest] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Resources (context, not orchestration)
# --------------------------------------------------------------------------


class Resource(BaseModel):
    """A piece of context attached to a node (instruction or document)."""

    ref: str
    content: Optional[str] = None


# --------------------------------------------------------------------------
# Plan operation result
# --------------------------------------------------------------------------


class NodeSpec(BaseModel):
    """A node to be created by a planner. Referenced by `key` within a plan."""

    model_config = ConfigDict(extra="forbid")

    key: str
    # Keep graph labels readable; put execution detail in generated_prompt.
    objective: str = Field(min_length=1, max_length=NODE_OBJECTIVE_MAX_LENGTH)
    generated_prompt: Optional[str] = None
    executor: Optional[str] = None
    agent: Optional[Agent] = None
    # A plan can request a built-in agent specialization while inheriting the
    # parent's harness/model configuration. An explicit ``agent`` remains the
    # escape hatch for a fully configured agent.
    agent_type: Optional[AgentType] = Field(
        default=None,
        description=(
            "Turn role for this node. A planner role is a planning boundary, not "
            "a leaf worker: Turn normalizes agent_type=planner to plan=true and "
            "runs the planner operation for that node."
        ),
    )
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    organization_contract: OrganizationContract | None = None
    exported_handoffs: list[HandoffContract] = Field(default_factory=list)
    required_handoffs: list[HandoffContract] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    priority: int = Field(default=0, ge=-100_000, le=100_000)
    subgraph_refs: list[SubgraphRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subgraph_refs", "graph_refs", "graph_files", "graph_ref", "graph_file"),
    )
    artifacts: list["ArtifactSpec"] = Field(default_factory=list)
    # Capability plugin ids loaded by the planner into the project.
    capabilities: list[str] = Field(default_factory=list)

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

    # placement within the generated graph
    parent_key: Optional[str] = None          # CONTAINS parent (another key)
    # Sequence is deliberately independent from ownership. Multiple nodes may
    # follow one node (fan-out), and one node may follow multiple nodes
    # (fan-in), but sequence never crosses a composition boundary.
    follows: list[str] = Field(default_factory=list)  # prior sequence keys
    # A planner node is a recursive organization boundary. ``agent_type`` is
    # the semantic source of truth; ``plan`` remains as a concise wire-level
    # declaration and for compatibility with existing graph files.
    plan: bool = Field(
        default=False,
        description=(
            "Create a recursive planning boundary. plan=true, executor=planner, "
            "or agent_type=planner are equivalent planner declarations."
        ),
    )

    @model_validator(mode="after")
    def normalize_planning_boundary(self) -> "NodeSpec":
        """Make planner role and planner execution semantics impossible to split.

        Historically a plan could declare ``agent_type=planner`` while leaving
        ``plan`` false. The created node then *looked* like a planner and carried
        planner capabilities, but the runner executed it as an ordinary worker
        because persistence uses the special ``executor=planner`` operation
        sentinel. That is especially destructive for recursive organizations:
        a department planner silently becomes one generalist leaf.

        Normalize every planner-shaped declaration at the contract boundary so
        the role shown in the graph always matches the operation the runner will
        execute. Contradictory explicit roles fail instead of producing a hybrid
        node whose behavior depends on mutation-order details.
        """
        explicit_agent_type = self.agent.type_id if self.agent is not None else None
        planning_requested = (
            self.plan
            or self.executor == "planner"
            or self.agent_type is AgentType.PLANNER
            or explicit_agent_type is AgentType.PLANNER
        )
        if not planning_requested:
            return self
        if self.agent_type is not None and self.agent_type is not AgentType.PLANNER:
            raise ValueError("planning boundaries must use agent_type=planner")
        if explicit_agent_type is not None and explicit_agent_type is not AgentType.PLANNER:
            raise ValueError("planning boundaries must use a planner Agent")
        self.plan = True
        if self.agent is None:
            self.agent_type = AgentType.PLANNER
        return self


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EdgeType
    src: str  # key
    dst: str  # key


class TriggerSpec(BaseModel):
    """A trigger declared by a planner for a node in the same plan."""

    model_config = ConfigDict(extra="forbid")

    target_key: str
    event_name: Optional[str] = Field(default=None, max_length=200)
    kind: TriggerKind = TriggerKind.EVENT
    schedule: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_schedule(self) -> "TriggerSpec":
        if self.kind is TriggerKind.EVENT and not self.event_name:
            raise ValueError("event triggers require an event name")
        if self.kind is TriggerKind.SCHEDULE:
            if not self.schedule:
                raise ValueError("schedule triggers require a schedule expression")
            if len(self.schedule.split()) != 5:
                raise ValueError("schedule triggers require a five-field cron expression")
            if self.event_name is not None:
                raise ValueError("schedule triggers cannot define an event name")
        if self.kind is TriggerKind.EVENT and self.schedule is not None:
            raise ValueError("event triggers cannot define a schedule expression")
        return self


class PlanResult(BaseModel):
    """The output of the Plan operation.

    ``nodes`` may be empty. That represents a valid no-op or document-only
    planning handoff, which preserves any existing child composition.
    """

    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeSpec]
    # Only the project-root setup planner may use this. The server ingests it
    # as the navigation name when the user did not provide one at creation.
    project_name: Optional[str] = Field(default=None, min_length=1, max_length=72)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    subgraph_refs: list[SubgraphRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subgraph_refs", "graph_refs", "graph_files", "graph_ref", "graph_file"),
    )
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    triggers: list[TriggerSpec] = Field(default_factory=list)
    notes: Optional[str] = None
    organization_contract: OrganizationContract | None = None
    usage: Usage = Field(default_factory=Usage)
    session_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_references_and_cycles(self) -> "PlanResult":
        """Reject malformed graphs at the operation boundary.

        Persistence deliberately does not guess at planner intent: duplicate
        objectives are valid, while duplicate keys, missing references, and
        cycles are structural errors that must be corrected by the planner.
        """
        keys = [node.key for node in self.nodes]
        known = set(keys)
        if len(keys) != len(known):
            raise ValueError("plan node keys must be unique")
        for trigger in self.triggers:
            if trigger.target_key not in known:
                raise ValueError(f"unknown trigger target key: {trigger.target_key}")

        containment: dict[str, set[str]] = {key: set() for key in keys}
        sequence: dict[str, set[str]] = {key: set() for key in keys}
        adjacency: dict[str, set[str]] = {key: set() for key in keys}
        boundaries = {node.key: node.parent_key for node in self.nodes}
        sequence_pairs: set[tuple[str, str]] = set()
        for node in self.nodes:
            for capability_id in node.capabilities:
                validate_capability_id(capability_id)
            if node.parent_key:
                if node.parent_key not in known:
                    raise ValueError(f"unknown parent key: {node.parent_key}")
                if node.parent_key == node.key:
                    raise ValueError(f"node {node.key} cannot contain itself")
                containment[node.parent_key].add(node.key)
                adjacency[node.parent_key].add(node.key)
            for predecessor in node.follows:
                if predecessor not in known:
                    raise ValueError(f"unknown sequence key: {predecessor}")
                if predecessor == node.key:
                    raise ValueError(f"node {node.key} cannot follow itself")
                if boundaries[predecessor] != boundaries[node.key]:
                    raise ValueError(
                        f"sequence edge {predecessor}->{node.key} crosses a composition boundary"
                    )
                sequence[predecessor].add(node.key)
                adjacency[predecessor].add(node.key)
                sequence_pairs.add((predecessor, node.key))

        for edge in self.edges:
            if edge.src not in known or edge.dst not in known:
                missing = edge.src if edge.src not in known else edge.dst
                raise ValueError(f"unknown edge key: {missing}")
            if edge.src == edge.dst:
                raise ValueError(f"node {edge.src} cannot link to itself")
            if edge.type is EdgeType.FOLLOWS:
                if boundaries[edge.src] != boundaries[edge.dst]:
                    raise ValueError(
                        f"sequence edge {edge.src}->{edge.dst} crosses a composition boundary"
                    )
                sequence[edge.src].add(edge.dst)
                sequence_pairs.add((edge.src, edge.dst))
            adjacency[edge.src].add(edge.dst)

        def cycle_path(graph: dict[str, set[str]]) -> list[str] | None:
            visiting: list[str] = []
            active = set()
            visited: set[str] = set()

            def visit(key: str) -> list[str] | None:
                if key in active:
                    return [*visiting[visiting.index(key):], key]
                if key in visited:
                    return None
                active.add(key)
                visiting.append(key)
                for child in sorted(graph[key]):
                    found = visit(child)
                    if found:
                        return found
                visiting.pop()
                active.remove(key)
                visited.add(key)
                return None

            for key in keys:
                found = visit(key)
                if found:
                    return found
            return None

        containment_cycle = cycle_path(containment)
        if containment_cycle:
            raise ValueError(
                "containment cycle: " + " -> ".join(containment_cycle)
            )
        sequence_cycle = cycle_path(sequence)
        if sequence_cycle:
            raise ValueError(
                "sequence cycle: " + " -> ".join(sequence_cycle)
            )

        def has_alternative_sequence_path(source: str, target: str) -> bool:
            pending = [child for child in sequence[source] if (source, child) != (source, target)]
            visited: set[str] = set()
            while pending:
                current = pending.pop()
                if current == target:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(sequence[current])
            return False

        for source, target in sorted(sequence_pairs):
            if has_alternative_sequence_path(source, target):
                raise ValueError(
                    f"sequence edge {source}->{target} is a transitive shortcut; "
                    "connect the adjacent workflow stages instead"
                )
        combined_cycle = cycle_path(adjacency)
        if combined_cycle:
            raise ValueError(
                "graph cycle across composition/sequence edges: "
                + " -> ".join(combined_cycle)
            )
        return self


class ManagerResult(BaseModel):
    """Small retained-session result for a planner's management review."""

    model_config = ConfigDict(extra="forbid")

    decision: ManagerDecision
    summary: str = Field(min_length=1)
    plan: PlanResult | None = None
    work_items: list[WorkItemSpec] = Field(default_factory=list)
    missing_inputs: list[InputSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Execute operation result
# --------------------------------------------------------------------------


class ArtifactSpec(BaseModel):
    """An artifact a worker wants persisted."""

    model_config = ConfigDict(extra="forbid")

    kind: ArtifactKind = ArtifactKind.TEXT
    name: str
    content: Optional[Any] = None
    ref: Optional[str] = None
    schema_name: Optional[str] = None
    schema_version: Optional[str] = None
    evidence_refs: list[str] = Field(default_factory=list)


class WorkerResult(BaseModel):
    """The output of the Execute operation: exactly one outcome."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    summary: str = ""
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    evidence: list[AcceptanceEvidence] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    subgraph_refs: list[SubgraphRef] = Field(
        default_factory=list,
        validation_alias=AliasChoices("subgraph_refs", "graph_refs", "graph_files", "graph_ref", "graph_file"),
    )

    # EXPAND
    children: Optional[PlanResult] = None
    # BLOCK
    missing_inputs: list[InputSpec] = Field(default_factory=list)
    # FAIL
    error: Optional[str] = None
    retry_recommended: bool = False

    executor_notes: Optional[str] = None
    usage: Usage = Field(default_factory=Usage)
    session_id: Optional[str] = None
    verification: Optional[VerificationResult] = None
