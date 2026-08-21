"""Codex worker — the software-engineering / general agent worker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from turn.config import settings
from turn.contracts.dag import parse_plan, parse_result, parse_verification
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
from turn.workers.base import InvalidSubmission, NodeExecutionContext, Worker, render_context_block
from turn.workers.interactive import (
    agent_environment,
    format_verification_result,
    prepare_result_file,
    read_codex_session_usage,
    read_result_file,
    read_submission_file,
    run_until_result,
)
from turn.workers import parsing
from turn.workers.terminal import LocalPtyTransport
from turn.workers.harness_catalog import codex_project_root_flags
from turn.metrics import emit_jsonl_telemetry
from turn.workers.native_telemetry import (
    codex_notify_flags,
    emit_telemetry_status,
    prepare_codex_notify_telemetry,
    schedule_late_sidecar_collection,
)

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
        agent = ctx.node.agent
        # HerdrPtyTransport intentionally subclasses LocalPtyTransport: both
        # expose a real interactive PTY. Telemetry is a passive side channel,
        # never a reason to replace Codex's native terminal UI.
        native = isinstance(transport, LocalPtyTransport)
        verification = bool(agent and agent.type_id is AgentType.VERIFIER)
        protocol_kind = "verification" if verification else "result"
        control_root = ctx.project_repo_path or cwd
        result_path = prepare_result_file(control_root, ctx.node.id, protocol_kind)
        machine_output_path = result_path.with_suffix(".jsonl") if not native else None
        environment = agent_environment(
            cwd,
            ctx.node.id,
            protocol_kind,
            result_path,
            agent,
            data_dir=self.s.data_dir,
            project_repo_path=ctx.project_repo_path,
            run_id=ctx.run_id,
        )
        environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
        sidecar = prepare_codex_notify_telemetry(cwd, ctx.node.id) if native else None
        if sidecar is not None:
            environment.update(sidecar.environment)
        prompt = self._build_prompt(
            ctx,
            cwd=cwd,
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
        telemetry_flags = codex_notify_flags(cwd) if sidecar is not None else []
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
                    *codex_project_root_flags(cwd), *mcp_flags, *telemetry_flags,
                    "--no-alt-screen", "-C", cwd, session_id, prompt,
                ]
            else:
                cmd = [
                    self.s.codex_binary, *model_flags, *reasoning_flags,
                    *codex_project_root_flags(cwd), *mcp_flags, *telemetry_flags,
                    "--no-alt-screen", "-C", cwd, prompt,
                ]
        elif session_id:
            # Continue the same conversation when the runner resumes a node.
            # Resume has a deliberately smaller flag surface than a fresh exec.
            # JSON execution is an automatic machine-transport boundary. Supplying
            # the prompt as an argument means it never depends on PTY input or
            # an EOF event that a terminal-control stream cannot prove it
            # delivered to Codex.
            prompt_arg = prompt
            cmd = [
                self.s.codex_binary, "exec", "--json", "resume", *model_flags, *reasoning_flags,
                *codex_project_root_flags(cwd), *mcp_flags, "-C", cwd, session_id, prompt_arg,
            ]
        else:
            prompt_arg = prompt
            cmd = [
                self.s.codex_binary, "exec", "--json", *model_flags, *reasoning_flags,
                *codex_project_root_flags(cwd), *mcp_flags, "-C", cwd, prompt_arg,
            ]

        # Both sources below are documented structured channels, never
        # terminal output: Codex notify rollouts for native sessions and
        # ``exec --json`` for headless machine transports.
        live_machine_events = bool(
            (sidecar is not None or machine_output_path is not None)
            and getattr(transport, "supports_inject", False)
        )
        telemetry_records = 0
        late_sidecar_scheduled = False

        async def emit_machine_event(line: str) -> None:
            nonlocal telemetry_records
            telemetry_records += 1
            if telemetry_records == 1 and sidecar is not None:
                await emit_telemetry_status(
                    ctx.telemetry, harness="codex", source=sidecar.source,
                    status="connected", detail="Codex notify delivered structured rollout events.",
                )
            await emit_jsonl_telemetry("codex", line, ctx.telemetry)

        structured_text = ""
        try:
            if sidecar is not None:
                await emit_telemetry_status(
                    ctx.telemetry, harness="codex", source=sidecar.source,
                    status="ready", detail=sidecar.detail,
                )
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
                    known_session_id=session_id,
                    session_marker=str(ctx.node.id),
                    excluded_session_ids={ctx.forbidden_session_id}
                    if ctx.forbidden_session_id
                    else None,
                    harness_name=self.s.codex_binary,
                    environment=environment,
                    machine_output_path=sidecar.path if sidecar else None,
                    machine_output_handler=emit_machine_event if sidecar else None,
                )
            elif getattr(transport, "supports_inject", False):
                structured_kwargs = {
                    "keep_attached": False,
                    "machine_output_path": machine_output_path,
                    "machine_output_handler": emit_machine_event if live_machine_events else None,
                    "capture_machine_output": True,
                }
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
                    known_session_id=session_id,
                    session_marker=str(ctx.node.id),
                    excluded_session_ids={ctx.forbidden_session_id}
                    if ctx.forbidden_session_id
                    else None,
                    harness_name=self.s.codex_binary,
                    environment=environment,
                    # `codex exec --json` is a bounded process, unlike the
                    # native TUI.  Wait for its exit marker after the Turn
                    # handoff so the structured output is complete before it
                    # is normalized.
                    **structured_kwargs,
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
            if machine_output_path is not None and not live_machine_events:
                raw_output = (
                    machine_output_path.read_text(encoding="utf-8", errors="replace")
                    if machine_output_path.exists()
                    else terminal.output.decode(errors="replace")
                )
                await emit_jsonl_telemetry("codex", raw_output, ctx.telemetry)
            if sidecar is not None and telemetry_records == 0:
                # Codex emits its documented ``notify`` callback after the
                # model turn closes, while an agent handoff occurs inside that
                # turn. Keep observing the attempt-scoped sidecar in the
                # background instead of delaying the node's completion.
                late_sidecar_scheduled = True
                schedule_late_sidecar_collection(
                    sidecar,
                    emit_machine_event,
                    emit=ctx.telemetry,
                    harness="codex",
                    unavailable_detail="Codex completed without a notify rollout; lifecycle evidence remains available but tool metrics are unavailable for this run.",
                )
            # The accepted Turn handoff is semantic authority. Read it before
            # interpreting a provider exit, stall, or detached-idle marker;
            # native harnesses can submit and then exit non-zero while
            # flushing their UI/process.
            submitted_present, submitted = read_submission_file(result_path)
            if submitted_present and submitted is None:
                raise InvalidSubmission("Codex returned an invalid JSON submission")
            if submitted is not None:
                structured_text = json.dumps(submitted)
            elif terminal.idle_reaped:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary="Codex terminal was reaped after being idle while detached",
                    error="detached idle terminal",
                    retry_recommended=False,
                    session_id=discovered_session,
                )
            if submitted is None and terminal.stalled:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary=f"Codex stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                    error="stalled terminal output",
                    retry_recommended=False,
                )
            if submitted is None and terminal.returncode != 0:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary=f"Codex exited {terminal.returncode}",
                    error=f"Codex exited with code {terminal.returncode}",
                    retry_recommended=False,
                    session_id=discovered_session or session_id,
                )
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
            for temporary_path in (
                result_path,
                machine_output_path,
                sidecar.path if sidecar and not late_sidecar_scheduled else None,
            ):
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
                decision = parse_verification(json.loads(structured_text))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise InvalidSubmission(f"Codex verifier returned an invalid verification: {error}") from error
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
                decision = parse_verification(json.loads(structured_text))
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
    ) -> str:
        """Send the assignment envelope; operational rules live in skills."""
        gp = ctx.node.generated_prompt or "Complete the objective above using the available tools."
        # The prompt and worker both point at the same assigned project path.
        repo = ctx.repo_path
        if cwd and repo and repo != cwd:
            gp = gp.replace(repo, cwd)
        correction = (
            ctx.node.agent_message
            if ctx.node.agent_state == "correction_required"
            else None
        )
        acceptance_contract = self._acceptance_evidence_prompt(ctx)
        return "\n".join([
            render_context_block(ctx),
            f"objective={ctx.node.objective}",
            f"instructions={gp}",
            *([acceptance_contract] if acceptance_contract else []),
            *([f"correction={correction}"] if correction else []),
        ])

    @staticmethod
    def _acceptance_evidence_prompt(ctx: "NodeExecutionContext") -> str:
        """Make the typed completion contract visible at the launch boundary.

        The skill explains the workflow, but the node's actual criterion ids
        only exist in the graph. Include those ids in the provider prompt so a
        real harness can produce a complete ``WorkerResult`` on its first
        handoff instead of relying on a prose summary or generic artifact list.
        """
        if not ctx.node.acceptance_criteria:
            return ""
        criteria = json.dumps(
            [
                {"id": criterion.id, "description": criterion.description}
                for criterion in ctx.node.acceptance_criteria
            ],
            ensure_ascii=False,
        )
        return "\n".join([
            "TURN_ACCEPTANCE_EVIDENCE",
            f"criteria={criteria}",
            "Before outcome COMPLETE, exercise every criterion and include one evidence item per criterion in the turn-result JSON.",
            'Evidence item shape: {"criterion_id":"<exact id>","status":"PASS","summary":"what was checked and observed","refs":["repo-relative/path"]}.',
            "Use PASS only for checks you actually ran; use FAIL or UNVERIFIED when the criterion is not proven. Generic artifacts or a prose summary do not replace criterion-level evidence.",
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
            raise InvalidSubmission(f"codex returned an invalid Turn result: {error}") from error
        result.summary = parsing.clean_summary(result.summary)
        return result

    @staticmethod
    def _to_plan(plan_json: dict) -> PlanResult:
        return parse_plan(plan_json)
