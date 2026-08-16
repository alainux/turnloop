"""Planners.

The initial planner and any later decomposition use the *same* operation:
produce a complete workgraph that can begin executing now, with no duplicate
responsibilities or unnecessary coordination nodes.

`CodexPlanner` asks Codex to submit a plan through Turn's CLI. If it submits
nothing usable, the run fails visibly. ``HeuristicPlanner``
is a deterministic test fixture and is never selected by the served application.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path

from turn.config import settings
from turn.contracts.dag import plan_handoff_example
from turn.workers import parsing
from turn.domain.schemas import (
    AgentConfig,
    ArchitectureDiagram,
    ArchitectureDiagramEdge,
    ArchitectureDiagramNode,
    ArchitectureSection,
    ArchitectureSpec,
    AgentType,
    EdgeSpec,
    EdgeType,
    HarnessKind,
    InputKind,
    InputSpec,
    NodeSpec,
    PlanResult,
    PermissionMode,
    Resource,
    Usage,
)
from turn.workers.base import NodeExecutionContext, Planner, render_context_block
from turn.workers.harnesses import recover_session_id
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.interactive import (
    agent_environment,
    opencode_session_ids,
    prepare_result_file,
    read_result_file,
    result_handoff,
    run_until_result,
)
from turn.workers.terminal import GenerationStalled, LocalPtyTransport

class HeuristicPlanner(Planner):
    """Deterministic test-only decomposition — domain-agnostic scaffolding.

    Produces a few independent domain lanes and an integrator agent that
    recomposes their outputs. This keeps the deterministic test fixture
    of Turn's architectural model instead of manufacturing a checklist.
    """

    name = "heuristic"

    def __init__(self, default_executor: str = "codex"):
        self.default_executor = default_executor

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        # A root may have a concise project name while its complete intent is
        # stored in generated_prompt. Keep graph-card objectives compact and
        # put the authoritative detail in the execution prompt.
        objective = ctx.node.generated_prompt or ctx.node.objective
        # The project/node agent is the user's explicit worker choice.  The
        # registry's default only applies when a plan is created without an
        # agent (for example, a headless legacy project).  Freezing the
        # registry default here caused a project visibly configured for Codex
        # to silently fan out Echo children after workspace preferences had
        # changed at runtime.
        exe = (
            ctx.node.agent.harness.value
            if ctx.node.agent is not None
            else self.default_executor
        )
        nodes = [
            NodeSpec(
                key="core",
                objective="Define core structure",
                generated_prompt=(
                    f"Own the core concepts, constraints, and invariants for: {objective}. "
                    "Work independently in a domain-appropriate scope directory or output namespace. "
                    "Write the resulting contract and concrete deliverables to files for a later integrator."
                ),
                executor=exe,
            ),
            NodeSpec(
                key="inputs",
                objective="Handle inputs and storage",
                generated_prompt=(
                    f"Own the input, persistence, or source-material boundary for: {objective}. "
                    "Choose a domain-appropriate scope directory or output namespace, document its contract, "
                    "and write the deliverables there so another worker can consume them."
                ),
                executor=exe,
            ),
            NodeSpec(
                key="outputs",
                objective="Create output surface",
                generated_prompt=(
                    f"Own the user-facing, presentation, publishing, or delivery surface for: {objective}. "
                    "Work in a domain-appropriate scope directory or output namespace, state the assumptions "
                    "and invariants you honor, and write concrete deliverables for a later integrator."
                ),
                executor=exe,
            ),
            NodeSpec(
                key="integrate",
                objective="Integrate the deliverable",
                generated_prompt=(
                    f"Read the outputs produced by the core, inputs, and outputs lanes and integrate them into "
                    f"one coherent result for: {objective}. Preserve each lane's contracts, resolve conflicts "
                    "explicitly, run the real user-facing launch command and an end-to-end check, and make the "
                    "requested result usable from the assigned project directory. Do not create an integration "
                    "directory or duplicate the lane implementations; wire the existing package entry points."
                ),
                executor=exe,
                agent_type="integrator",
                depends_on=["core", "inputs", "outputs"],
            ),
        ]
        edges = [
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="core", dst="integrate"),
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="inputs", dst="integrate"),
            EdgeSpec(type=EdgeType.DEPENDS_ON, src="outputs", dst="integrate"),
        ]
        return PlanResult(
            nodes=nodes,
            architecture_spec=ArchitectureSpec(
                title=f"Architecture: {ctx.node.objective}",
                executive_summary=(
                    f"A modular implementation plan for {objective}, with independent "
                    "domain lanes and one final integration boundary."
                ),
                approach=(
                    "Establish the core domain contract first, develop independent "
                    "input/storage and user-facing output lanes in parallel, then "
                    "integrate them into the requested deliverable."
                ),
                strategy=(
                    "Keep boundaries explicit, preserve the user's end-to-end outcome, "
                    "and make the final integrator validate the real launch/use path."
                ),
                architecture_principles=[
                    "Independent lanes own stable boundaries.",
                    "Dependencies describe real artifact or contract consumption.",
                    "The final integration produces the user-facing result.",
                ],
                requirements=[objective],
                acceptance_criteria=[
                    "The requested result is present in the assigned project directory.",
                    "The result can be launched, used, or inspected through its intended path.",
                ],
                sections=[
                    ArchitectureSection(
                        id="workstreams",
                        title="Workstreams and ownership",
                        content=(
                            "The core, input/storage, and output lanes are independent "
                            "where possible. The integration lane consumes their concrete "
                            "outputs and owns the final assembly."
                        ),
                        diagram_ids=["workstream-flow"],
                    ),
                    ArchitectureSection(
                        id="delivery",
                        title="Delivery and verification",
                        content=(
                            "The integrator verifies the actual user-facing outcome rather "
                            "than stopping at isolated contracts or passing unit tests."
                        ),
                    ),
                ],
                diagrams=[
                    ArchitectureDiagram(
                        id="workstream-flow",
                        title="Workstream flow",
                        description="Independent work converges at the final integration boundary.",
                        nodes=[
                            ArchitectureDiagramNode(id="core", label="Core structure", kind="domain"),
                            ArchitectureDiagramNode(id="inputs", label="Inputs and storage", kind="boundary"),
                            ArchitectureDiagramNode(id="outputs", label="Output surface", kind="surface"),
                            ArchitectureDiagramNode(id="integrate", label="Final integration", kind="integration"),
                        ],
                        edges=[
                            ArchitectureDiagramEdge(src="core", dst="integrate"),
                            ArchitectureDiagramEdge(src="inputs", dst="integrate"),
                            ArchitectureDiagramEdge(src="outputs", dst="integrate"),
                        ],
                    )
                ],
            ),
            edges=edges,
            notes="Heuristic architectural decomposition (no LLM planner available).",
        )


class CodexPlanner(Planner):
    """Asks Codex to submit a workgraph through the Turn CLI."""

    name = "codex-planner"

    def __init__(self, settings=settings):
        self.s = settings

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        prompt = self._build_prompt(ctx)
        cwd = ctx.repo_path or os.getcwd()
        text, usage, session_id = await self._call_codex(
            prompt, cwd, agent=ctx.node.agent,
            stream=getattr(ctx, "stream", None), node_id=ctx.node.id,
            project_id=ctx.node.project_id,
            terminal=ctx.terminal, timeout=ctx.timeout_seconds,
            stall_timeout=ctx.stall_timeout_seconds,
            session_callback=ctx.session_callback,
        )
        plan = AgentPlanner._parse_plan(text, ctx.node.objective)
        if plan is not None and plan.nodes:
            plan.usage = usage
            plan.session_id = session_id
            return plan
        raise RuntimeError("planner returned no valid turn-plan; no heuristic fallback is enabled")

    def _build_prompt(self, ctx: NodeExecutionContext) -> str:
        handoff_example = plan_handoff_example()
        return f"""{render_context_block(ctx)}
