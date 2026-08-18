"""Codex worker — the software-engineering / general agent worker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from turn.config import settings
from turn.contracts.dag import parse_result
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    ArtifactKind,
    ArtifactSpec,
    DocumentRef,
    EdgeType,
    EdgeSpec,
    InputKind,
    InputSpec,
    NodeSpec,
    Outcome,
    PlanResult,
    ReasoningLevel,
    VerificationResult,
    WorkerResult,
)
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.interactive import (
    agent_environment,
    format_verification_result,
    prepare_result_file,
    read_codex_session_usage,
    read_result_file,
    run_until_result,
)
from turn.workers import parsing
from turn.workers.terminal import LocalPtyTransport

logger = logging.getLogger("turn.worker.codex")


class CodexWorker(Worker):
    name = "codex"

    def __init__(self, settings=settings):
        self.s = settings

    # -- public ----------------------------------------------------------

    async def execute(self, ctx: "NodeExecutionContext") -> WorkerResult:
        repo = ctx.repo_path
        if not repo or not Path(repo).is_dir():
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="assigned project directory is unavailable; refusing to run Codex",
                retry_recommended=False,
            )
        cwd = repo
        transport = ctx.terminal or LocalPtyTransport()
        # HerdrPtyTransport intentionally subclasses LocalPtyTransport: both
        # expose a real interactive PTY. Injection is only how Turn reaches
        # that PTY; it must not switch Codex to JSON/exec output.
        native = isinstance(transport, LocalPtyTransport)
        agent = ctx.node.agent
        verification = bool(agent and agent.type_id is AgentType.VERIFIER)
        protocol_kind = "verification" if verification else "result"
        result_path = prepare_result_file(cwd, ctx.node.id, protocol_kind)
        environment = agent_environment(
            cwd, ctx.node.id, protocol_kind, result_path, agent, data_dir=self.s.data_dir
        )
        environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
        prompt = self._build_prompt(
            ctx,
            cwd=cwd,
            result_path=result_path,
            verification=verification,
        )

        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning_flags = []
        if agent and agent.reasoning != ReasoningLevel.DEFAULT:
            reasoning_flags = ["-c", f'model_reasoning_effort="{agent.reasoning.value}"']
        mcp_flags = [
            item
            for override in json.loads(environment.get("TURN_AGENT_CODEX_MCP_OVERRIDES", "[]"))
            for item in ("-c", override)
        ]
        session_id = ctx.node.agent.session_id if ctx.node.agent else None
        discovered_session = session_id

        async def remember_session(session: str) -> None:
            nonlocal discovered_session
            discovered_session = session
            if ctx.session_callback is not None:
                await ctx.session_callback(session)
        if native:
            # `codex` without a subcommand is the native interactive TUI. Its
            # positional prompt starts the first turn without a PTY race; the
            # JSONL `exec` subcommand is kept only for machine transports.
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
            # Continue the same conversation when the runner resumes a node.
            # Resume has a deliberately smaller flag surface than a fresh exec.
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", "resume", *model_flags, *reasoning_flags,
                *mcp_flags, "-C", cwd, session_id, prompt_arg,
            ]
        else:
            prompt_arg = "-" if getattr(transport, "supports_inject", False) else prompt
            cmd = [
                self.s.codex_binary, "exec", *model_flags, *reasoning_flags,
                *mcp_flags, "-C", cwd, prompt_arg,
            ]

        structured_text = ""
        try:
            if native:
                terminal = await run_until_result(
                    transport,
                    ctx.node.id,
                    cmd,
                    cwd=cwd,
                    result_path=result_path,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                    session_callback=remember_session,
                    session_marker=str(ctx.node.id),
                    excluded_session_ids={ctx.forbidden_session_id}
                    if ctx.forbidden_session_id
                    else None,
                    harness_name=self.s.codex_binary,
                    environment=environment,
                )
            elif getattr(transport, "supports_inject", False):
                terminal = await run_until_result(
                    transport,
                    ctx.node.id,
                    cmd,
                    cwd=cwd,
                    result_path=result_path,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                    session_callback=remember_session,
                    session_marker=str(ctx.node.id),
                    excluded_session_ids={ctx.forbidden_session_id}
                    if ctx.forbidden_session_id
                    else None,
                    harness_name=self.s.codex_binary,
                    initial_input=prompt,
                    initial_input_mode="stdin" if not native else "native",
                    environment=environment,
                )
            else:
                terminal = await transport.run(
                    ctx.node.id,
                    cmd,
                    cwd=cwd,
                    environment=environment,
                    stream=ctx.stream,
                    timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                    stall_timeout=ctx.stall_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                )
            if terminal.idle_reaped:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="Codex terminal was reaped after being idle while detached",
                    error="detached idle terminal",
                    retry_recommended=False,
                    session_id=discovered_session,
                )
            if terminal.stalled:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary=f"Codex stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                    error="stalled terminal output",
                    retry_recommended=False,
                )
            submitted = read_result_file(result_path)
            if submitted is not None:
                structured_text = json.dumps(submitted)
            usage = read_codex_session_usage(discovered_session or session_id)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="Codex exceeded the run timeout",
                error="execution timeout",
                retry_recommended=False,
            )
        except FileNotFoundError:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="codex not found",
                error=f"codex binary '{self.s.codex_binary}' is not available",
                retry_recommended=False,
            )
        finally:
            for temporary_path in (result_path,):
                if temporary_path is None:
                    continue
                try:
                    os.unlink(str(temporary_path))
                except OSError:
                    pass

        if verification:
            if not structured_text:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="Codex verifier stopped without a Turn decision",
                    error="missing verification submission",
                    retry_recommended=False,
                    session_id=discovered_session or session_id,
                )
            try:
                decision = VerificationResult.model_validate(json.loads(structured_text))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="Codex verifier returned an invalid verification",
                    error=str(error),
                    retry_recommended=False,
                    session_id=discovered_session or session_id,
                )
            return WorkerResult(
                outcome=Outcome.COMPLETE,
                summary=decision.summary,
                verification=decision,
                session_id=discovered_session or session_id,
                usage=usage,
                artifacts=[ArtifactSpec(
                    kind=ArtifactKind.TEXT,
                    name="verification-result",
                    content=format_verification_result(decision),
                )],
            )

        # A non-verifier can still use the shared review CLI when its work
        # discovers a defect in another node. It writes the decision through
        # the ordinary result handoff path; normal result payloads do not
        # contain a decision, so this remains unambiguous.
        if structured_text:
            try:
                decision = VerificationResult.model_validate(json.loads(structured_text))
            except (TypeError, ValueError, json.JSONDecodeError):
                decision = None
            if decision is not None:
                return WorkerResult(
                    outcome=Outcome.COMPLETE,
                    summary=decision.summary,
                    verification=decision,
                    session_id=discovered_session or session_id,
                    usage=usage,
                    artifacts=[ArtifactSpec(
                        kind=ArtifactKind.TEXT,
                        name="verification-result",
                        content=format_verification_result(decision),
                    )],
                )

        if not structured_text:
            result = self._parse_result("")
            result.session_id = discovered_session or session_id
            return result

        text = structured_text
        result = self._parse_result(text)
        result.session_id = discovered_session or session_id
        result.usage = usage
        result.artifacts.insert(
            0,
            ArtifactSpec(
                kind=ArtifactKind.JSON,
                name="result-submission",
                content=json.loads(text),
            ),
        )
        return result

    # -- prompt ----------------------------------------------------------

    def _build_prompt(
        self,
        ctx: NodeExecutionContext,
        cwd=None,
        result_path: Path | None = None,
        verification: bool = False,
    ) -> str:
        gp = ctx.node.generated_prompt or "Complete the objective above using the available tools."
        # The prompt and worker both point at the same assigned project path.
        repo = ctx.repo_path
        if cwd and repo and repo != cwd:
            gp = gp.replace(repo, cwd)
        return "\n".join([
            render_context_block(ctx),
            f"objective={ctx.node.objective}",
            f"instructions={gp}",
        ])

    # -- parsing ---------------------------------------------------------

    def _parse_result(self, text: str) -> WorkerResult:
        fences = parsing.extract_fences(text)
        result_json = parsing.first_result_json(text)
        plan_json = parsing.first_plan_json(text)

        if result_json is None and plan_json is not None:
            result_json = {"outcome": "EXPAND"}

        if result_json is None:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="codex stopped without a structured result",
                error=text[-2000:] or "Codex returned no turn-result block",
                retry_recommended=False,
            )

        data = dict(result_json)
        if plan_json is not None and data.get("outcome") == Outcome.EXPAND.value:
            data.setdefault("children", plan_json)
        try:
            result = parse_result(data)
        except (TypeError, ValueError) as error:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="codex returned an invalid Turn result",
                error=str(error),
                retry_recommended=False,
            )
        result.summary = parsing.clean_summary(result.summary)
        return result

    @staticmethod
    def _to_plan(plan_json: dict) -> PlanResult:
        nodes = [
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
                parent_key=n.get("parent_key"),
                depends_on=list(n.get("depends_on", [])),
                plan=bool(n.get("plan", False)),
            )
            for n in plan_json.get("nodes", [])
        ]
        edges = [
            EdgeSpec(type=EdgeType(e["type"]), src=e["src"], dst=e["dst"])
            for e in plan_json.get("edges", [])
        ]
        return PlanResult(
            nodes=nodes,
            document_refs=[
                item if isinstance(item, DocumentRef) else DocumentRef(ref=item)
                if isinstance(item, str) else DocumentRef.model_validate(item)
                for item in plan_json.get("document_refs", [])
            ],
            artifacts=[
                item if isinstance(item, ArtifactSpec) else ArtifactSpec(
                    kind=ArtifactKind.FILE,
                    name=item.rsplit("/", 1)[-1] or item,
                    ref=item,
                ) if isinstance(item, str) else ArtifactSpec.model_validate(item)
                for item in plan_json.get("artifacts", [])
            ],
            edges=edges,
            notes=plan_json.get("notes"),
        )
