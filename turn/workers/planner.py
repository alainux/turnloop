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
from turn.capabilities.catalog import CapabilityCatalog
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
    NodeSpec,
    PlanResult,
    Resource,
    SubgraphRef,
    Usage,
    NODE_OBJECTIVE_MAX_LENGTH,
    concise_node_title,
)
from turn.workers.base import NodeExecutionContext, Planner, render_context_block
from turn.workers.harnesses import recover_session_id
from turn.workers.harness_catalog import HarnessCommandFactory
from turn.workers.interactive import (
    agent_environment,
    opencode_session_ids,
    prepare_result_file,
    read_codex_session_usage,
    read_result_file,
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

    def __init__(self, default_executor: str = "codex", settings=settings):
        self.default_executor = default_executor
        self.s = settings

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        # This deterministic test fixture has no shell/tool loop. Mirror the
        # real planner's catalog load action so its synthetic plans obey the
        # same project-local capability contract.
        if ctx.repo_path:
            catalog = CapabilityCatalog(Path(self.s.data_dir) / "capabilities")
            for entry in catalog.list():
                catalog.load_into_project(entry.id, ctx.repo_path)
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
                follows=["core", "inputs", "outputs"],
            ),
        ]
        edges = [
            EdgeSpec(type=EdgeType.FOLLOWS, src="core", dst="integrate"),
            EdgeSpec(type=EdgeType.FOLLOWS, src="inputs", dst="integrate"),
            EdgeSpec(type=EdgeType.FOLLOWS, src="outputs", dst="integrate"),
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
        if plan is not None:
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
        """Send only node data; planning behavior lives in turn-planning."""
        instructions = ctx.node.generated_prompt or ctx.node.objective
        return "\n".join([
            render_context_block(ctx),
            f"objective={ctx.node.objective}",
            f"instructions={instructions}",
        ])

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
        environment = agent_environment(
            cwd, node_id, "plan", result_path, agent, data_dir=self.s.data_dir
        )
        if project_id is not None:
            environment["TURN_PROJECT_ID"] = str(project_id)
        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning = agent.reasoning.value if agent else "default"
        reasoning_flags = [] if reasoning == "default" else [
            "-c", f'model_reasoning_effort="{reasoning}"'
        ]
        mcp_flags = [
            item
            for override in json.loads(environment.get("TURN_AGENT_CODEX_MCP_OVERRIDES", "[]"))
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
        if plan is not None:
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
        skill_paths: list[str] | None = None,
    ) -> list[str]:
        return self.commands.planner_command(
            agent,
            prompt,
            cwd=cwd or os.getcwd(),
            native=native,
            resume=resume,
            prompt_via_stdin=prompt_via_stdin,
            mcp_config=mcp_config,
            skill_paths=skill_paths,
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
            environment = agent_environment(
                cwd, ctx.node.id, "plan", result_path, agent, data_dir=self.s.data_dir
            )
            environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
            native_prompt = prompt
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
                        skill_paths=[item for item in environment.get("TURN_AGENT_SKILLS", "").split(",") if item],
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
                        skill_paths=[item for item in environment.get("TURN_AGENT_SKILLS", "").split(",") if item],
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
                        skill_paths=[item for item in environment.get("TURN_AGENT_SKILLS", "").split(",") if item],
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
        for index, node in enumerate(data.get("nodes", [])):
            if isinstance(node, dict) and ("skills" in node or "mcp_servers" in node):
                raise ValueError(
                    f"plan node {index} uses removed skills/mcp_servers fields; use capabilities"
                )
            if isinstance(node, dict) and "depends_on" in node:
                raise ValueError(
                    f"plan node {index} uses removed depends_on; use immediate follows stages"
                )

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

        def subgraph_refs(values):
            if values is None:
                return []
            if isinstance(values, str):
                values = [values]
            return [
                item
                if isinstance(item, SubgraphRef)
                else SubgraphRef(ref=item)
                if isinstance(item, str)
                else SubgraphRef.model_validate(item)
                for item in values
            ]

        def objective(value):
            return concise_node_title(value)

        def generated_prompt(node):
            raw = node["objective"]
            return node.get("generated_prompt") or (
                raw if len(raw) > NODE_OBJECTIVE_MAX_LENGTH else None
            )

        raw_nodes = [
            NodeSpec(
                key=n["key"],
                objective=objective(n["objective"]),
                generated_prompt=generated_prompt(n),
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
                subgraph_refs=subgraph_refs(
                    n.get("subgraph_refs", n.get("graph_file"))
                ),
                artifacts=artifact_specs(n.get("artifacts")),
                capabilities=list(n.get("capabilities", [])),
                parent_key=n.get("parent_key"),
                follows=list(n.get("follows", [])),
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

        # 2) Canonicalize the two structural relationships. Declaration order
        #    is presentation, not semantics. CONTAINS becomes parent_key and
        #    FOLLOWS becomes an immediate predecessor in follows.
        follows: dict[str, list[str]] = {
            n.key: list(dict.fromkeys(n.follows)) for n in nodes
        }
        by_key = {node.key: node for node in nodes}
        for index, raw_edge in enumerate(data.get("edges", [])):
            if not isinstance(raw_edge, dict):
                raise ValueError(f"plan edge {index} must be an object")
            edge = EdgeSpec.model_validate(
                {**raw_edge, "type": raw_edge.get("type", EdgeType.FOLLOWS.value)}
            )
            if edge.src not in by_key or edge.dst not in by_key:
                missing = edge.src if edge.src not in by_key else edge.dst
                raise ValueError(f"unknown edge key: {missing}")
            if edge.type is EdgeType.CONTAINS:
                child = by_key[edge.dst]
                if child.parent_key not in (None, edge.src):
                    raise ValueError(
                        f"node {edge.dst} has conflicting composition parents"
                    )
                child.parent_key = edge.src
            elif edge.src not in follows[edge.dst]:
                follows[edge.dst].append(edge.src)
        for n in nodes:
            n.follows = list(dict.fromkeys(follows[n.key]))

        return PlanResult(
            nodes=nodes,
            project_name=data.get("project_name"),
            document_refs=document_refs(data.get("document_refs")),
            subgraph_refs=subgraph_refs(
                data.get("subgraph_refs", data.get("graph_file"))
            ),
            artifacts=artifact_specs(data.get("artifacts")),
            edges=[],
            notes=data.get("notes"),
        )