THIS NODE'S OBJECTIVE:
{ctx.node.objective}

PLANNING INSTRUCTIONS FOR THIS NODE:
{ctx.node.generated_prompt or "No additional planning instructions."}

You are the architect decomposing THIS node into its direct children. Produce
a complete workgraph that divides all of the actual labor required to
accomplish the user's objective, using the smallest number of meaningful
responsibilities. This is an orchestration effort, not a mere abstraction
exercise or chronological checklist.

DELIVERY BAR — preserve the requested product:
- Unless the user explicitly requests an MVP, proof of concept, prototype,
  demo, spike, mock, or other limited slice, plan for the complete finished product
  described by the request. Do not silently convert it into an MVP,
  POC, vertical slice, framework, or intentionally small first release.
- Cover every requested capability, user interaction, integration, quality
  expectation, and acceptance condition. “Smallest useful” refers only to
  avoiding duplicate nodes and unnecessary coordination; it never authorizes
  omitting product scope.
- If a limited scope is explicitly requested, record the omitted scope and
  the resulting acceptance boundary in the architecture metadata.
- The final integrator must make the complete requested result runnable and
  usable, not merely prove that isolated modules or a demo shell exist.

ARCHITECTURAL METADATA — the graph is also the durable project specification:
- For a broad product, system, or multi-part deliverable, include an
  `architecture_spec` object in the submitted PlanResult. This is mandatory
  for broad work and optional only when the objective is genuinely atomic.
