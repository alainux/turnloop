"""Worker and Planner protocols + the execution context handed to them.

A worker is temporary; it receives a node's context and returns exactly one
outcome (COMPLETE / EXPAND / BLOCK / FAIL). A planner returns a PlanResult.
Neither owns Turn's data model — they only read context and emit results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from turn.capabilities.plugin import CapabilityPluginError, load_capability_plugin
from turn.domain.schemas import (
    ArtifactSpec,
    Node,
    PlanResult,
    Resource,
    WorkerResult,
    TriggerContext,
)
from turn.workers.terminal import SessionCallback, StreamCallback, TerminalTransport


class NodeExecutionContext(BaseModel):
    """Everything a worker needs to act on a node."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node: Node
    ancestry: list[Node] = Field(default_factory=list)  # root .. immediate parent
    resources: list[Resource] = Field(default_factory=list)
    repo_path: Optional[str] = None
    purpose: str = "execute"
    # Optional live stream plus provider-neutral terminal transport. Local
    # harnesses use a true PTY; future cloud adapters can expose equivalent
    # event/input semantics without changing the graph or worker protocol.
    stream: StreamCallback | None = None
    terminal: SkipValidation[TerminalTransport | None] = None
    # Called as soon as a harness exposes a resumable conversation id. The
    # runner persists it before the worker returns, so an interrupted process
    # can be resumed without parsing a completed transcript first.
    session_callback: SessionCallback | None = None
    # A user-triggered rerun must not receive the provider conversation that
    # belonged to the previous attempt. The runner fills this only for an
    # explicit fresh attempt; workers use it to reject a provider that reports
    # the old identity anyway.
    forbidden_session_id: str | None = None
    # One-based durable run number, used by deterministic fixtures to model
    # first-pass/retry behavior without inspecting persistence internals.
    attempt: int = 1
    trigger_context: TriggerContext | None = None
    interactive_terminal: bool = False
    timeout_seconds: float | None = None
    stall_timeout_seconds: float | None = None


class Worker(ABC):
    """Executes a leaf node. Must return exactly one outcome."""

    name: str

    @abstractmethod
    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        ...

    def render_artifacts(self, specs: list[ArtifactSpec]) -> list[ArtifactSpec]:
        return specs


class Planner(ABC):
    """Produces a complete workgraph that can begin executing now."""

    name: str

    @abstractmethod
    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        ...


def _capability_skill_names(ctx: NodeExecutionContext) -> list[str]:
    """Read skill names from loaded project manifests for launch prompting."""
    if ctx.repo_path is None or ctx.node.agent is None:
        return []
    root = Path(ctx.repo_path).expanduser().resolve() / ".turn" / "capabilities"
    names: list[str] = []
    for capability_id in ctx.node.agent.capabilities:
        try:
            package = load_capability_plugin(root / capability_id)
        except (CapabilityPluginError, OSError):
            continue
        names.extend(skill.name for skill in package.skills)
    return list(dict.fromkeys(names))


def _skill_invocation_marker(harness: str, skill_name: str) -> str:
    if harness == "codex":
        return f"${skill_name}"
    if harness == "pi":
        return f"/skill:{skill_name}"
    return f"/{skill_name}"


def render_context_block(ctx: NodeExecutionContext) -> str:
    """Render the small data envelope sent before the task-specific prompt."""
    agent = ctx.node.agent
    if agent is None:
        return "\n".join([
            "TURN_CONTEXT",
            f"project_id={ctx.node.project_id}",
            f"node_id={ctx.node.id}",
            f"repo={ctx.repo_path or ''}",
        ])

    skill_names = _capability_skill_names(ctx)
    markers = [
        _skill_invocation_marker(agent.harness.value, name)
        for name in skill_names
    ]
    return "\n".join([
        "TURN_CONTEXT",
        f"project_id={ctx.node.project_id}",
        f"node_id={ctx.node.id}",
        f"role={agent.type_id.value}",
        f"repo={ctx.repo_path or ''}",
        f"harness={agent.harness.value}",
        f"model={agent.model or ''}",
        f"reasoning={agent.reasoning.value}",
        f"activate={' '.join(markers)}",
        f"trigger_context={ctx.trigger_context.model_dump_json() if ctx.trigger_context else ''}",
    ])
