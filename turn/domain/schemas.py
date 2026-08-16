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
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Optional
from urllib.parse import urlsplit

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
    PREPARING = "preparing"
    PAUSED = "paused"
    WAITING_INPUT = "waiting_input"
    WAITING_DEPENDENCY = "waiting_dependency"
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
    """Graph relationships used for hierarchy and workflow ordering."""

    CONTAINS = "CONTAINS"      # decomposition / visual hierarchy / inherited context
    DEPENDS_ON = "DEPENDS_ON"  # genuine left-to-right stage / integration join


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


class AgentType(str, Enum):
    PLANNER = "planner"
    EXECUTOR = "executor"
    INTEGRATOR = "integrator"
    VERIFIER = "verifier"


class VerificationDecision(str, Enum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class VerificationResult(BaseModel):
    """Evidence-backed decision emitted by a verifier through the CLI."""

    decision: VerificationDecision
    summary: str = Field(min_length=1)
    findings: list[str] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


def skill_paths_for_agent_type(agent_type: AgentType | str) -> list[str]:
    """Return the filesystem skills required by a built-in agent type."""
    key = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)
    root = Path(__file__).resolve().parent.parent / "agents" / "skills"
    paths = {
        AgentType.PLANNER.value: [
            root / "planner" / "turn-planning.md",
            root / "planner" / "imagegen.md",
            root / "planner" / "find-skills.md",
        ],
        AgentType.EXECUTOR.value: [
            root / "executor" / "turn-executing.md",
        ],
        AgentType.INTEGRATOR.value: [
            root / "integrator" / "turn-integrating.md",
        ],
        AgentType.VERIFIER.value: [
            root / "verifier" / "turn-verifying.md",
        ],
    }
    return [str(path) for path in paths.get(key, [])]


def skill_ids_for_agent_type(agent_type: AgentType | str) -> list[str]:
    """Stable library ids assigned to every built-in agent specialization."""
    key = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)
    return {
        AgentType.PLANNER.value: [
            "turn-planning", "imagegen", "find-skills"
        ],
        AgentType.EXECUTOR.value: ["turn-executing"],
        AgentType.INTEGRATOR.value: ["turn-integrating"],
        AgentType.VERIFIER.value: ["turn-verifying"],
    }.get(key, [])


class Agent(BaseModel):
    """Top-level domain object describing one executable agent.

    ``type_id`` names the agent specialization. Planner, executor, and
    integrator are the built-in types; each receives its filesystem skill
    contract automatically.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: uuid.UUID = Field(default_factory=_new_id)
    type_id: AgentType = AgentType.EXECUTOR
    harness: HarnessKind = HarnessKind.CODEX
    model: Optional[str] = None
    reasoning: ReasoningLevel = ReasoningLevel.DEFAULT
    permission: PermissionMode = PermissionMode.WORKSPACE
    # ``skills`` is the materialized filesystem view used by harness adapters.
    # ``skill_ids`` is the stable graph/library contract and is project-scoped
    # into the project's .turn/skills directory before a harness is launched.
    skills: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    session_id: Optional[str] = None

    @model_validator(mode="after")
    def attach_type_skills(self) -> "Agent":
        from turn.skills.library import validate_skill_reference

        required = skill_paths_for_agent_type(self.type_id)
        required_ids = skill_ids_for_agent_type(self.type_id)
        object.__setattr__(self, "skills", list(dict.fromkeys([*required, *self.skills])))
        references = list(dict.fromkeys([*required_ids, *self.skill_ids]))
        for reference in references:
            validate_skill_reference(reference)
        object.__setattr__(self, "skill_ids", references)
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
        built_in_ids = {
            skill_id
            for kind in AgentType
            for skill_id in skill_ids_for_agent_type(kind)
        }
        custom_skill_ids = [skill_id for skill_id in self.skill_ids if skill_id not in built_in_ids]
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
                "skills": [*skill_paths_for_agent_type(target), *custom_skills],
                "skill_ids": [*skill_ids_for_agent_type(target), *custom_skill_ids],
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


class Integrator(Agent):
    """Specialized agent type carrying the turn-integrating skill."""

    type_id: AgentType = AgentType.INTEGRATOR


class Verifier(Agent):
    """Specialized agent that approves or rejects a predecessor's work."""

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


