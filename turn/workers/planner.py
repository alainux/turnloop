"""Planners.

The initial planner and any later decomposition use the *same* operation:
produce the smallest useful workgraph that can begin executing now.

`CodexPlanner` asks Codex to emit a `turn-plan` JSON block. If Codex is
unavailable or returns nothing usable, it falls back to `HeuristicPlanner` so
the graph always appears and execution can start.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from turn.config import settings
from turn.workers import codex_schemas, parsing
from turn.domain.schemas import (
    EdgeSpec,
    EdgeType,
    InputKind,
    InputSpec,
    NodeSpec,
    PlanResult,
    Resource,
)
from turn.workers.base import NodeExecutionContext, Planner, render_context_block

_FENCE_RE = re.compile(r"```(\w+)\n(.*?)```", re.DOTALL)


class HeuristicPlanner(Planner):
    """Generic fallback decomposition — domain-agnostic scaffolding.

    Produces: an investigation leaf (runs immediately), a clarification node
    that BLOCKS on a required decision, a produce leaf that joins on both, and
    a verify leaf. This exercises run / block / dependency-join / complete in
    one graph and is only used when no LLM planner is available.
    """

    name = "heuristic"

    def __init__(self, default_executor: str = "codex"):
        self.default_executor = default_executor

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        objective = ctx.node.objective
        exe = self.default_executor
        nodes = [
            NodeSpec(
                key="investigate",
                objective=f"Investigate the context and gather what is already known about: {objective}",
                executor=exe,
            ),
            NodeSpec(
                key="clarify",
                objective=f"Confirm the key decisions and constraints for: {objective}",
                executor="echo",
                required_inputs=[
                    InputSpec(
                        id="scope",
                        label="Scope, constraints, and success criteria",
                        kind=InputKind.DECISION,
                        description="The exact decisions/limits needed before producing the deliverable.",
                    )
                ],
            ),
            NodeSpec(
                key="produce",
                objective=f"Produce the first concrete deliverable for: {objective}",
                executor=exe,
                depends_on=["investigate", "clarify"],
            ),
            NodeSpec(
                key="verify",
                objective=f"Verify the deliverable against the objective: {objective}",
                executor=exe,
                depends_on=["produce"],
            ),
        ]
        edges = [
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="investigate", dst="produce"),
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="clarify", dst="produce"),
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="produce", dst="verify"),
        ]
        return PlanResult(
            nodes=nodes,
            edges=edges,
            notes="Heuristic fallback decomposition (no LLM planner available).",
        )


class CodexPlanner(Planner):
    """Asks Codex to produce a workgraph as a `turn-plan` JSON block."""

    name = "codex-planner"

    def __init__(self, fallback: Planner | None = None, settings=settings):
        self.s = settings
        self.fallback = fallback

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        prompt = self._build_prompt(ctx)
        cwd = ctx.repo_path or os.getcwd()
        text = await self._call_codex(
            prompt, cwd, stream=getattr(ctx, "stream", None), node_id=ctx.node.id
        )
        plan = self._parse_plan(text, ctx.node.objective)
        if plan is not None and plan.nodes:
            return plan
        if self.fallback is not None:
            return await self.fallback.plan(ctx)
        return PlanResult(nodes=[], notes="planner produced no nodes")

    def _build_prompt(self, ctx: NodeExecutionContext) -> str:
        return f"""{render_context_block(ctx)}
THIS NODE'S OBJECTIVE:
{ctx.node.objective}

You are decomposing THIS node into its direct children. Produce the SMALLEST
useful set of concrete, runnable child steps that can begin executing now.
Decompose only far enough to expose real work.

DECOMPOSITION POLICY — match the structure to the objective:
- ATOMIC step: if the objective is a SINGLE concrete step (it says "one", "a
single", "just one", "the next step", "first step", or names exactly one
action), produce EXACTLY ONE child that performs it. Do NOT pad with
investigate / plan / verify scaffolding.
- BROAD container: if the objective is a wide effort (e.g. "build X",
"create a game", "implement the system", "plan the project"), decompose into
the genuinely distinct sub-tasks and express their relationships using the
TOPOLOGY rules below.
- Prefer one well-specified child over generic multi-step scaffolding. A child
that merely restates this objective is never useful — drop it. Never list the
same sub-task twice, and do NOT create parallel sub-planners that cover the
same scope under different names (e.g. do not make both a "Kanto cities"
planner and a "Kanto guide" planner). Each distinct scope appears exactly once
in the plan. BEFORE you finalize the plan, use the GRAPH EXPLORATION TOOL in
  the context block (python -m turn.tools.graph_explorer --tree) to confirm no
  existing or already-planned node elsewhere in the graph already covers a scope
  you were about to add; if it does, reference or extend that node instead of
  recreating it.

