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

from pydantic import BaseModel, Field

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
    generated_prompt: Optional[str] = None  # prompt handed to the worker

    executor: Optional[str] = None  # worker name (e.g. "codex", "planner")
    status: NodeStatus = NodeStatus.PENDING
    paused: bool = False
    # Project-level execution mode: True = auto-run ready nodes (default);
    # False = manual "step" mode where the runner plans but waits for an
    # explicit step()/run_node() before executing. Effective only on the
    # project root node (id == project_id).
    auto_run: bool = True

    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[uuid.UUID] = Field(default_factory=list)

    # revision & lineage metadata (edits create new revisions, not rewrites)
    revision: int = 1
    superseded_by: Optional[uuid.UUID] = None
    forked_from: Optional[uuid.UUID] = None

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
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)

    # placement within the generated graph
    parent_key: Optional[str] = None          # CONTAINS parent (another key)
    depends_on: list[str] = Field(default_factory=list)  # prerequisite keys


class EdgeSpec(BaseModel):
    type: EdgeType
    src: str  # key
    dst: str  # key


class PlanResult(BaseModel):
    """The output of the Plan operation: schema-valid nodes + edges."""

    nodes: list[NodeSpec]
    edges: list[EdgeSpec] = Field(default_factory=list)
    notes: Optional[str] = None


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
