"""Typed in-memory aggregate owned by the local Store."""
from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Literal

from turn.domain.lead import ProjectLead, ReviewRequest
from turn.domain.organization import BudgetRequest, Handoff, WorkItem
from turn.domain.schemas import Artifact, Edge, InboundMessage, Node, Run, Trigger


@dataclass
class ProjectState:
    """All durable records for one project.

    The JSON representation remains the Store's existing versioned document;
    this type only makes ownership and the record collections explicit in
    memory.
    """

    nodes: dict[uuid.UUID, Node] = field(default_factory=dict)
    edges: dict[uuid.UUID, Edge] = field(default_factory=dict)
    runs: dict[uuid.UUID, Run] = field(default_factory=dict)
    artifacts: dict[uuid.UUID, Artifact] = field(default_factory=dict)
    triggers: dict[uuid.UUID, Trigger] = field(default_factory=dict)
    work_items: dict[uuid.UUID, WorkItem] = field(default_factory=dict)
    handoffs: dict[uuid.UUID, Handoff] = field(default_factory=dict)
    budget_requests: dict[uuid.UUID, BudgetRequest] = field(default_factory=dict)
    review_requests: dict[uuid.UUID, ReviewRequest] = field(default_factory=dict)
    # Bootstrap automation: BOOTSTRAPPING while the lead/planner bootstrap
    # loop runs the root plan to acceptance, READY once accepted (or when a
    # user interrupts). Existing projects default to READY.
    bootstrap_status: str = "READY"
    # Exactly one project lead per project. ``None`` only for projects that
    # predate the lead model until their next normalization pass creates it.
    lead: ProjectLead | None = None
    inbound_messages: dict[uuid.UUID, InboundMessage] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ProjectState":
        return cls()

    def __getitem__(
        self, collection: Literal["nodes", "edges", "runs", "artifacts", "triggers", "work_items", "handoffs", "budget_requests", "review_requests"]
    ) -> dict[uuid.UUID, Node | Edge | Run | Artifact | Trigger | WorkItem | Handoff | BudgetRequest | ReviewRequest]:
        """Read-only compatibility for existing diagnostic callers."""
        return getattr(self, collection)
