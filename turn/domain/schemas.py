"""Strict schemas for the four persistent primitives and the two operations.

Primitives
----------
* Node      — a unit of intent (objective, parent, executor, status, inputs, resources, artifacts, lineage)
* Edge      — CONTAINS (decomposition) or DEPENDS_ON (ordering/joins)
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
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class NodeStatus(str, Enum):
    """Lifecycle state of a node, maintained by the runner."""

    PENDING = "PENDING"      # created, not yet evaluated
    BLOCKED = "BLOCKED"      # missing dependency or required input
    RUNNABLE = "RUNNABLE"    # ready to execute now
    RUNNING = "RUNNING"      # a Run is in flight
    EXPANDED = "EXPANDED"    # a container; progress derived from descendants
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EdgeType(str, Enum):
    """The only two relationships in a WorkGraph."""

    CONTAINS = "CONTAINS"      # decomposition / visual hierarchy / inherited context
    DEPENDS_ON = "DEPENDS_ON"  # execution order / parallelism / joins


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
    ECHO = "echo"
    SHELL = "shell"


class ReasoningLevel(str, Enum):
    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class PermissionMode(str, Enum):
    ASK = "ask"
    WORKSPACE = "workspace"
    FULL = "full"


class ReviewMode(str, Enum):
    MANUAL = "manual"
    # Legacy persisted value. It now follows the same parent-verification
    # semantics as PARENT; Turn never silently accepts unverified work.
    AUTO_ACCEPT = "auto_accept"
    PARENT = "parent"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class AgentConfig(BaseModel):
    """Runtime-neutral agent assignment for a node.

    ``type_id`` is deliberately an open string. Today it is usually
    ``planner`` or ``general``; later custom agent types can be registered
    without changing the graph schema.
    """

    type_id: str = "general"
    harness: HarnessKind = HarnessKind.CODEX
    model: Optional[str] = None
    reasoning: ReasoningLevel = ReasoningLevel.DEFAULT
    permission: PermissionMode = PermissionMode.WORKSPACE
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None


class RunPolicy(BaseModel):
    """Project execution and recovery policy."""

    auto_run: bool = True
    force_sequential: bool = False
    delay_between_jobs_ms: int = Field(default=0, ge=0, le=600_000)
    timeout_seconds: float = Field(default=600, gt=0, le=86_400)
    # Inter-output watchdog. Unlike the whole-run timeout this detects a
    # provider process that is still alive but has stopped producing bytes.
    stall_timeout_seconds: float = Field(default=90, gt=0, le=3_600)
    max_retries: int = Field(default=1, ge=0, le=20)
    retry_backoff_ms: int = Field(default=750, ge=0, le=600_000)
    retry_choked_models: bool = True
    compact_on_context_pressure: bool = True
    review_mode: ReviewMode = ReviewMode.MANUAL


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Optional[float] = Field(default=None, ge=0)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()


# --------------------------------------------------------------------------
# Node
# --------------------------------------------------------------------------


class InputSpec(BaseModel):
    """One explicitly-requested input for a node."""

    id: str
    label: str
    kind: InputKind = InputKind.TEXT
    description: Optional[str] = None
    # id of the Artifact that satisfied this input (set when supplied)
    satisfied_by: Optional[uuid.UUID] = None


class Node(BaseModel):
    """A unit of intent. Persisted; workers are temporary."""

    id: uuid.UUID = Field(default_factory=_new_id)
    project_id: uuid.UUID  # root ancestor id
    parent_id: Optional[uuid.UUID] = None  # CONTAINS parent

    objective: str
    project_name: Optional[str] = None  # concise root-only navigation identity
    generated_prompt: Optional[str] = None  # prompt handed to the worker

    # --- repo (per-project working directory) ---------------------------
    # The absolute path of THIS project's own git repository (the directory
    # the user chose / Turn created). The root node's worktree IS this path, so
    # by the time work completes the directory holds the finished files plus a
    # merge log. Non-root nodes leave this null and inherit it from the root.
    repo_path: Optional[str] = None

    executor: Optional[str] = None  # worker name (e.g. "codex", "planner")
    agent: Optional[AgentConfig] = None
    status: NodeStatus = NodeStatus.PENDING
    paused: bool = False
    # Project-level execution mode: True = auto-run ready nodes (default);
    # False = manual "step" mode where the runner plans but waits for an
    # explicit step()/run_node() before executing. Effective only on the
    # project root node (id == project_id).
    auto_run: bool = True
    # Project-only policy. Descendants inherit it from the root at runtime.
    run_policy: Optional[RunPolicy] = None

    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[uuid.UUID] = Field(default_factory=list)

    # revision & lineage metadata (edits create new revisions, not rewrites)
    revision: int = 1
    superseded_by: Optional[uuid.UUID] = None
    forked_from: Optional[uuid.UUID] = None

    # --- merge review (set by the runner, surfaced to the UI) ----------
    # A node whose worktree has been merged up into its parent is redundant
    # on disk. The runner asks the user to review the merged result:
    #   needs_review   -> merged up, awaiting accept (clean subtree) / reject
    #   merge_accepted -> reviewed & accepted; subtree filesystem cleaned
    needs_review: bool = False
    merge_accepted: bool = False
    # Parent-owned automatic review lifecycle. Evidence is retained on the
    # child even after acceptance/rejection so the decision remains auditable.
    verification_status: Optional[VerificationStatus] = None
    verification_summary: Optional[str] = None
    verification_round: int = 0
    # The parent-review conversation is distinct from both the parent's own
    # planning/execution session and the child's correction session.
    verification_session_id: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    # --- derived, never persisted -------------------------------------
    progress: Optional[float] = None  # 0..1 for containers, set by graph logic


# --------------------------------------------------------------------------
# Edge
# --------------------------------------------------------------------------


class Edge(BaseModel):
    """A relationship between two nodes. Only CONTAINS or DEPENDS_ON."""

    id: uuid.UUID = Field(default_factory=_new_id)
    # For CONTAINS: src is the parent, dst is the child.
    # For DEPENDS_ON: src is the prerequisite, dst is the dependent.
    src: uuid.UUID
    dst: uuid.UUID
    type: EdgeType
    created_at: datetime = Field(default_factory=_utcnow)


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

    node_revision: int = 1  # node.revision captured when the Run started
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
    created_at: datetime = Field(default_factory=_utcnow)


class ArtifactRef(BaseModel):
    id: uuid.UUID
    node_id: Optional[uuid.UUID] = None
    kind: ArtifactKind
    name: str
    ref: Optional[str] = None


# --------------------------------------------------------------------------
# Resources / skills (context, not orchestration)
# --------------------------------------------------------------------------


class Resource(BaseModel):
    """A piece of context attached to a node (skill, instruction, doc)."""

    ref: str
    content: Optional[str] = None


# --------------------------------------------------------------------------
# Plan operation result
# --------------------------------------------------------------------------


class NodeSpec(BaseModel):
    """A node to be created by a planner. Referenced by `key` within a plan."""

    key: str
    objective: str
    generated_prompt: Optional[str] = None
    executor: Optional[str] = None
    agent: Optional[AgentConfig] = None
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)

    # placement within the generated graph
    parent_key: Optional[str] = None          # CONTAINS parent (another key)
    depends_on: list[str] = Field(default_factory=list)  # prerequisite keys
    # When True (or executor == "planner") the created node is itself a
    # sub-planner: the runner will decompose it again on its next turn instead
    # of executing it as a leaf. This lets plans nest planners arbitrarily.
    plan: bool = False


class EdgeSpec(BaseModel):
    type: EdgeType
    src: str  # key
    dst: str  # key


class PlanResult(BaseModel):
    """The output of the Plan operation: schema-valid nodes + edges."""

    nodes: list[NodeSpec]
    edges: list[EdgeSpec] = Field(default_factory=list)
    notes: Optional[str] = None
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

        adjacency: dict[str, set[str]] = {key: set() for key in keys}
        for node in self.nodes:
            if node.parent_key:
                if node.parent_key not in known:
                    raise ValueError(f"unknown parent key: {node.parent_key}")
                if node.parent_key == node.key:
                    raise ValueError(f"node {node.key} cannot contain itself")
                adjacency[node.parent_key].add(node.key)
            for dependency in node.depends_on:
                if dependency not in known:
                    raise ValueError(f"unknown dependency key: {dependency}")
                if dependency == node.key:
                    raise ValueError(f"node {node.key} cannot depend on itself")
                adjacency[dependency].add(node.key)

        for edge in self.edges:
            if edge.src not in known or edge.dst not in known:
                missing = edge.src if edge.src not in known else edge.dst
                raise ValueError(f"unknown edge key: {missing}")
            if edge.src == edge.dst:
                raise ValueError(f"node {edge.src} cannot link to itself")
            adjacency[edge.src].add(edge.dst)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(key: str) -> None:
            if key in visiting:
                raise ValueError("plan graph must be acyclic")
            if key in visited:
                return
            visiting.add(key)
            for child in adjacency[key]:
                visit(child)
            visiting.remove(key)
            visited.add(key)

        for key in keys:
            visit(key)
        return self


# --------------------------------------------------------------------------
# Execute operation result
# --------------------------------------------------------------------------


class ArtifactSpec(BaseModel):
    """An artifact a worker wants persisted."""

    kind: ArtifactKind = ArtifactKind.TEXT
    name: str
    content: Optional[Any] = None
    ref: Optional[str] = None


class WorkerResult(BaseModel):
    """The output of the Execute operation: exactly one outcome."""

    outcome: Outcome
    summary: str = ""
    artifacts: list[ArtifactSpec] = Field(default_factory=list)

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
