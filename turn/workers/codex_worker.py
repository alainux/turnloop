"""Codex worker — the software-engineering / general agent worker."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from turn.config import settings
from turn.domain.schemas import (
    ArtifactKind,
    ArtifactSpec,
    EdgeType,
    EdgeSpec,
    InputKind,
    InputSpec,
    NodeSpec,
    Outcome,
    PlanResult,
    PermissionMode,
    ReasoningLevel,
    WorkerResult,
)
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.artifacts import (
    capture_filesystem,
    has_material_change,
    missing_declared_files,
    requires_material_change,
    snapshot_filesystem,
)
from turn.workers.interactive import (
    agent_environment,
    prepare_result_file,
    read_result_file,
    result_handoff,
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
        before = snapshot_filesystem(cwd)
        transport = ctx.terminal or LocalPtyTransport()
        native = isinstance(transport, LocalPtyTransport)
        agent = ctx.node.agent
        result_path = prepare_result_file(cwd, ctx.node.id, "result")
        environment = agent_environment(cwd, ctx.node.id, "result", result_path, agent)
        environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
        prompt = self._build_prompt(ctx, cwd=cwd, result_path=result_path)

        permission = agent.permission if agent else PermissionMode.WORKSPACE
        configured_bypass = any("bypass" in a for a in self.s.codex_args)
        bypass = configured_bypass or permission == PermissionMode.FULL
        if bypass:
            permission_flags = ["--dangerously-bypass-approvals-and-sandbox"]
        elif permission == PermissionMode.ASK:
            permission_flags = ["-s", "workspace-write"]
        else:
            # The current Codex CLI treats --approve-for-me as the
            # workspace-write approval mode; combining it with -s is rejected.
            permission_flags = ["--approve-for-me"]

        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning_flags = []
        if agent and agent.reasoning != ReasoningLevel.DEFAULT:
            reasoning_flags = ["-c", f'model_reasoning_effort="{agent.reasoning.value}"']
        session_id = ctx.node.agent.session_id if ctx.node.agent else None
        discovered_session = session_id

        async def remember_session(session: str) -> None:
            nonlocal discovered_session
            discovered_session = session
            if ctx.session_callback is not None:
                await ctx.session_callback(session)
        if native:
            # `codex` without a subcommand is the native interactive TUI. The
            # JSONL `exec` subcommand is intentionally kept only for injected
            # test transports and non-interactive compatibility adapters.
            native_args = [
                a for a in self.s.codex_args
                if a not in {
                    "--skip-git-repo-check", "exec", "resume",
                } and "bypass" not in a
            ]
            if session_id:
                cmd = [
                    self.s.codex_binary, "resume", *model_flags, *reasoning_flags,
                    *permission_flags, "--no-alt-screen", "-C", cwd,
                    *native_args, session_id,
                ]
            else:
                cmd = [
                    self.s.codex_binary, *model_flags, *reasoning_flags,
                    *permission_flags, "--no-alt-screen", "-C", cwd,
                    *native_args,
                ]
        elif session_id:
            # Continue the same conversation after review feedback. Resume has
            # a deliberately smaller flag surface than a fresh exec.
            resume_permissions = ["--dangerously-bypass-approvals-and-sandbox"] if bypass else permission_flags
            cmd = [
                self.s.codex_binary, "exec", "resume", *model_flags, *reasoning_flags,
                *resume_permissions, "-C", cwd, session_id, prompt,
            ]
        else:
            cmd = [
                self.s.codex_binary, "exec", *model_flags, *reasoning_flags, *permission_flags,
                "-C", cwd, *[a for a in self.s.codex_args if "bypass" not in a], prompt,
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
                    initial_input=prompt,
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
                    artifacts=[ArtifactSpec(
                        kind=ArtifactKind.TEXT,
                        name="transcript",
                        content=terminal.display_output.decode(errors="replace"),
                    )],
                )
            if terminal.stalled:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary=f"Codex stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                    error="stalled terminal output",
                    retry_recommended=False,
                    artifacts=[ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=terminal.display_output.decode(errors="replace"))],
                )
            submitted = read_result_file(result_path)
            if submitted is not None:
                structured_text = json.dumps(submitted)
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

        text = structured_text
        result = self._parse_result(text)
        result.session_id = discovered_session or session_id
        if cwd:
            missing_files = missing_declared_files(result.artifacts, cwd)
            if missing_files:
                result.outcome = Outcome.FAIL
                result.summary = f"codex reported missing file outputs: {', '.join(missing_files)}"
                result.error = result.summary
                result.retry_recommended = True
            # Filesystem inspection remains an execution invariant, but it is
            # never an artifact source. The agent's CLI submission is the
            # single authority for the small artifact list shown in Turn.
            captured = self._capture_filesystem(cwd, before)
            missing_material = (
                result.outcome == Outcome.COMPLETE
                and requires_material_change(ctx.node.objective, ctx.node.generated_prompt)
                and not has_material_change(captured)
            )
            if missing_material:
                result.outcome = Outcome.FAIL
                result.summary = "codex completed a file-writing objective without a material filesystem change"
                result.error = result.summary
                result.retry_recommended = True
        # Preserve the unaltered PTY stream as a durable artifact after the
        # live terminal transport has closed.
        transcript = (
            terminal.output.decode(errors="replace")
            if native
            else terminal.display_output.decode(errors="replace")
        )
        if transcript.strip():
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)
            )
        else:
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="codex-output", content=text[:8000])
            )
        return result

    # -- prompt ----------------------------------------------------------

    def _build_prompt(
        self,
        ctx: NodeExecutionContext,
        cwd=None,
        result_path: Path | None = None,
    ) -> str:
        gp = ctx.node.generated_prompt or "Complete the objective above using the available tools."
        # The prompt and worker both point at the same assigned project path.
        repo = ctx.repo_path
        if cwd and repo and repo != cwd:
            gp = gp.replace(repo, cwd)
        prompt = f"""{render_context_block(ctx)}