- CARD TITLE (HARD RULE): each child's "objective" MUST be a short
  TITLE-LIKE phrase — at most ~6 words and ~50 characters. It is rendered as
  the graph-card title, so it must be scannable at a glance. Do NOT put a
  sentence, paragraph, or long description in "objective"; put all task detail,
  rationale, file names, and instructions in "generated_prompt".
  GOOD:  "Write chapter 1", "Build the parser", "Style the theme",
         "Assemble the package", "Add the code renderer".
  BAD:   "Design and implement the page-renderer feature area with 2-3
         renderer modules" (too long — move the detail to generated_prompt),
         "Create a comprehensive Markdown->HTML engine and supporting
         renderers for headings, paragraphs, lists and fenced code blocks"
         (paragraph — not a title).
  If you catch yourself writing more than a short title, STOP and move the
  rest into "generated_prompt".

TOPOLOGY — arrange the children to express the real workflow:
- SEQUENTIAL: steps that must run in order. The later step lists its
  prerequisite as a "depends_on" key.
- PARALLEL: independent steps that can run at the same time have NO depends_on.
- COMPLEX: freely mix sequential chains and parallel branches.
- NESTED PLANNERS: for any sub-task that is itself a broad problem, set
  "plan": true AND "executor": "planner". Such a node will be decomposed AGAIN
  on its next turn — so do NOT pre-expand it here; give it a clear objective and
  let it plan its own children. Nest sub-planners as deeply as the problem
  GENUINELY requires: a sub-problem that is still broad should itself become a
  planner, and so on, until the remaining work is concrete enough for leaf
  workers. Do NOT add depth for show — only turn a node into a sub-planner when
  that sub-node is truly a broad problem in its own right (a single concrete
  step should stay a leaf).
- LEAF WORK: every node that actually does work (writing code, prose, files)
  gets "executor": "codex" and a concrete "generated_prompt". The generated_prompt
  MUST tell the worker to WRITE its deliverable to a file in the working
  directory (e.g. create parser.py, recipe.md, or tokyo.md) — not merely return
  text — because downstream assemblers read those files to compose the result.
  Never use "executor": "echo" for real work; only use "shell" for a single shell
  command (put that command alone in generated_prompt).
- COMPOSERS / ASSEMBLERS: if a node's job is to combine or integrate its
  prerequisites (objective names 'assemble', 'merge', 'integrate', 'combine',
  'stitch'), its generated_prompt MUST tell it to READ the files those
  prerequisites already produced in the working directory and merge/stitch them.
  A "depends_on" edge merges each prerequisite's output files up into this
  node's working directory BEFORE it runs, so the node should load and integrate
  those existing files — never regenerate their content from the prompt text.
  Give the node the file names to read (or tell it to read all files produced by
  its dependencies).

ORDER & SAFETY:
- List children IN EXECUTION ORDER (prerequisites before the steps that depend
  on them).
- Never create a cycle (a step must never depend, directly or transitively, on
  itself).
- Only add "required_inputs" for a genuinely EXTERNAL, human-supplied item — a
  decision, credential, account, approval, or a file the user must provide.
  NEVER use required_inputs to hand data from one step to another: a
  "depends_on" edge already guarantees the prerequisite ran first, and its
  outputs are available to the dependent step as context. If a step needs the
  result of a prior step, DEPEND ON that step; do not block on an input for it.
  Leave required_inputs empty unless a real human gate exists.
- Do NOT create a "coordinator" / "oversee" / "manage" wrapper node — this node
  is already the coordinator.
- Every child is a DIRECT child of this node (parent_key must be null).