- Treat it as an implementation-ready architecture brief, not a task-list
  preamble. It must explain the requested outcome, approach, strategy,
  boundaries, important decisions, constraints, risks, and acceptance criteria.
- Use `sections` for substantive, named parts of the approach. Each section
  has an id, title, Markdown `content`, and optional `subsections`. The
  document view derives its table of contents from this section tree, so use
  meaningful section order and nesting.
- Use typed `diagrams` when a relationship, data flow, system boundary, or
  execution topology is clearer visually. A diagram has nodes with stable ids
  and edges using those ids; do not submit decorative or generic diagrams.
- For a visual, spatial, game, brand, or interaction-heavy outcome, use the
  project-scoped imagegen skill to create at least one purposeful concept
  reference when it would reduce implementation ambiguity. Store the image
  under turn/concepts/ and include it in architecture_spec.concept_images with exactly
  {{id,title,source,alt,caption}}. source is the project-relative path or a
  direct HTTPS URL. Do not invent a concept image entry for a non-visual
  objective. Concept images are references for workers and verifiers, not
  substitutes for runnable deliverables. The worker that creates or changes
  the image reports its path through the normal Turn CLI artifact workflow.
- Include `filesystem_structure` as a concise plain-text tree for the project
  (for example `src/`, `tests/`, and the domain/application boundaries). This
  is graph metadata shared with workers, not a second plan handoff. It must
  name the real application/package entry point, test locations, asset or
  concept locations where relevant, and the integration boundary that a clean
  checkout will launch. Every concrete node prompt must respect this tree.
- Include `research_sources` with the direct URLs actually consulted while
  planning. Use the harness's web/search facility when available, and also
  use `turn skills show find-skills`, `npx skills find`, skills.sh, Agency
  Agents, and authoritative project documentation as appropriate. Do not
  fabricate sources or rely only on intuition for a broad product.
- Keep the metadata specific to this request. Do not pad it with generic
  headings. Every work node must map to a meaningful section or boundary in
  the spec, and every integrator must be accountable for the acceptance
  criteria, not merely for merging files.
- The submitted architecture metadata is persisted on this graph boundary and
  is automatically shown to every descendant worker. Do not write a separate
  architecture document to the filesystem as a Turn protocol handoff.
- The architecture metadata is strict JSON. Use only the fields in the exact
  example below; do not invent aliases or extra fields. A minimal valid spec
  has only `title`, `executive_summary`, `approach`, `strategy`, and text
  `sections`; all other fields are optional and should be omitted when they do
  not materially help. In particular, do not emit `decisions`, `risks`, or
  `diagrams` just to fill a template. If you do emit them, complete every
  required field exactly as documented below—never submit a half-shaped item.
  A section is only `id`, `title`, Markdown `content`, and optional nested
  `subsections`; do not put decisions, risks, or diagram objects inside it.
- If you use `decisions`, each item is exactly `{{id,title,decision,rationale,
  consequences}}`, where `consequences` is one short text string. If you use `risks`, each item is exactly
  `{{id,title,description,mitigation}}`. Diagram nodes are exactly
  `{{id,label,kind}}` and diagram edges are exactly `{{src,dst,label}}`. Do not
  add `id`-like keys to a section or invent alternate names such as `choice`,
  `why`, `from`, or `to`.
- Put dependencies only in each node's `depends_on` list. Omit the optional
  top-level `edges` array; Turn creates the graph edges from those lists. This
  avoids a duplicate relationship syntax and the edge `type` enum entirely.

First preserve the requested outcome. State what must be runnable, usable,
readable, or otherwise deliverable at the end. For software, account for the
concrete runtime/host, launch command, user interaction loop, and a complete
end-to-end acceptance scenario. A collection of contracts, mocks, services,
or tests is not a finished application unless the user explicitly requested
those things.

