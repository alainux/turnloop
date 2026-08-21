"""Worker and Planner protocols + the execution context handed to them.

A worker is temporary; it receives a node's context and returns exactly one
outcome (COMPLETE / EXPAND / BLOCK / FAIL). A planner returns a PlanResult.
Neither owns Turn's data model — they only read context and emit results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from turn.capabilities.plugin import CapabilityPluginError, load_capability_plugin
from turn.domain.schemas import (
    Artifact,
    ArtifactSpec,
    InboundMessage,
    Node,
    PlanResult,
    Resource,
    WorkerResult,
    TriggerContext,
)
from turn.workers.terminal import SessionCallback, StreamCallback, TerminalTransport
from turn.metrics import HarnessEvent


class NodeExecutionContext(BaseModel):
    """Everything a worker needs to act on a node."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node: Node
    ancestry: list[Node] = Field(default_factory=list)  # root .. immediate parent
    resources: list[Resource] = Field(default_factory=list)
    repo_path: Optional[str] = None
    project_repo_path: Optional[str] = None
    predecessor_artifacts: list[Artifact] = Field(default_factory=list)
    # Durable information queued for this node is injected only when a new
    # Run context is built. It is never written into an active provider pane.
    inbound_messages: list[InboundMessage] = Field(default_factory=list)
    # General data passing: values resolved from upstream predecessors for
    # this node's declared ``consumes`` names.
    variables: dict[str, str] = Field(default_factory=dict)
    purpose: str = "execute"
    review_feedback: str | None = None
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
    # Stable execution-attempt identity propagated to the CLI submission
    # protocol.  A node id alone is insufficient once a retry exists.
    run_id: str | None = None
    trigger_context: TriggerContext | None = None
    interactive_terminal: bool = False
    timeout_seconds: float | None = None
    stall_timeout_seconds: float | None = None
    telemetry: Callable[[HarnessEvent], Awaitable[None]] | None = None


class Worker(ABC):
    """Executes a leaf node. Must return exactly one outcome."""

    name: str

    @abstractmethod
    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        ...

    def render_artifacts(self, specs: list[ArtifactSpec]) -> list[ArtifactSpec]:
        return specs


class InvalidSubmission(RuntimeError):
    """A provider produced a handoff that needs correction on this Run.

    This is intentionally not a ``WorkerResult(FAIL)``.  The provider may be
    alive and able to correct the same attempt, so turning protocol feedback
    into an infrastructure failure would create a needless retry race.
    """


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


def substitute_prompt_variables(prompt: str, variables: dict[str, str]) -> str:
    """Replace ``${name}`` references with resolved variable values.

    Only resolved names are substituted; unknown references stay literal so a
    missing upstream value is visible in the prompt instead of silently
    disappearing.
    """
    import re

    def replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        return variables[name] if name in variables else match.group(0)

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}", replace, prompt)


def render_context_block(ctx: NodeExecutionContext) -> str:
    """Render the small data envelope sent before the task-specific prompt."""
    variables_line = f"variables={json.dumps(ctx.variables, sort_keys=True)}" if ctx.variables else "variables={}"
    agent = ctx.node.agent
    if agent is None:
        return "\n".join([
            "TURN_CONTEXT",
            f"project_id={ctx.node.project_id}",
            f"node_id={ctx.node.id}",
            f"run_id={ctx.run_id or ''}",
            f"repo={ctx.repo_path or ''}",
            f"project_repo={ctx.project_repo_path or ctx.repo_path or ''}",
            f"purpose={ctx.purpose}",
            f"review_feedback={ctx.review_feedback or ''}",
            f"predecessor_artifacts={json.dumps([item.model_dump(mode='json') for item in ctx.predecessor_artifacts])}",
            f"inbound_messages={json.dumps([item.model_dump(mode='json') for item in ctx.inbound_messages])}",
            variables_line,
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
        f"run_id={ctx.run_id or ''}",
        f"role={agent.type_id.value}",
        f"repo={ctx.repo_path or ''}",
        f"project_repo={ctx.project_repo_path or ctx.repo_path or ''}",
        f"purpose={ctx.purpose}",
        f"review_feedback={ctx.review_feedback or ''}",
        f"predecessor_artifacts={json.dumps([item.model_dump(mode='json') for item in ctx.predecessor_artifacts])}",
        f"inbound_messages={json.dumps([item.model_dump(mode='json') for item in ctx.inbound_messages])}",
        f"harness={agent.harness.value}",
        f"model={agent.model or ''}",
        f"reasoning={agent.reasoning.value}",
        f"activate={' '.join(markers)}",
        variables_line,
        f"trigger_context={ctx.trigger_context.model_dump_json() if ctx.trigger_context else ''}",
    ])
