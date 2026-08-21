"""Read-only graph state exposed to agents through the graph explorer."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from turn.domain.schemas import (
    Agent,
    ArtifactKind,
    DocumentRef,
    Edge,
    InputSpec,
    NodeStatus,
    Outcome,
    ProcessState,
    RunPolicy,
    RunStatus,
    RuntimeGuard,
    SubgraphRef,
    VerificationResult,
)


class GraphInspectionRun(BaseModel):
    """A concise execution history entry without terminal transcript noise."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    attempt: int
    worker: str
    status: RunStatus
    outcome: Optional[Outcome] = None
    summary: Optional[str] = None
    error: Optional[str] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    session_id: Optional[str] = None
    process_state: ProcessState = ProcessState.LAUNCH_REQUESTED
    process_exit_code: Optional[int] = None
    pane_id: Optional[str] = None
    provider: Optional[str] = None
    accepted_submission: bool = False


class GraphInspectionArtifact(BaseModel):
    """Artifact identity exposed for coordination; content stays in the project."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    node_id: Optional[uuid.UUID] = None
    kind: ArtifactKind
    name: str
    ref: Optional[str] = None


class GraphInspectionNode(BaseModel):
    """The complete coordination-relevant state of one graph node."""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    parent_id: Optional[uuid.UUID] = None
    objective: str
    instructions: Optional[str] = None
    status: NodeStatus
    executor: Optional[str] = None
    agent: Optional[Agent] = None
    session_id: Optional[str] = None
    agent_state: Optional[str] = None
    agent_message: Optional[str] = None
    runtime_guard: Optional[RuntimeGuard] = None
    verification: Optional[VerificationResult] = None
    paused: bool = False
    auto_run: bool = True
    run_policy: Optional[RunPolicy] = None
    required_inputs: list[InputSpec] = Field(default_factory=list)
    resource_refs: list[str] = Field(default_factory=list)
    document_refs: list[DocumentRef] = Field(default_factory=list)
    subgraph_refs: list[SubgraphRef] = Field(default_factory=list)
    artifact_refs: list[uuid.UUID] = Field(default_factory=list)
    provides: list[str] = Field(default_factory=list)
    consumes: list[str] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    route_taken: Optional[str] = None
    follows: list[uuid.UUID] = Field(default_factory=list)
    children: list[uuid.UUID] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    artifacts: list[GraphInspectionArtifact] = Field(default_factory=list)
    runs: list[GraphInspectionRun] = Field(default_factory=list)


class GraphInspection(BaseModel):
    """The read-only spec and coordination snapshot for one project."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 6
    project_id: uuid.UUID
    nodes: list[GraphInspectionNode] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