DECOMPOSITION POLICY — match the structure to the objective:
- ATOMIC step: if the objective is a SINGLE concrete step (it says "one", "a
single", "just one", "the next step", "first step", or names exactly one
action), produce EXACTLY ONE child that performs it. Do NOT pad with
investigate / plan / verify scaffolding.
- BROAD container: if the objective is a wide effort (e.g. "build X",
"create a game", "implement the system", "plan the project"), identify the
genuinely distinct domains, modules, capabilities, or output sections that
make up the result. Prefer 2–5 orthogonal direct children that can proceed in
parallel. Name them in the vocabulary of the domain; do not turn milestones,
tests, or generic approval steps into fake domains.
- PARALLELISM AUDIT: for a broad software product, at least two direct product
branches should normally have empty `depends_on` lists and be runnable from
the user request plus stable interfaces. Do not serialize every branch behind
one content or architecture branch just to impose order. Add that dependency
only when the downstream branch truly cannot begin without the sibling's
concrete files, contract, or decision; the final integrator reconciles the
independent outputs.
- Prefer one well-specified child over generic multi-step scaffolding. A child
that merely restates this objective is never useful — drop it. Never list the
same sub-task twice, and do NOT create parallel sub-planners that cover the
same scope under different names (e.g. do not make both a "Kanto cities"
planner and a "Kanto guide" planner). Each distinct scope appears exactly once
in the plan. BEFORE you finalize the plan, use the GRAPH EXPLORATION TOOL in
  the context block (`turn graph "$TURN_PROJECT_ID" --tree`) to confirm no
  existing or already-planned node
  elsewhere in the graph already covers a scope
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

TOPOLOGY — arrange the children to express a left-to-right architectural flow:
- PARALLEL ONLY WHEN INDEPENDENT: children may omit depends_on only when they
  can create their deliverables without reading another child's files or
  contracts. Parallelism is useful, but it is never a substitute for a real
  prerequisite.
- SEQUENCE CONTRACTS: if a child consumes a sibling's domain model, API,
  schema, fixtures, or files, add that sibling to depends_on. In software
  projects, tests that exercise implementation work depend on the relevant
  implementation branches; they must not be parallel merely because they are
  called "tests".
- INTEGRATION: when several sibling outputs must be recombined, add an
  integrator child whose objective says integrate, assemble, merge, or
  otherwise recombine. Set `agent_type` to `integrator`, list all sibling
  outputs in `depends_on`, and make its prompt tell it to read and wire those
  existing outputs. It must not create a special integration directory or
  regenerate prerequisite domain work.
- SINGULAR PRODUCT: when THIS node represents one product, deliverable, or
  end-to-end outcome, add exactly one final integrator to produce that
  result. It must depend on every direct branch output, including nested
  planner/container branches. That final node is the only terminal output at
  this workflow boundary. Multiple terminal outputs are appropriate only when
  the objective explicitly requests multiple products or independent outputs.
- FINAL INTEGRATION: name the singular-product node integrate, assemble,
  merge, or otherwise recombine, and make it the final stage after all branch
  integrators or major outputs. A nested planner is itself a direct branch
  output; depend on its container key rather than reaching into its future
  grandchildren.
- SEQUENTIAL: use a dependency whenever later work truly cannot begin before
  earlier output. Such edges are left-to-right workflow stages and should be
  explicit; never use them just to make a checklist or to order unrelated
  work.
- NESTED PLANNERS: for a sub-domain that is itself broad, set "plan": true
  and "executor": "planner". This is an exception, not the normal shape for
  one user's request: prefer direct concrete executors and a final integrator
  so the initial specification is visible at a glance. Use a nested planner
  only for a genuinely huge or uncertain scope, such as a multi-organization
  enterprise, many independently governed platforms, or a sub-system whose
  architecture cannot responsibly be decided yet. If the work can be named
  and assigned now, do that instead. Do not add a planner at every
  bifurcation: a broad parent may directly create concrete executors and
  integrators.
- LEAF WORK: every node that actually does work (writing code, prose, files)
  gets "executor": "codex" and a concrete "generated_prompt". The generated_prompt
  MUST tell the worker to WRITE its deliverable to a domain-appropriate file,
  directory, section, or other durable output in the assigned working area —
  not merely return text — because downstream integrators read those outputs.
  When separate directories, namespaces, chapters, or collections reduce
  collisions, assign one to each domain child and name it in the prompt. Keep
  this guidance agnostic: a directory may mean a real folder, an output
  namespace, a chapter, or another natural unit for the domain.
  Never use "executor": "echo" for real work; only use "shell" for a single shell
  command (put that command alone in generated_prompt).
