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
    ArtifactSpec,
    DocumentRef,
    AgentType,
    EdgeSpec,
    EdgeType,
    HarnessKind,
    InputKind,
    InputSpec,
    MCPServerAccess,
    NodeSpec,
    PlanResult,
    Resource,
    Usage,
)
from turn.workers.base import NodeExecutionContext, Planner, render_context_block
from turn.workers.harnesses import recover_session_id
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.mcp.runtime import codex_mcp_overrides
from turn.skills.library import SKILLS, install_builtin_skill
from turn.workers.interactive import (
    agent_environment,
    opencode_session_ids,
    prepare_result_file,
    read_codex_session_usage,
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
        # This deterministic test fixture has no shell/tool loop. Mirror the
        # real planner's library-install action so its synthetic plans obey
        # the same project-local skill contract.
        if ctx.repo_path:
            for skill_id in SKILLS:
                install_builtin_skill(skill_id, ctx.repo_path)
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
            edges=edges,
            notes="Deterministic test-only decomposition (no LLM planner available).",
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
            forbidden_session_id=ctx.forbidden_session_id,
        )
        plan = AgentPlanner._parse_plan(text, ctx.node.objective)
        if plan is not None and plan.nodes:
            self._validate_setup_scope(ctx, plan)
            plan.usage = usage
            plan.session_id = session_id
            return plan
        raise RuntimeError("planner returned no valid turn-plan; no heuristic fallback is enabled")

    @staticmethod
    def _validate_setup_scope(ctx: NodeExecutionContext, plan: PlanResult) -> None:
        """Reject a narrow root plan that contradicts explicit broad scope."""
        node = ctx.node
        if node.parent_id is not None:
            return
        intent = " ".join((node.objective, node.generated_prompt or "")).lower()
        broad_markers = (
            "app factory",
            "organization-scale",
            "organization scale",
            "entire organization",
            "multi-organization",
            "multiple organizations",
            "multiple products",
            "multiple teams",
            "multi-team",
            "enterprise",
            "platform",
            "ecosystem",
        )
        if not any(marker in intent for marker in broad_markers):
            return
        if len(plan.nodes) != 1:
            return
        only_node = plan.nodes[0]
        is_nested_planner = (
            only_node.plan
            or only_node.executor == "planner"
            or only_node.agent_type is AgentType.PLANNER
        )
        if not is_nested_planner:
            raise RuntimeError(
                "root planner collapsed an explicitly organization-scale request "
                "to one leaf; return a broad first-level graph or a nested planner"
            )

    def _build_prompt(self, ctx: NodeExecutionContext) -> str:
        handoff_example = plan_handoff_example()
        initial_setup = ctx.node.parent_id is None
        planning_role = "setup planner" if initial_setup else "scoped planner"
        setup_guidance = """
SETUP — this is the project-root setup planner:
- Set up the board by interpreting the user's actual request. Identify the
  requested outcome, domain, users, constraints, runtime or delivery form,
  quality bar, and explicit scope before choosing work.
- Classify the requested scale from the user's explicit words before applying
  any minimization heuristic. Explicit scope and scale are binding. "Smallest
  complete" means the smallest complete topology that preserves the stated
  outcome and scale; it never means the smallest interpretation of the
  request.
- Choose the shape that fits after that classification: it may be one
  focused worker, a lean MVP or demo, a book-writing workflow, a routine
  automation, a broad product or system, an organization-scale platform, or
  something else entirely.
- An app factory is organization-scale by definition: it is a repeatable
  organization/system for producing multiple applications, not one app and
  not a research assignment. Treat the same way any request that explicitly
  names an organization, platform, ecosystem, enterprise, multiple products,
  or multiple teams. Do not collapse that scope into one research, design, or
  implementation child. Use broad first-level ownership and nested planners
  for domains that need their own evolving subtree. Research may support one
  domain, but it must never substitute for the organization-scale setup.
- When the request does not state organization-scale scope, do not invent it.
  For a broad product or system, research, design, engineering, verification,
  integration, launch or adoption, and operations stages may be useful, but
  add only stages the prompt, risk, and delivery goal justify. This is a
  decision, not a mandatory pipeline. Keep explicitly small work small.
- Create only the direct agent nodes, real dependencies, selected skills, and
  ownership boundaries that the chosen setup needs. The board is the handoff
  to the next planners and workers.
- Project name: if this root project has no user-provided name, choose a concise
  navigation name and return it in the top-level PlanResult field
  `project_name`. If the project already has a name, preserve it and do not
  replace it. This name is project metadata, not a future document or artifact.
- Use `find-skills` to search for narrow domain guidance for chosen agents. For
  web or app architecture, procure a stack- and runtime-specific architecture
  skill only when an architecture stage is warranted. Do not assume a generic
  architecture skill is needed, and do not invent a placeholder skill.
- Use `find-mcps` to research external tools and data sources when a worker
  materially needs one. Put the researched access object in that worker's
  `mcp_servers` array, including its source website and native transport
  configuration when known. Use `transport: "configured"` for a server the
  user already manages in the selected harness. Never claim credentials,
  local applications, or server processes are installed.
- Fit MCP procurement to the target worker's declared harness capabilities:
  first name the capability the worker needs, then subtract the capabilities
  already provided by that harness. Do not assign an MCP merely to duplicate a
  harness capability such as browser or computer use. Procure an MCP when it
  fills a missing capability, provides a specialized service or data source,
  or is explicitly requested by the user. The harness profile is a declared
  adapter view for now and may later be replaced by active capability discovery.
- The setup plan must not know, name, reserve, or register future documents.
  Requirements for outputs belong only in the assigned skill and worker
  prompt. The worker that creates a file submits its actual artifact.
- Stop at each nested planner: never invent, replace, or edit its future
  descendants. The next planner owns its own replacement subtree. Do not edit
  sibling or later-stage graph content.
""" if initial_setup else """
SCOPED PLANNING — this node is owned by a parent planner:
- This is not setup. Do not recreate the project-wide setup or treat yourself
  as the board owner; work only inside this planner's boundary. The setup
  planner already established the surrounding board.
- Plan only this node's assigned domain and direct children. Read upstream
  outputs and preserve their contracts, but do not edit ancestor-owned edges,
  sibling stages, or later stages owned by another planner.
- Return a complete replacement subtree for this planning boundary when
  revising it; never reach across a nested planner boundary.
"""
        document_guidance = """
DOCUMENTS AND ARTIFACTS — root setup boundary:
- Define topology, ownership, dependencies, and skill assignment only.
- Do not name, reserve, or register files that a later agent may create.
- Requirements for concrete deliverables belong in the selected skill and
  assigned worker prompt. A worker submits an artifact only after creating it.
""" if initial_setup else """
DOCUMENTS AND ARTIFACTS — scoped planning boundary:
- Use upstream artifacts and references as available context.
- Do not reserve outputs for descendant workers. If this planner creates a
  file during this planning turn, submit that actual file as an artifact.
- Requirements for descendant deliverables belong in their skills and prompts.
"""
        return f"""{render_context_block(ctx)}
THIS NODE'S OBJECTIVE:
{ctx.node.objective}

PLANNING INSTRUCTIONS FOR THIS NODE:
{ctx.node.generated_prompt or "No additional planning instructions."}

You are the {planning_role} decomposing THIS node into its direct children. Produce
a complete workgraph that divides all of the actual labor required to
accomplish the user's objective, using the smallest number of meaningful
responsibilities. This is an orchestration effort, not a mere abstraction
exercise or chronological checklist.

{setup_guidance}

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
  the resulting acceptance boundary in the project document.
- The final integrator must make the complete requested result runnable and
  usable, not merely prove that isolated modules or a demo shell exist.

{document_guidance}

VISUAL REFERENCES:
- For visual or spatial work, use the image-generation skill when a concept
  reference would reduce ambiguity. Submit any image you actually create as a
  normal file artifact and use image embeds or links from the current output;
  do not reserve future visual files.

First preserve the requested outcome. State what must be runnable, usable,
readable, or otherwise deliverable at the end. For software, account for the
concrete runtime/host, launch command, user interaction loop, and a complete
end-to-end acceptance scenario. A collection of contracts, mocks, services,
or tests is not a finished application unless the user explicitly requested
those things.

DECOMPOSITION POLICY — match the structure to the objective:
- SCOPE PRESERVATION: explicit scale words such as "app factory",
  "organization", "platform", "ecosystem", "enterprise", "multiple
  products", or "multiple teams" outrank the default preference for a small
  plan. Preserve that breadth in the first-level graph; do not represent it
  with a lone research or discovery leaf.
- ATOMIC step: if the objective is a SINGLE concrete step (it says "one", "a
single", "just one", "the next step", "first step", or names exactly one
action), produce EXACTLY ONE child that performs it. Do NOT pad with
investigate / plan / verify scaffolding.
- BROAD container: if the objective is a wide effort (e.g. "build X",
"create a game", "implement the system", "plan the project"), identify the
genuinely distinct domains, modules, capabilities, or output sections that
make up the result. Prefer 2–5 orthogonal direct children, then use
`depends_on` to express their actual information flow. Name them in the
vocabulary of the domain; do not turn milestones, tests, or generic approval
steps into fake domains.
- INFORMATION-FLOW AUDIT: for every child, ask what it must know or consume in
  order to make a sound decision or produce its deliverable. A child may omit
  `depends_on` only when it can work from the user request and stable existing
  interfaces without another child's decisions, files, or contracts. When a
  product direction, research result, design, requirements decision, or other
  upstream output determines the scope of later engineering or delivery work,
  add that real prerequisite. For a broad software product this often means
  discovery and product/design work precede architecture or implementation
  planning, and launch work follows the product direction; apply the same rule
  using the domain's equivalent handoffs for books, automations, and other
  work. This is an information dependency, not a mandatory pipeline: keep
  genuinely independent research or domains parallel, and do not invent stages
  just to create a sequence.
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

TOPOLOGY — arrange the children to express the information and delivery flow:
- PARALLEL ONLY WHEN INDEPENDENT: children may omit depends_on only when they
  can create their deliverables without reading another child's decisions,
  files, or contracts. Parallelism is useful, but it is never a substitute for
  a real prerequisite.
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
  end-to-end outcome, the development planning boundary must add exactly one
  final integrator to produce that result. It must depend on every direct branch
  output, including nested planner/container branches. At the setup boundary,
  do not add a root-level integrator merely to recombine stages that already
  have responsible nested planners. Multiple terminal outputs are appropriate
  only when the objective explicitly requests multiple products or independent
  outputs.
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
  and "executor": "planner". At the setup boundary, use nested planners when
  a broad domain needs its own evolving subtree and ownership boundary. In a
  narrower plan, use a nested planner only for a genuinely huge or uncertain
  scope whose architecture cannot responsibly be decided yet. Do not add a
  planner at every bifurcation.
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
- SKILLS: before submitting, run `turn skills show find-skills` and investigate
  the concrete work. Search for the narrowest useful guidance for each
  executor, integrator, and verifier. You own procurement: for every selected
  built-in skill, inspect it with `turn skills show <id>` and copy it into this
  project with `turn skills install <id>`; for every external skill, use your
  available shell/browser/download tools to place its files under
  `.turn/skills/<slug>/` and reference it as `project:<slug>`. Never submit a
  URL as a skill reference. If no useful skill exists, do not create a project skill merely to carry the assignment; leave the additional skills array empty.
  Project skills are only for reusable domain or method guidance that applies
  beyond this one project prompt; they must never contain the user's request,
  this node's objective or generated_prompt, the graph, acceptance criteria,
  or a copy of work instructions. If genuinely reusable project-specific
  guidance is needed, author `.turn/skills/<slug>/SKILL.md` with YAML `name`
  and `description` frontmatter and reference it as `project:<slug>`. Do not
  paste skill bodies into prompts. Role-base skills are attached automatically
  from the selected agent type, but you still own installing those library
  files into this project before submission. A node may have an empty
  additional `skills` array when investigation finds no material addition.
  Record sources actually consulted in the relevant project document when one
  is appropriate. Scoped planners have
  `turn-planning`, `imagegen`, `find-skills`, and `find-mcps`; the root setup
  planner has `turn-planning`, `find-skills`, `find-mcps`, and `turn-setup`.
  Domain, architecture, QA,
  and product skills are selected for workers or scoped planners by this
  planning process.
- VERIFICATION: almost every concrete executor or integrator should be
  followed by a verifier when its output has meaningful code, visual, runtime,
  or contract risk. A verifier is an ordinary sibling at this planning
  boundary: set `agent_type` to `verifier`, omit `parent_key`, and put every
  implementation or integration key it checks in `depends_on`. A verifier may
  have multiple dependencies. Prefer an integrator before a verifier when
  several branches need one cohesive user-facing review, but treat that as a
  planning recommendation rather than a validation rule. This is the only
  graph relationship for verification. A verifier must receive explicit
  criteria in its prompt and inspect real evidence before approving. If it
  rejects a multi-dependency graph, set `target_node_id` to the specific
  earlier node needing correction. Rejection feedback is a runtime Herdr
  conversation with the selected return target; it is not additional graph
  data.
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
- Before submission, actually run `turn skills show find-skills`, `turn
  skills show find-mcps`, and `turn skills install <id>` for every selected
  built-in skill. Inspect the
  repository and live graph, and perform the relevant web/search or skills.sh
  queries. Do not claim a source you did not consult.
- For broad engineering work, investigate the domain before choosing nodes.
  `turn skills show turn-architecture-research` is an optional architecture
  reference to inspect when it fits; it is not automatically assigned to this
  planner. A game plan that only names an engine and a story is incomplete
  unless the request genuinely has no other product boundary.
- Record each direct URL consulted in the project document when research is
  part of the plan. Do not add research metadata to the graph payload.
- MCP CHECK: when a node has a non-empty `mcp_servers` array, include a
  `source_url` for every entry and record the selected server's source and
  user-owned setup requirements in the relevant project document. Use only
  the smallest set of servers needed by that node. A plan that names an MCP
  without a source URL or a concrete reason is incomplete.
- Give every concrete executor, integrator, and verifier a non-empty `skills`
  array when the investigation found a material project-specific addition;
  the server supplies the role base assignment, while you install its library
  file into the project.
- For every skill in the planner's own configuration and every node's
  `skills` or `agent.skill_ids`, make sure the corresponding file exists in
  this project's `.turn/skills` directory before submitting. Verify each one
  with a filesystem check. For each selected skill, make the node prompt state
  the contract it improves. Do not paste skill text into the prompt, and do not turn the node
  prompt or project request into a skill. Agents already receive their node
  objective and assignment directly and can inspect the live graph and
  predecessor outputs through the Turn context.
- For a visual or interactive product, include a purposeful concept reference
  when it clarifies the result, and make the final acceptance path test one
  coherent product rather than isolated modules.

Before finishing, submit the plan object through the Turn CLI. The CLI is the
only submission interface and writes Turn's internal handoff record. Do not
use filesystem output as a protocol or type `TURN_CLI` as a command. Use the
installed `turn` command with stdin so apostrophes and other shell characters
inside JSON remain safe:
turn agent submit --kind plan --stdin <<'TURN_PAYLOAD'
{handoff_example}
TURN_PAYLOAD
Replace the example with the actual single-line JSON object. Do not return a
fenced `turn-plan` block or use provider JSON output mode. The CLI submission
is the only valid plan handoff.
    """

    async def _call_codex(
        self, prompt: str, cwd: str, *, agent: AgentConfig | None = None,
        stream=None, node_id=None, terminal=None, timeout=None, stall_timeout=None,
        session_callback=None, project_id=None,
        forbidden_session_id: str | None = None,
    ) -> tuple[str, Usage, str | None]:
        if shutil.which(self.s.codex_binary) is None:
            return "", Usage(), None
        transport = terminal or LocalPtyTransport()
        # HerdrPtyTransport is an interactive PTY transport backed by a
        # durable pane. Injection is only the delivery mechanism; it must not
        # switch Codex from its native TUI to JSON/exec output.
        native = isinstance(transport, LocalPtyTransport)
        result_path = prepare_result_file(cwd, node_id, "plan")
        environment = agent_environment(cwd, node_id, "plan", result_path, agent)
        if project_id is not None:
            environment["TURN_PROJECT_ID"] = str(project_id)
        prompt = f"{prompt}\n\n{result_handoff(plan=True)}"
        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning = agent.reasoning.value if agent else "default"
        reasoning_flags = [] if reasoning == "default" else [
            "-c", f'model_reasoning_effort="{reasoning}"'
        ]
        mcp_flags = [
            item
            for override in codex_mcp_overrides(agent or AgentConfig(harness=HarnessKind.CODEX))
            for item in ("-c", override)
        ]
        session_id = agent.session_id if agent else None
        observed_session = session_id

        async def remember_session(session: str) -> None:
            nonlocal observed_session
            if forbidden_session_id and session == forbidden_session_id:
                raise RuntimeError("provider reused the previous session during a fresh run")
            observed_session = session
            if session_callback is not None:
                await session_callback(session)
        if native:
            if session_id:
                cmd = [
                    self.s.codex_binary, "resume", *model_flags, *reasoning_flags,
                    *mcp_flags, "--no-alt-screen", "-C", cwd, session_id, prompt,
                ]
            else:
                cmd = [
                    self.s.codex_binary, *model_flags, *reasoning_flags,
                    *mcp_flags, "--no-alt-screen", "-C", cwd, prompt,
                ]
        elif session_id:
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", "resume", *model_flags,
                *reasoning_flags, *mcp_flags, "-C", cwd, session_id, prompt_arg,
            ]
        else:
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", *model_flags, *reasoning_flags,
                *mcp_flags, "-C", cwd,
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
                excluded_session_ids={forbidden_session_id}
                if forbidden_session_id
                else None,
                # This path always launches the Codex planner. The selected
                # project harness is inherited by its planned leaves and is
                # not the foreground process we are waiting for here.
                harness_name=self.s.codex_binary,
                initial_input=prompt
                if getattr(transport, "supports_inject", False) and not native
                else None,
                initial_input_mode="stdin" if getattr(transport, "supports_inject", False) else "native",
                environment=environment,
            )
            if result.stalled:
                raise GenerationStalled(f"planner produced no output for {stall_timeout:g} seconds")
            submitted = read_result_file(result_path)
            if submitted is not None:
                structured = json.dumps(submitted)
            usage = read_codex_session_usage(observed_session or session_id)
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
        return structured, usage, observed_session or session_id


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
            CodexPlanner._validate_setup_scope(ctx, plan)
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
        mcp_config: str | None = None,
    ) -> list[str]:
        return self.commands.planner_command(
            agent,
            prompt,
            cwd=cwd or os.getcwd(),
            native=native,
            resume=resume,
            prompt_via_stdin=prompt_via_stdin,
            mcp_config=mcp_config,
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
                if ctx.forbidden_session_id and session == ctx.forbidden_session_id:
                    raise RuntimeError("provider reused the previous session during a fresh run")
                agent.session_id = session
                ctx.node.agent = agent
                if ctx.session_callback is not None:
                    await ctx.session_callback(session)

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
                        mcp_config=environment.get("TURN_AGENT_MCP_CONFIG"),
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
                        mcp_config=environment.get("TURN_AGENT_MCP_CONFIG"),
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
                    self._command(
                        agent,
                        native_prompt,
                        cwd=cwd,
                        mcp_config=environment.get("TURN_AGENT_MCP_CONFIG"),
                    ),
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

        def document_refs(values):
            return [
                item
                if isinstance(item, DocumentRef)
                else DocumentRef(ref=item)
                if isinstance(item, str)
                else DocumentRef.model_validate(item)
                for item in (values or [])
            ]

        def artifact_specs(values):
            specs = []
            for item in values or []:
                if isinstance(item, str):
                    specs.append(
                        ArtifactSpec(
                            kind="file",
                            name=item.rsplit("/", 1)[-1] or item,
                            ref=item,
                        )
                    )
                else:
                    specs.append(ArtifactSpec.model_validate(item))
            return specs

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
                document_refs=document_refs(n.get("document_refs")),
                artifacts=artifact_specs(n.get("artifacts")),
                skills=list(n.get("skills", [])),
                mcp_servers=[MCPServerAccess.model_validate(item) for item in n.get("mcp_servers", [])],
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

        # 2) Collect dependencies from both per-node depends_on and explicit
        #    edges (domain convention: src is prerequisite, dst dependent).
        #    Declaration order is presentation, not semantics. Preserve a
        #    dependency even when the model listed its prerequisite later; the
        #    PlanResult validator owns missing-reference and cycle errors.
        deps: dict[str, list[str]] = {
            n.key: list(dict.fromkeys(n.depends_on)) for n in nodes
        }
        for e in data.get("edges", []):
            s, d = e.get("src"), e.get("dst")
            if s in deps and d in deps and s != d:
                if s not in deps[d]:
                    deps[d].append(s)
        for n in nodes:
            n.depends_on = list(dict.fromkeys(deps[n.key]))

        return PlanResult(
            nodes=nodes,
            project_name=data.get("project_name"),
            document_refs=document_refs(data.get("document_refs")),
            artifacts=artifact_specs(data.get("artifacts")),
            edges=[],
            notes=data.get("notes"),
        )
