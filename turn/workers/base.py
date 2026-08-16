"""Worker and Planner protocols + the execution context handed to them.

A worker is temporary; it receives a node's context and returns exactly one
outcome (COMPLETE / EXPAND / BLOCK / FAIL). A planner returns a PlanResult.
Neither owns Turn's data model — they only read context and emit results.
"""
from __future__ import annotations

import os
import shlex
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from turn.domain.schemas import (
    ArchitectureSpec,
    ArtifactSpec,
    Node,
    PlanResult,
    Resource,
    WorkerResult,
)
from turn.domain.specification import architecture_spec_text


class NodeExecutionContext(BaseModel):
    """Everything a worker needs to act on a node."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    node: Node
    ancestry: list[Node] = Field(default_factory=list)  # root .. immediate parent
    resources: list[Resource] = Field(default_factory=list)
    # The root project metadata and the nearest branch metadata are both
    # available so nested planners cannot lose the original intent.
    project_spec: Optional[ArchitectureSpec] = None
    branch_spec: Optional[ArchitectureSpec] = None
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
    """Produces a complete workgraph that can begin executing now."""

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
        lines.append(f"- Turn node id: {ctx.node.id}")
        lines.append(
            f"- harness: {agent.harness.value}; model: {agent.model or 'harness default'}; "
            f"reasoning: {agent.reasoning.value}; permission: {agent.permission.value}"
        )
        if agent.skill_ids:
            lines.append(
                "- skills available through the project-scoped Turn library: "
                + ", ".join(agent.skill_ids)
            )
        if agent.tools:
            lines.append(f"- tools allowed at launch: {', '.join(agent.tools)}")
        if agent.mcp_servers:
            lines.append(f"- MCP servers available at launch: {', '.join(agent.mcp_servers)}")
        lines.append(
            "- Use `turn skills list` and `turn skills show <id>` for local skills; "
            "external references are installed under `.turn/skills` and their "
            "paths are listed in TURN_AGENT_SKILLS. Skill text is delivered "
            "through the project filesystem, not appended to this initial prompt."
        )
        lines.append(
            "- Project-authored skills use `project:<slug>` and live at "
            "`.turn/skills/<slug>/SKILL.md`; read them from the project filesystem."
        )
        lines.append(
            "- Before acting, read every selected skill file from `TURN_AGENT_SKILLS` "
            "and apply its contract to this node. Skill text is not a substitute "
            "for inspecting the graph, predecessor outputs, or the user's outcome."
        )
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
    project_spec = ctx.project_spec
    branch_spec = ctx.branch_spec
    if project_spec is None or branch_spec is None:
        for candidate in [*ctx.ancestry, ctx.node]:
            if candidate.architecture_spec is None:
                continue
            if project_spec is None:
                project_spec = candidate.architecture_spec
            branch_spec = candidate.architecture_spec
    if project_spec is not None:
        lines.append("PROJECT GRAPH ARCHITECTURE METADATA (read-only):")
        lines.append(architecture_spec_text(project_spec))
        lines.append("")
    if branch_spec is not None and branch_spec != project_spec:
        lines.append("CURRENT BRANCH ARCHITECTURE METADATA (read-only):")
        lines.append(architecture_spec_text(branch_spec))
        lines.append("")
    # GRAPH EXPLORATION TOOL — expose the same installed Turn CLI that agents
    # use for status and handoff. The agent is launched in the assigned project
    # directory, so the project-local state path stays relative and no internal
    # Python module path leaks into the agent protocol.
    turn_cli = os.getenv("TURN_CLI") or shutil.which("turn")
    if not turn_cli:
        raise RuntimeError(
            "Turn CLI is not installed; install the project package before launching workers"
        )
    ge_pid = ctx.node.project_id
    lines.append("GRAPH EXPLORATION TOOL (query the live project graph at runtime):")
    lines.append("  Before you plan or write, explore what is already planned/built so you")
    lines.append("  build on existing work instead of duplicating it. Run this EXACT command:")
    lines.append(
        f'    {shlex.quote(turn_cli)} graph {shlex.quote(str(ge_pid))} '
        f'--requester {shlex.quote(str(ctx.node.id))} --tree'
    )
    lines.append("  It prints every node in this project: ids, hierarchy, dependencies, status,")
    lines.append("  instructions, agent configuration/session, run history, and produced files.")
    lines.append("  Use --format json when you need the complete machine-readable spec state.")
    lines.append("  Useful filters:")
    lines.append("    --node <id>       show one node")
    lines.append("    --children <id>   show a node's direct children")
    lines.append("    --ancestors <id>  show a node's parent chain")
    lines.append("    --format json     machine-readable")
    lines.append("  If a scope (e.g. 'audio', 'engine', 'HUD') already exists or is planned")
    lines.append("  elsewhere in the graph, reference or extend that node rather than")
    lines.append("  recreating it.")
    lines.append("")
    objective = ctx.node.objective.lower()
    agent_type = ctx.node.agent.type_id.value if ctx.node.agent is not None else ""
    if agent_type == "integrator" or any(
        word in objective for word in ("assemble", "merge", "integrate", "combine", "stitch")
    ):
        lines.append("INTEGRATOR CONTRACT:")
        lines.append("  Read and reuse all prerequisite outputs already in the assigned working area.")
        lines.append("  Make those outputs form one coherent result that satisfies the original user objective.")
        lines.append("  Limit changes to assembly, interfaces, wiring, and necessary integration fixes;")
        lines.append("  preserve prerequisite domain content and do not create an integrator-only directory.")
        lines.append("  Verify the real user-facing launch/use path, not only imports or unit tests.")
        lines.append("")
    if agent_type == "verifier":
        lines.append("VERIFIER CONTRACT:")
        lines.append("  Inspect the target's actual deliverables, graph contracts, invariants, and user-facing behavior.")
        lines.append("  Approve only when the stated acceptance criteria are evidenced; otherwise reject with concrete findings.")
        lines.append("  Submit exactly one APPROVE or REJECT decision through `turn agent verify --payload '<JSON_OBJECT>'`.")
        lines.append("  Do not edit the target's work and do not write Turn protocol files directly.")
        lines.append("")
    return "\n".join(lines)