def flatten_document_refs(refs: list[DocumentRef]) -> list[DocumentRef]:
    """Return a stable depth-first view of nested document imports."""
    flattened: list[DocumentRef] = []
    for ref in refs:
        flattened.append(ref)
        flattened.extend(flatten_document_refs(ref.imports))
    return flattened


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
    verification: Optional[VerificationResult] = None
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
    document_refs: list[DocumentRef] = Field(default_factory=list)
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
    """A relationship between two nodes. Only CONTAINS or DEPENDS_ON."""

    id: uuid.UUID = Field(default_factory=_new_id)
    # For CONTAINS: src is the parent, dst is the child.
    # For DEPENDS_ON: src is the prerequisite, dst is the dependent.
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
    flow_edges: list[FlowEdge] = Field(default_factory=list)
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

    model_config = ConfigDict(extra="forbid")

    key: str
    objective: str
    generated_prompt: Optional[str] = None
    executor: Optional[str] = None
    agent: Optional[Agent] = None
    # A plan can request a built-in agent specialization while inheriting the
    # parent's harness/model configuration. An explicit ``agent`` remains the
    # escape hatch for a fully configured agent.
    agent_type: Optional[AgentType] = None
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    artifacts: list["ArtifactSpec"] = Field(default_factory=list)
    # Local library ids or HTTP(S) URLs requested for this worker; the server
    # materializes them into the current project's .turn/skills scope.
    skills: list[str] = Field(default_factory=list)

    # placement within the generated graph
    parent_key: Optional[str] = None          # CONTAINS parent (another key)
    # Workflow sequencing is deliberately independent from containment. A
    # verifier is a normal sibling in the graph and names the work it checks
    # only through this ordinary prerequisite relation.
    depends_on: list[str] = Field(default_factory=list)  # prior left-to-right stage keys
    # When True (or executor == "planner") the created node is itself a
    # sub-planner: the runner will decompose it again on its next turn instead
    # of executing it as a leaf. This is intentionally available for very
    # large or uncertain scopes, but should be rare for one user request.
    plan: bool = False


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EdgeType
    src: str  # key
    dst: str  # key


class PlanResult(BaseModel):
    """The output of the Plan operation: schema-valid nodes + edges."""

    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeSpec]
    document_refs: list[DocumentRef] = Field(default_factory=list)
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
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

        containment: dict[str, set[str]] = {key: set() for key in keys}
        dependencies: dict[str, set[str]] = {key: set() for key in keys}
        adjacency: dict[str, set[str]] = {key: set() for key in keys}
        for node in self.nodes:
            if node.skills:
                from turn.skills.library import validate_skill_reference

                for reference in node.skills:
                    validate_skill_reference(reference)
            if node.parent_key:
                if node.parent_key not in known:
                    raise ValueError(f"unknown parent key: {node.parent_key}")
                if node.parent_key == node.key:
                    raise ValueError(f"node {node.key} cannot contain itself")
                containment[node.parent_key].add(node.key)
                adjacency[node.parent_key].add(node.key)
            for dependency in node.depends_on:
                if dependency not in known:
                    raise ValueError(f"unknown dependency key: {dependency}")
                if dependency == node.key:
                    raise ValueError(f"node {node.key} cannot depend on itself")
                dependencies[dependency].add(node.key)
                adjacency[dependency].add(node.key)
            requested_type = node.agent_type or (
                node.agent.type_id if node.agent is not None else None
            )
            if requested_type is AgentType.VERIFIER:
                if node.parent_key:
                    raise ValueError(
                        f"verifier node {node.key} must use depends_on, not parent_key"
                    )
                if len(node.depends_on) != 1:
                    raise ValueError(
                        f"verifier node {node.key} must depend on exactly one target"
                    )

        for edge in self.edges:
            if edge.src not in known or edge.dst not in known:
                missing = edge.src if edge.src not in known else edge.dst
                raise ValueError(f"unknown edge key: {missing}")
            if edge.src == edge.dst:
                raise ValueError(f"node {edge.src} cannot link to itself")
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
        dependency_cycle = cycle_path(dependencies)
        if dependency_cycle:
            raise ValueError(
                "dependency cycle: " + " -> ".join(dependency_cycle)
            )
        combined_cycle = cycle_path(adjacency)
        if combined_cycle:
            raise ValueError(
                "graph cycle across containment/dependency edges: "
                + " -> ".join(combined_cycle)
            )
        return self


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


class WorkerResult(BaseModel):
    """The output of the Execute operation: exactly one outcome."""

    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    summary: str = ""
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)

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