- CONTRACTS & INVARIANTS: every child prompt must state the boundary it owns,
  expected inputs and outputs, contracts or invariants, and where its durable
  result lives. An integrator must preserve or reconcile those contracts. Do
  not assume every objective is software; use equivalent concepts for books,
  research, operations, design, or other kinds of generation.
- SKILLS: before submitting, run `turn skills show find-skills`, search for
  guidance separately for each concrete executor, integrator, and verifier,
  inspect the candidate, and choose the narrowest useful match. Every such
  node must declare the selected references in its `skills` array; do not omit
  the field or rely on the server to infer it. A local built-in skill is valid
  when it fits. An external reference must be a direct HTTP(S) URL to the
  skill document. If no suitable skill exists, author a concise
  `.turn/skills/<slug>/SKILL.md` with YAML `name` and `description` frontmatter
  and reference it as `project:<slug>`. Do not paste skill bodies into prompts.
  Record the sources actually consulted in `architecture_spec.research_sources`.
  Every concrete executor, integrator, and verifier must declare the
  project skill references it needs in the node's `skills` array. A reference
  is either a local library id or a direct HTTP(S) URL to the selected skill
  document. Use the planner's `find-skills` guidance to research candidates
  (including skills.sh, agency-agents, `npx skills find`, and GitHub's
  agent-skills topic), then choose only skills that materially improve the
  assigned work. The server installs URL references into the current project's
  `.turn/skills` directory. Do not paste skill bodies into the prompt. Planners
  automatically have only `turn-planning`, `imagegen`, and `find-skills`; all
  domain, architecture, QA, and product skills must be deliberately selected
  for the relevant worker. Use the image skill whenever a visual reference
  will guide implementation or QA.
- VERIFICATION: almost every concrete executor or integrator should be
  followed by a verifier when its output has meaningful code, visual, runtime,
  or contract risk. A verifier is an ordinary sibling at this planning
  boundary: set `agent_type` to `verifier`, omit `parent_key`, and put exactly
  the implementation key it checks in `depends_on`. This is the only graph
  relationship for verification. It makes the verifier appear immediately
  after the work it inspects without inventing containment or a VERIFIES edge.
  A verifier must receive explicit criteria in its prompt and inspect real
  evidence before approving. Rejection feedback is a runtime Herdr
  conversation with that one predecessor; it is not additional graph data.
- INTEGRATORS: if a node's job is to combine or integrate its
  prerequisites (objective names 'assemble', 'merge', 'integrate', 'combine',
  'stitch'), its generated_prompt MUST tell it to READ the files those
  prerequisites already produced in the working directory and merge/stitch them.
  A "depends_on" edge means each prerequisite has already run in this same
  assigned project area, so the node should load and integrate those existing
  outputs — never regenerate their content from the prompt text. Its result
  belongs in the existing package/application composition boundary, not in a
  new integrator-specific directory. It must verify the actual user-facing
  outcome and report failure if only a framework was produced.

ORDER & SAFETY:
- List children in architectural order from left to right: domain branches,
  then their integrators, then any final integration. Prerequisites should
  appear before dependent nodes, but unrelated parallel branches need not be
  artificially serialized.
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
- Every child is a DIRECT child of this node (parent_key must be null). A
  later planning turn can give a nested planner its own direct children.
- If the user asks to revise an already-expanded plan, return the complete
  replacement subtree for this planning boundary using fresh local `key`
  values. Do not put persisted UUIDs in `parent_key`, `depends_on`, or any
  invented target field. Existing descendants are replaced as one graph
  operation; the current planning node remains the boundary and its session
  remains the conversation context.

FINAL RESEARCH AND SKILL CHECK:
- Before submission, actually run `turn skills show find-skills`, inspect the
  repository and live graph, and perform the relevant web/search or skills.sh
  queries. Do not claim a source you did not consult.
- For broad engineering work, investigate the domain before choosing nodes.
  `turn skills show turn-architecture-research` is an optional architecture
  reference to inspect when it fits; it is not automatically assigned to this
  planner. A game plan that only names an engine and a story is incomplete
  unless the request genuinely has no other product boundary.
