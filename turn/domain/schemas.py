"""Strict schemas for the four persistent primitives and the two operations.

Primitives
----------
* Node      — a unit of intent (objective, parent, executor, status, inputs, resources, artifacts, lineage)
* Edge      — CONTAINS (decomposition) or DEPENDS_ON (rare left-to-right stages/joins)
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
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> uuid.UUID:
    return uuid.uuid4()

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


class NodeUIState(str, Enum):
    """Server-projected state presented by the web client."""

    QUEUED = "queued"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    WAITING_DEPENDENCY = "waiting_dependency"
    REVIEW = "review"
    ACCEPTED = "accepted"
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
    ACCEPT = "accept"
    REJECT = "reject"
    PROVIDE_INPUT = "provide_input"


class EdgeType(str, Enum):
    """The only two relationships in a WorkGraph."""

    CONTAINS = "CONTAINS"      # decomposition / visual hierarchy / inherited context
    DEPENDS_ON = "DEPENDS_ON"  # genuine left-to-right stage / integration join


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


class AgentType(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"


def skill_paths_for_agent_type(agent_type: AgentType | str) -> list[str]:
    """Return the filesystem skills required by a built-in agent type."""
    key = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)
    root = Path(__file__).resolve().parent.parent / "agents" / "skills"
    paths = {
        AgentType.PLANNER.value: [root / "planner" / "turn-planning.md"],
        AgentType.EXECUTOR.value: [root / "executor" / "turn-executing.md"],
    }
    return [str(path) for path in paths.get(key, [])]


class Agent(BaseModel):
    """Top-level domain object describing one executable agent.

    ``type_id`` names the agent specialization. Planner and executor are the
    built-in types; each receives its filesystem skill contract automatically.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    type_id: AgentType = AgentType.EXECUTOR
    harness: HarnessKind = HarnessKind.CODEX
    model: Optional[str] = None
    reasoning: ReasoningLevel = ReasoningLevel.DEFAULT
    permission: PermissionMode = PermissionMode.WORKSPACE
    skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None

    @model_validator(mode="after")
    def attach_type_skills(self) -> "Agent":
        required = skill_paths_for_agent_type(self.type_id)
        object.__setattr__(self, "skills", list(dict.fromkeys([*required, *self.skills])))
        return self

    def as_type(self, agent_type: AgentType | str) -> "Agent":
        """Return this agent as a new specialization with exact skills."""
        target = AgentType(agent_type)
        built_in = {
            path
            for kind in AgentType
            for path in skill_paths_for_agent_type(kind)
        }
        custom_skills = [skill for skill in self.skills if skill not in built_in]
        agent_model = Planner if target is AgentType.PLANNER else Executor
        return agent_model.model_validate(
            {
                **self.model_dump(mode="python"),
                "type_id": target,
                "skills": [*skill_paths_for_agent_type(target), *custom_skills],
            }
        )


class AgentConfig(Agent):
    """Request boundary for creating or updating an :class:`Agent`."""


class Planner(Agent):
    """Specialized agent type carrying the turn-planning skill."""

    type_id: AgentType = AgentType.PLANNER


class Executor(Agent):
    """Specialized agent type carrying the turn-executing skill."""

    type_id: AgentType = AgentType.EXECUTOR


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
    review_mode: ReviewMode = ReviewMode.MANUAL

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
    # The absolute path assigned to every worker in THIS project (the directory
    # the user chose or Turn created). Non-root nodes leave this null and
    # inherit it from the root.
    repo_path: Optional[str] = None

    executor: Optional[str] = None  # worker name (e.g. "codex", "planner")
    agent: Optional[Agent] = None
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

    # --- review metadata (set by the runner, surfaced to the UI) ----------
    # Retained as graph metadata for existing state files. Files are already
    # written to the assigned project directory; no merge or cleanup is done.
    needs_review: bool = False
    merge_accepted: bool = False

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


class Graph(BaseModel):
    """The extensible workgraph aggregate owned by the server."""

    project_id: uuid.UUID
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


class GraphNodeView(Node):
    """A node enriched with the server-owned UI projection."""

    ui_state: NodeUIState
    allowed_actions: list[NodeAction]
    state_reason: Optional[str] = None
    generation_active: bool = False


class GraphView(BaseModel):
    """Serialized graph returned to the web client."""

    project_id: uuid.UUID
    nodes: list[GraphNodeView] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


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
    agent: Optional[Agent] = None
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)

    # placement within the generated graph
    parent_key: Optional[str] = None          # CONTAINS parent (another key)
    depends_on: list[str] = Field(default_factory=list)  # prior left-to-right stage keys
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
