"""Worker and Planner protocols + the execution context handed to them.

A worker is temporary; it receives a node's context and returns exactly one
outcome (COMPLETE / EXPAND / BLOCK / FAIL). A planner returns a PlanResult.
Neither owns Turn's data model — they only read context and emit results.
"""
from __future__ import annotations

import os
import shlex
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from turn.config import settings
from turn.domain.schemas import (
    ArtifactSpec,
    Node,
    PlanResult,
    Resource,
    WorkerResult,
)
from turn.tools import graph_explorer as _graph_explorer


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
    stream: Any = None
    terminal: Any = None
    # Called as soon as a harness exposes a resumable conversation id. The
    # runner persists it before the worker returns, so an interrupted process
    # can be resumed without parsing a completed transcript first.
    session_callback: Any = None
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
    """Produces the smallest useful workgraph that can begin executing now."""

    name: str

    @abstractmethod
    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        ...


def render_context_block(ctx: NodeExecutionContext) -> str:
    """Render ancestor context + resources into a compact text block."""
    lines: list[str] = []
    if ctx.node.agent is not None:
        agent = ctx.node.agent
        lines.append("TURN LAUNCH CONFIGURATION:")
        lines.append(
            f"- harness: {agent.harness.value}; model: {agent.model or 'harness default'}; "
            f"reasoning: {agent.reasoning.value}; permission: {agent.permission.value}"
        )
        if agent.skills:
            lines.append(f"- skills available at launch: {', '.join(agent.skills)}")
        if agent.tools:
            lines.append(f"- tools allowed at launch: {', '.join(agent.tools)}")
        if agent.mcp_servers:
            lines.append(f"- MCP servers available at launch: {', '.join(agent.mcp_servers)}")
        lines.append("")
        for skill_ref in agent.skills:
            skill_path = Path(skill_ref).expanduser()
            if not skill_path.is_file():
                raise FileNotFoundError(f"agent skill does not exist: {skill_path}")
            skill_text = skill_path.read_text(encoding="utf-8").strip()
            if not skill_text:
                raise ValueError(f"agent skill is empty: {skill_path}")
            lines.append(f"AGENT SKILL: {skill_path}")
            lines.append(skill_text)
            lines.append("")
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
    # GRAPH EXPLORATION TOOL — baked with absolute, copy-pasteable values so
    # the agent needs no environment variables or PYTHONPATH to use it.
    ge_path = os.path.abspath(_graph_explorer.__file__)
    # The graph explorer receives the project's own state file. It remains
    # usable from any assigned project directory because this path is absolute.
    ge_state = os.path.abspath(os.path.join(ctx.repo_path or ctx.node.repo_path or settings.projects_dir, ".turn", "state.json"))
    ge_pid = ctx.node.project_id
    lines.append("GRAPH EXPLORATION TOOL (query the live project graph at runtime):")
    lines.append("  Before you plan or write, explore what is already planned/built so you")
    lines.append("  build on existing work instead of duplicating it. Run this EXACT command:")
    lines.append(
        f'    {shlex.quote(sys.executable)} {shlex.quote(ge_path)} '
        f'--project {ge_pid} --state-file {shlex.quote(ge_state)} '
        f'--requester {ctx.node.id} --tree'
    )
    lines.append("  It prints every node in this project: objective, parent, status, executor,")
    lines.append("  and the files each produced. Useful filters:")
    lines.append("    --node <id>       show one node")
    lines.append("    --children <id>   show a node's direct children")
    lines.append("    --ancestors <id>  show a node's parent chain")
    lines.append("    --format json     machine-readable")
    lines.append("  If a scope (e.g. 'audio', 'engine', 'HUD') already exists or is planned")
    lines.append("  elsewhere in the graph, reference or extend that node rather than")
    lines.append("  recreating it.")
    lines.append("")
    objective = ctx.node.objective.lower()
    if any(word in objective for word in ("assemble", "merge", "integrate", "combine", "stitch")):
        lines.append("INTEGRATOR CONTRACT:")
        lines.append("  This node is ordinary executor integration work. Inspect and reuse prior")
        lines.append("  stage outputs already in the assigned working area. Preserve their")
        lines.append("  Limit changes to assembly, interfaces, wiring, and compatibility; preserve contracts,")
        lines.append("  and do not recreate their domain content.")
        lines.append("")
    return "\n".join(lines)
