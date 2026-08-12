"""Worker and Planner protocols + the execution context handed to them.

A worker is temporary; it receives a node's context and returns exactly one
outcome (COMPLETE / EXPAND / BLOCK / FAIL). A planner returns a PlanResult.
Neither owns Turn's data model — they only read context and emit results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from turn.domain.schemas import (
    ArtifactSpec,
    Node,
    PlanResult,
    Resource,
    WorkerResult,
)


class NodeExecutionContext(BaseModel):
    """Everything a worker needs to act on a node."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node: Node
    ancestry: list[Node] = Field(default_factory=list)  # root .. immediate parent
    resources: list[Resource] = Field(default_factory=list)
    repo_path: Optional[str] = None
    # Optional live stream for raw tool/agent output (e.g. terminal bytes).
    # The runner wires this to the project's SSE bus. TODO(real-pty): replace
    # this one-way mirror with a true bidirectional PTY so a human can type
    # into the agent's terminal.
    stream: Any = None


class Worker(ABC):
    """Executes a leaf node. Must return exactly one outcome."""

    name: str

    @abstractmethod
    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        ...

    def render_artifacts(self, specs: list[ArtifactSpec]) -> list[ArtifactSpec]:
        return specs


class Planner(ABC):
    """Produces the smallest useful workgraph that can begin executing now."""

    name: str

    @abstractmethod
    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        ...


def render_context_block(ctx: NodeExecutionContext) -> str:
    """Render ancestor context + resources into a compact text block."""
    lines: list[str] = []
    if ctx.ancestry:
        lines.append("ANCESTOR CONTEXT (root -> parent):")
        for a in ctx.ancestry:
            lines.append(f"- {a.objective}")
        lines.append("")
    if ctx.resources:
        lines.append("ATTACHED RESOURCES / SKILLS:")
        for r in ctx.resources:
            body = (r.content or "").strip()
            if body:
                lines.append(f"# {r.ref}\n{body}")
            else:
                lines.append(f"# {r.ref} (ref only)")
        lines.append("")
    return "\n".join(lines)