- Record each direct URL consulted in `architecture_spec.research_sources`.
- Every concrete executor, integrator, and verifier must have a non-empty
  `skills` array containing the selected local id, direct skill URL, or a
  `project:<slug>` skill you authored under `.turn/skills/<slug>/SKILL.md`.
- For each selected skill, make the node prompt state the contract it improves
  and make sure the file will exist in the project scope before that worker
  launches. Do not paste skill text into the prompt.
- For a visual or interactive product, include a purposeful concept reference
  when it clarifies the result, and make the final acceptance path test one
  coherent product rather than isolated modules.

Before finishing, submit the plan object through the Turn CLI. The CLI is the
only submission interface and writes Turn's internal handoff record. Do not
use filesystem output as a protocol. Use `TURN_CLI agent submit --kind plan --payload '<JSON_OBJECT>'`,
    replacing the placeholder with the actual single-line JSON object. Do not
    return a fenced `turn-plan` block or use provider JSON output mode. The CLI
    submission is the only valid plan handoff:
    {handoff_example}
    """

    async def _call_codex(
        self, prompt: str, cwd: str, *, agent: AgentConfig | None = None,
        stream=None, node_id=None, terminal=None, timeout=None, stall_timeout=None,
        session_callback=None, project_id=None,
    ) -> tuple[str, Usage, str | None]:
        if shutil.which(self.s.codex_binary) is None:
            return "", Usage(), None
        transport = terminal or LocalPtyTransport()
        native = isinstance(transport, LocalPtyTransport)
        result_path = prepare_result_file(cwd, node_id, "plan")
        environment = agent_environment(cwd, node_id, "plan", result_path, agent)
        if project_id is not None:
            environment["TURN_PROJECT_ID"] = str(project_id)
        prompt = f"{prompt}\n\n{result_handoff(plan=True)}"
        bypass = any("bypass" in a for a in self.s.codex_args)
        permission = agent.permission if agent else PermissionMode.WORKSPACE
        if bypass or permission == PermissionMode.FULL:
            sandbox_flags = ["--dangerously-bypass-approvals-and-sandbox"]
        elif permission == PermissionMode.ASK:
            sandbox_flags = ["-s", "workspace-write"]
        else:
            sandbox_flags = ["--approve-for-me"]
        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning = agent.reasoning.value if agent else "default"
        reasoning_flags = [] if reasoning == "default" else [
            "-c", f'model_reasoning_effort="{reasoning}"'
        ]
        session_id = agent.session_id if agent else None
        observed_session = session_id

        async def remember_session(session: str) -> None:
            nonlocal observed_session
            observed_session = session
            if session_callback is not None:
                await session_callback(session)
        if native:
            native_args = [
                a for a in self.s.codex_args
                if a not in {
                    "--skip-git-repo-check", "exec", "resume",
                } and "bypass" not in a
            ]
            if session_id:
                cmd = [
                    self.s.codex_binary, "resume", *model_flags, *reasoning_flags,
                    *sandbox_flags, "--no-alt-screen", "-C", cwd,
                    *native_args, session_id,
                ]
            else:
                cmd = [
                    self.s.codex_binary, *model_flags, *reasoning_flags,
                    *sandbox_flags, "--no-alt-screen", "-C", cwd,
                    *native_args,
                ]
        elif session_id:
            resume_permissions = ["--dangerously-bypass-approvals-and-sandbox"] if bypass else sandbox_flags
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", "resume", *model_flags,
                *reasoning_flags, *resume_permissions, "-C", cwd, session_id, prompt_arg,
            ]
        else:
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", *model_flags, *reasoning_flags,
                *sandbox_flags, "-C", cwd,
                *[a for a in self.s.codex_args if "bypass" not in a],
                prompt_arg,
            ]
        structured = ""
        try:
            result = await run_until_result(
                transport,
                node_id,
                cmd,
                cwd=cwd,
                result_path=result_path,
                stream=stream,
                timeout=timeout or self.s.default_run_timeout_seconds,
                idle_warning=self.s.terminal_idle_warning_seconds,
                idle_reap=self.s.terminal_idle_reap_seconds,
                session_callback=remember_session,
                session_marker=str(node_id),
                harness_name=agent.harness.value,
                initial_input=prompt if getattr(transport, "supports_inject", False) else (prompt if native else None),
                initial_input_mode="stdin" if (not native and getattr(transport, "supports_inject", False)) else "native",
                environment=environment,
            )
            if result.stalled:
                raise GenerationStalled(f"planner produced no output for {stall_timeout:g} seconds")
            submitted = read_result_file(result_path)
            if submitted is not None:
                structured = json.dumps(submitted)
        except asyncio.TimeoutError as error:
            raise GenerationStalled("planner exceeded the run timeout") from error
        except (FileNotFoundError, OSError):
            return "", Usage(), None
        finally:
            for temporary_path in (result_path,):
                if temporary_path is None:
                    continue
                try:
                    os.unlink(str(temporary_path))
                except OSError:
                    pass
        # Native sessions communicate their plan through the atomic Turn
        # handoff file. Their PTY bytes are deliberately never scanned for
        # JSON; the browser terminal is the only consumer of that stream.
        if native:
            return structured, Usage(), observed_session or session_id

        return structured, Usage(), observed_session or session_id


class AgentPlanner(Planner):
    """Plan with the harness configured on the planner node.

    The graph keeps one planner operation while this adapter owns provider CLI
    differences. Codex retains schema-constrained output; other installed
    harnesses receive the identical decomposition contract and are parsed by
    the same strict ``PlanResult`` boundary.
    """

    name = "agent-planner"

    def __init__(self, settings=settings):
        self.s = settings
        self.codex = CodexPlanner(settings=settings)
        self.commands = HarnessCommandFactory(
            codex_binary=settings.codex_binary,
            codex_args=settings.codex_args,
        )

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        agent = ctx.node.agent or AgentConfig(harness=HarnessKind.CODEX, type_id="planner")
        if agent.harness == HarnessKind.CODEX:
            return await self.codex.plan(ctx)
        if agent.harness not in {HarnessKind.OPENCODE, HarnessKind.PI, HarnessKind.CLAUDE}:
            raise RuntimeError(f"planner harness '{agent.harness.value}' is unsupported")
        prompt = self.codex._build_prompt(ctx)
        text = await self._call_harness(agent, prompt, ctx)
        plan = AgentPlanner._parse_plan(text, ctx.node.objective)
        if plan is not None and plan.nodes:
            plan.session_id = agent.session_id
            return plan
        raise RuntimeError("planner returned no valid turn-plan; no heuristic fallback is enabled")

    def _command(
        self,
        agent: AgentConfig,
        prompt: str,
        *,
        cwd: str | None = None,
        native: bool = False,
        resume: bool = False,
        prompt_via_stdin: bool = False,
    ) -> list[str]:
        return self.commands.planner_command(
            agent,
            prompt,
            cwd=cwd or os.getcwd(),
            native=native,
            resume=resume,
            prompt_via_stdin=prompt_via_stdin,
        )

    async def _call_harness(
        self, agent: AgentConfig, prompt: str, ctx: NodeExecutionContext
    ) -> str:
        binary = {HarnessKind.OPENCODE: "opencode", HarnessKind.PI: "pi", HarnessKind.CLAUDE: "claude"}[agent.harness]
        if shutil.which(binary) is None:
            return ""
        resume = agent.session_id is not None
        if agent.session_id is None:
            # Pi supports an exact project session id. Generate it only for a
            # new planner conversation; a Re-Run clears this field first and
            # therefore receives a genuinely fresh session.
            if agent.harness == HarnessKind.PI:
                agent.session_id = str(uuid.uuid4())
                ctx.node.agent = agent
        try:
            transport = ctx.terminal or LocalPtyTransport()
            native = isinstance(transport, LocalPtyTransport)
            cwd = ctx.repo_path or os.getcwd()
            known_opencode_sessions = (
                set(opencode_session_ids())
                if native and agent.harness == HarnessKind.OPENCODE
                else set()
            )

            async def remember_session(session: str) -> None:
                agent.session_id = session
                ctx.node.agent = agent

            async def probe_session() -> str | None:
                if agent.harness != HarnessKind.OPENCODE:
                    return None
                current = await asyncio.to_thread(opencode_session_ids)
                return next(
                    (item for item in current if item not in known_opencode_sessions),
                    None,
                )

            result_path = prepare_result_file(cwd, ctx.node.id, "plan")
            environment = agent_environment(cwd, ctx.node.id, "plan", result_path, agent)
            environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
            native_prompt = f"{prompt}\n\n{result_handoff(plan=True)}"
            if native:
                result = await run_until_result(
                    transport,
                    ctx.node.id,
                    self._command(
                        agent,
                        native_prompt,
                        cwd=cwd,
                        native=True,
                        resume=resume,
                    ),
                    cwd=cwd,
                    result_path=result_path,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                    session_callback=remember_session,
                    session_probe=probe_session if agent.harness == HarnessKind.OPENCODE else None,
                    session_marker=str(ctx.node.id),
                    harness_name=agent.harness.value,
                    initial_input=native_prompt,
                    environment=environment,
                )
            elif getattr(transport, "supports_inject", False):
                result = await run_until_result(
                    transport,
                    ctx.node.id,
                    self._command(
                        agent,
                        native_prompt,
                        cwd=cwd,
                        native=False,
                        resume=resume,
                        prompt_via_stdin=True,
                    ),
                    cwd=cwd,
                    result_path=result_path,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                    session_callback=remember_session,
                    session_marker=str(ctx.node.id),
                    harness_name=agent.harness.value,
                    initial_input=native_prompt,
                    initial_input_mode="stdin",
                    environment=environment,
                )
            else:
                result = await transport.run(
                    ctx.node.id,
                    self._command(agent, native_prompt),
                    cwd=cwd,
                    environment=environment,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    stall_timeout=ctx.stall_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                )
            if result.stalled:
                raise GenerationStalled(f"planner produced no output for {ctx.stall_timeout_seconds:g} seconds")
            submitted = read_result_file(result_path)
            text = json.dumps(submitted) if submitted is not None else ""
            if agent.harness == HarnessKind.OPENCODE and not agent.session_id:
                agent.session_id = recover_session_id(text)
                if agent.session_id:
                    ctx.node.agent = agent
            return text
        except asyncio.TimeoutError as error:
            raise GenerationStalled("planner exceeded the run timeout") from error
        except (FileNotFoundError, OSError):
            return ""

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
                agent=n.get("agent"),
                agent_type=n.get("agent_type"),
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
                skills=list(n.get("skills", [])),
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
        parent_norm = AgentPlanner._norm(parent_objective)
        only_child = len(raw_nodes) == 1
        drop = set()
        for n in raw_nodes:
            kn = n.key.strip().lower()
            on = AgentPlanner._norm(n.objective)
            if on and on == parent_norm and not only_child:
                drop.add(n.key)
            elif kn in AgentPlanner.COORDINATOR_KEYS:
                drop.add(n.key)
        if drop:
            for n in raw_nodes:
                if n.parent_key in drop:
                    n.parent_key = None  # reparent to this node

        nodes = [n for n in raw_nodes if n.key not in drop]
        index = {n.key: i for i, n in enumerate(nodes)}

        # 2) Collect dependencies from both per-node depends_on and explicit
        #    edges (domain convention: src is prerequisite, dst dependent). Then enforce a
        #    DAG by keeping only a dependency whose prerequisite appears EARLIER
        #    in the node list (the prompt asks Codex to list in execution order).
        deps: dict[str, list[str]] = {
            n.key: list(dict.fromkeys(n.depends_on)) for n in nodes
        }
        for e in data.get("edges", []):
            s, d = e.get("src"), e.get("dst")
            if s in deps and d in deps and s != d:
                if s not in deps[d]:
                    deps[d].append(s)
        for n in nodes:
            n.depends_on = [
                d for d in deps[n.key] if d in index and index[d] < index[n.key]
            ]

        architecture_spec = data.get("architecture_spec")
        parsed_architecture = (
            ArchitectureSpec.model_validate(architecture_spec)
            if architecture_spec is not None
            else None
        )
        if parsed_architecture is not None and len(nodes) > 1:
            concrete = [
                node for node in nodes
                if not node.plan and node.agent_type is not AgentType.PLANNER
            ]
            missing_skills = [node.key for node in concrete if not node.skills]
            if missing_skills:
                raise ValueError(
                    "broad plan must declare selected skills for every concrete node; "
                    f"missing: {', '.join(missing_skills)}"
                )
            if not parsed_architecture.research_sources:
                raise ValueError(
                    "broad plan must record the direct research URLs consulted in "
                    "architecture_spec.research_sources"
                )
        return PlanResult(
            nodes=nodes,
            architecture_spec=parsed_architecture,
            edges=[],
            notes=data.get("notes"),
        )