Return ONLY a fenced code block labeled `turn-plan` containing JSON:
{{
  "nodes": [
    {{"key":"unique","objective":"...","executor":"codex"|"planner","plan":false|true,"generated_prompt":"...","required_inputs":[{{"id":"x","label":"...","kind":"text|decision|credential|account|approval|file"}}],"resource_refs":[],"parent_key":null,"depends_on":["otherkey"]}}
  ],
  "edges": []
}}
"""

    async def _call_codex(self, prompt: str, cwd: str, stream=None, node_id=None) -> str:
        if shutil.which(self.s.codex_binary) is None:
            return ""
        schema_path = codex_schemas.write_schema(codex_schemas.PLAN_SCHEMA)
        bypass = any("bypass" in a for a in self.s.codex_args)
        sandbox_flags = [] if bypass else ["-s", "workspace-write", "--approve-for-me"]
        model_flags = ["-m", self.s.codex_model] if self.s.codex_model else []
        cmd = [
            self.s.codex_binary,
            "exec",
            *model_flags,
            *sandbox_flags,
            "--ephemeral",
            "--output-schema",
            schema_path,
            "-C",
            cwd,
            *self.s.codex_args,
            prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out_buf: list[bytes] = []

            async def _read_out():
                assert proc.stdout is not None
                while True:
                    chunk = await proc.stdout.read(4096)
                    if not chunk:
                        break
                    out_buf.append(chunk)
                    if stream is not None:
                        await stream(node_id, chunk.decode(errors="replace"))

            async def _read_err():
                assert proc.stderr is not None
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    if stream is not None:
                        await stream(node_id, chunk.decode(errors="replace"))

            await asyncio.wait_for(
                asyncio.gather(_read_out(), _read_err(), proc.wait()),
                timeout=self.s.default_run_timeout_seconds,
            )
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            return ""
        finally:
            try:
                os.unlink(schema_path)
            except OSError:
                pass
        return b"".join(out_buf).decode(errors="replace")

    COORDINATOR_KEYS = {"coordinator", "oversee", "manage", "coordinate", "co-ordinate"}

    @staticmethod
    def _norm(s: str) -> str:
        return " ".join((s or "").lower().split())

    @staticmethod
    def _parse_plan(text: str, parent_objective: str | None = None) -> PlanResult | None:
        data = parsing.first_plan_json(text)
        if not isinstance(data, dict) or "nodes" not in data:
            return None

        raw_nodes = [
            NodeSpec(
                key=n["key"],
                objective=n["objective"],
                generated_prompt=n.get("generated_prompt"),
                executor=n.get("executor"),
                required_inputs=[
                    InputSpec(
                        id=i["id"],
                        label=i.get("label", i["id"]),
                        kind=parsing.safe_input_kind(i.get("kind")),
                        description=i.get("description"),
                    )
                    for i in n.get("required_inputs", [])
                ],
                resource_refs=list(n.get("resource_refs", [])),
                parent_key=n.get("parent_key"),
                depends_on=list(n.get("depends_on", [])),
                plan=bool(n.get("plan", False)),
            )
            for n in data.get("nodes", [])
        ]

        # 1) Drop redundant "coordinator"/duplicate nodes; reparent their children.
        # A LONE child whose objective merely echoes the parent is the intended
        # single step (e.g. when the user asked for exactly one) — never drop it,
        # or we'd regress to zero children. Only drop a duplicate-objective node
        # when siblings exist (there it's redundant scaffolding).
        parent_norm = CodexPlanner._norm(parent_objective)
        only_child = len(raw_nodes) == 1
        drop = set()
        for n in raw_nodes:
            kn = n.key.strip().lower()
            on = CodexPlanner._norm(n.objective)
            if on and on == parent_norm and not only_child:
                drop.add(n.key)
            elif kn in CodexPlanner.COORDINATOR_KEYS:
                drop.add(n.key)
        if drop:
            for n in raw_nodes:
                if n.parent_key in drop:
                    n.parent_key = None  # reparent to this node

        nodes = [n for n in raw_nodes if n.key not in drop]
        index = {n.key: i for i, n in enumerate(nodes)}

        # 2) Collect dependencies from both per-node depends_on and explicit
        #    edges (Codex convention: edge src depends on dst). Then enforce a
        #    DAG by keeping only a dependency whose prerequisite appears EARLIER
        #    in the node list (the prompt asks Codex to list in execution order).
        deps: dict[str, set[str]] = {n.key: set(n.depends_on) for n in nodes}
        for e in data.get("edges", []):
            s, d = e.get("src"), e.get("dst")
            if s in deps and d in deps and s != d:
                deps[s].add(d)
        for n in nodes:
            n.depends_on = [
                d for d in deps[n.key] if d in index and index[d] < index[n.key]
            ]

        return PlanResult(nodes=nodes, edges=[], notes=data.get("notes"))