OBJECTIVE:
{ctx.node.objective}

TASK:
{gp}

When you finish, submit the result through `TURN_CLI agent submit --payload '<JSON_OBJECT>'`,
replacing the placeholder with the actual single-line JSON object. The CLI is
the only submission interface; do not use filesystem output as a protocol.
Include a small `artifacts` array containing repo-relative files or directories
that represent the work.
If the task is too broad to complete directly, use outcome `EXPAND` and put
the child plan in that same submitted document. Do not print a fenced result
block and do not use provider JSON-output mode.

Never invent facts you do not have. However, do NOT block merely because a
prior stage's output is not pasted into this prompt: a "depends_on" edge means
that stage already ran first and its artifacts are part of your available
context (read them from the project directory or the provided context blocks).
Only return outcome "BLOCK" with explicit missing_inputs when a genuine
EXTERNAL gate is missing — a credential, account, approval, or a file the human
must supply — something no automated step or your own tools can produce. If you
can proceed using the objective, the provided context, and your tools, do so
and return "COMPLETE".
"""
        return f"{prompt}\n\n{result_handoff()}" if result_path else prompt

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

        data = result_json
        outcome = Outcome(data.get("outcome", "COMPLETE"))
        children = self._to_plan(plan_json) if (outcome == Outcome.EXPAND and plan_json) else None

        return WorkerResult(
            outcome=outcome,
            summary=parsing.clean_summary(data.get("summary", text[:500])),
            artifacts=parsing.artifact_specs(data.get("artifacts", [])),
            children=children,
            missing_inputs=[
                InputSpec(
                    id=i["id"],
                    label=i.get("label", i["id"]),
                    kind=parsing.safe_input_kind(i.get("kind")),
                    description=i.get("description"),
                )
                for i in data.get("missing_inputs", [])
            ],
            error=data.get("error"),
            retry_recommended=bool(data.get("retry_recommended", False)),
        )

    @staticmethod
    def _to_plan(plan_json: dict) -> PlanResult:
        nodes = [
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
            )
            for n in plan_json.get("nodes", [])
        ]
        edges = [
            EdgeSpec(type=EdgeType(e["type"]), src=e["src"], dst=e["dst"])
            for e in plan_json.get("edges", [])
        ]
        return PlanResult(nodes=nodes, edges=edges, notes=plan_json.get("notes"))

    def _capture_filesystem(self, path: str, before: dict[str, str]) -> list[ArtifactSpec]:
        return capture_filesystem(path, before)
