"""Typed in-memory aggregate owned by the local Store."""
from __future__ import annotations

from dataclasses import dataclass, field
import uuid
from typing import Literal

from turn.domain.schemas import Artifact, Edge, Node, Run


@dataclass
class ProjectState:
    """All durable records for one project.

    The JSON representation remains the Store's existing versioned document;
    this type only makes ownership and the four record collections explicit in
    memory.
    """

    nodes: dict[uuid.UUID, Node] = field(default_factory=dict)
    edges: dict[uuid.UUID, Edge] = field(default_factory=dict)
    runs: dict[uuid.UUID, Run] = field(default_factory=dict)
    artifacts: dict[uuid.UUID, Artifact] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ProjectState":
        return cls()

    def __getitem__(
        self, collection: Literal["nodes", "edges", "runs", "artifacts"]
    ) -> dict[uuid.UUID, Node | Edge | Run | Artifact]:
        """Read-only compatibility for existing diagnostic callers."""
        return getattr(self, collection)
