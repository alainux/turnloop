"""Codex worker — the software-engineering / general agent worker.

Shells out to `codex exec`, isolates software work in a Git worktree, and parses
the agent's structured result. This adapter is the *only* place Codex concepts
enter Turn; the data model stays clean.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
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
    Usage,
)
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.artifacts import (
    capture_worktree,
    has_material_change,
    missing_declared_files,
    requires_material_change,
)
from turn.workers import codex_schemas
from turn.workers import parsing
from turn.workers import worktree
from turn.workers.terminal import LocalPtyTransport

logger = logging.getLogger("turn.worker.codex")


class CodexWorker(Worker):
    name = "codex"

    def __init__(self, settings=settings):
        self.s = settings

    # -- public ----------------------------------------------------------

    async def execute(self, ctx: "NodeExecutionContext") -> WorkerResult:
        # Every node runs inside its project's own git repository: the root
        # node's worktree IS the project repo root, and non-root nodes get an
        # isolated worktree branched from their parent. There is no shared
        # "main" repository to fall back to.
        repo = ctx.repo_path
        is_verification = (
            ctx.purpose == "verify"
            or bool(ctx.node.agent and ctx.node.agent.type_id == "validator")
        )
        is_git = bool(repo) and (Path(repo) / ".git").exists()
        worktree_path = (
            worktree.get_or_create_worktree(
                ctx.node.id,
                ctx.node.parent_id,
                # Execution retries intentionally restart from the parent's
                # current branch. Parent verification is read-only and must
                # inspect the exact committed child worktree; forcing here
                # would delete the evidence immediately before reviewing it.
                force=not is_verification,
                repo_path=repo,
            )
            if is_git
            else None
        )
        # Safety: we MUST run in the project's own isolated repo/worktree.
        # Refusing here is far better than letting Codex run somewhere undefined
        # (e.g. the Turn app directory).
        if not repo or worktree_path is None:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary="no project repository configured; refusing to run Codex",
                retry_recommended=False,
            )
        cwd = worktree_path or os.getcwd()
        # Run inside the isolated worktree, and keep Codex pointed AT that
        # worktree (never the main repo) so it cannot rewrite files outside
        # the isolation boundary.
        prompt = self._build_prompt(ctx, cwd=cwd)

        schema_path = codex_schemas.write_schema(codex_schemas.RESULT_SCHEMA)
        result_file = tempfile.NamedTemporaryFile(prefix="turn-result-", suffix=".json", delete=False)
        result_path = result_file.name
        result_file.close()

        agent = ctx.node.agent
        permission = agent.permission if agent else PermissionMode.WORKSPACE
        configured_bypass = any("bypass" in a for a in self.s.codex_args)
        bypass = configured_bypass or permission == PermissionMode.FULL
        if bypass:
            permission_flags = ["--dangerously-bypass-approvals-and-sandbox"]
        elif permission == PermissionMode.ASK:
            permission_flags = ["-s", "workspace-write"]
        else:
            permission_flags = ["-s", "workspace-write", "--approve-for-me"]

        model = agent.model if agent and agent.model else self.s.codex_model
        model_flags = ["-m", model] if model else []
        reasoning_flags = []
        if agent and agent.reasoning != ReasoningLevel.DEFAULT:
            reasoning_flags = ["-c", f'model_reasoning_effort="{agent.reasoning.value}"']
        session_id = ctx.node.agent.session_id if ctx.node.agent else None
        if session_id:
            # Continue the same conversation after review feedback. Resume has
            # a deliberately smaller flag surface than a fresh exec.
            resume_permissions = ["--dangerously-bypass-approvals-and-sandbox"] if bypass else []
            cmd = [
                self.s.codex_binary, "exec", "resume", *model_flags, *reasoning_flags,
                *resume_permissions, "--output-schema", schema_path, "--json",
                "--output-last-message", result_path, session_id, prompt,
            ]
        else:
            cmd = [
                self.s.codex_binary, "exec", *model_flags, *reasoning_flags, *permission_flags,
                "--output-schema", schema_path, "--output-last-message", result_path,
                "--json", "-C", cwd,
                *[a for a in self.s.codex_args if "bypass" not in a], prompt,
            ]

        structured_text = ""
        try:
            terminal = await (ctx.terminal or LocalPtyTransport()).run(
                ctx.node.id,
                cmd,
                cwd=cwd,
                stream=ctx.stream,
                timeout=ctx.timeout_seconds or self.s.default_run_timeout_seconds,
                stall_timeout=ctx.stall_timeout_seconds,
            )
            if terminal.stalled:
                return WorkerResult(
                    outcome=Outcome.FAIL,
                    summary=f"Codex stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                    error="stalled terminal output",
                    retry_recommended=True,
                    artifacts=[ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=terminal.display_output.decode(errors="replace"))],
                )
            try:
                structured_text = Path(result_path).read_text().strip()
            except OSError:
                pass
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
            for temporary_path in (schema_path, result_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

        raw_stdout = terminal.output.decode(errors="replace")

        text, discovered_session, usage = self._decode_json_stream(raw_stdout)
        result = self._parse_result(structured_text or text)
        result.session_id = discovered_session or session_id
        result.usage = usage
        if worktree_path:
            missing_files = missing_declared_files(result.artifacts, worktree_path)
            if missing_files:
                result.outcome = Outcome.FAIL
                result.summary = f"codex reported missing file outputs: {', '.join(missing_files)}"
                result.error = result.summary
                result.retry_recommended = True
            captured = self._capture_worktree(worktree_path)
            result.artifacts.extend(captured)
            missing_material = (
                result.outcome == Outcome.COMPLETE
                and not is_verification
                and requires_material_change(ctx.node.objective, ctx.node.generated_prompt)
                and not has_material_change(captured)
            )
            if missing_material:
                result.outcome = Outcome.FAIL
                result.summary = "codex completed a file-writing objective without a material worktree change"
                result.error = result.summary
                result.retry_recommended = True
            # Merge this node's produced files up into its parent's worktree so
            # downstream nodes (e.g. an assembler) find them on disk instead of
            # having to regenerate them from context.
            if not is_verification and not missing_files and not missing_material:
                try:
                    worktree.merge_into_parent(
                        ctx.node.id, ctx.node.parent_id, repo_path=repo
                    )
                except Exception as e:  # never let housekeeping fail a node
                    logger.warning("worktree merge-up failed for %s: %s", ctx.node.id, e)
        # Preserve the unaltered PTY stream as a durable artifact after the
        # live terminal transport has closed.
        transcript = terminal.display_output.decode(errors="replace")
        if transcript.strip():
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=transcript)
            )
        else:
            result.artifacts.append(
                ArtifactSpec(kind=ArtifactKind.TEXT, name="codex-output", content=text[:8000])
            )
        return result

    @staticmethod
    def _decode_json_stream(raw: str) -> tuple[str, str | None, Usage]:
        """Extract final agent text, resumable thread id, and token usage.

        Older Codex builds may still print plain text; that remains a supported
        fallback so the adapter is version-tolerant.
        """
        messages: list[str] = []
        session_id = None
        usage = Usage()
        parsed_any = False
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            parsed_any = True
            if event.get("type") == "thread.started":
                session_id = event.get("thread_id") or event.get("threadId")
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") in ("agent_message", "message"):
                body = item.get("text") or item.get("content")
                if isinstance(body, str):
                    messages.append(body)
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = Usage(
                    input_tokens=int(raw_usage.get("input_tokens") or 0),
                    cached_input_tokens=int(raw_usage.get("cached_input_tokens") or 0),
                    output_tokens=int(raw_usage.get("output_tokens") or 0),
                    cost_usd=raw_usage.get("cost_usd"),
                )
        return ("\n".join(messages) if parsed_any and messages else raw), session_id, usage

    # -- prompt ----------------------------------------------------------

    def _build_prompt(self, ctx: NodeExecutionContext, cwd=None) -> str:
        gp = ctx.node.generated_prompt or "Complete the objective above using the available tools."
        # If we execute inside an isolated worktree, rewrite any mention of the
        # project repository path so Codex operates on the worktree, not the
        # source tree it was branched from.
        repo = ctx.repo_path
        if cwd and repo and repo != cwd:
            gp = gp.replace(repo, cwd)
        if ctx.purpose == "verify":
            return f"""{render_context_block(ctx)}
PARENT VERIFICATION TASK:
{gp}

Act as the parent agent responsible for this child result. Inspect the actual
merged files, run focused checks where possible, and compare the result with
the child's objective and acceptance constraints. Do not edit files.

Return exactly one fenced `turn-result` JSON block:
- COMPLETE means the evidence is sufficient and the parent accepts the child.
- BLOCK means the parent rejects it; `summary` MUST be actionable feedback for
  the same child agent to correct in its existing session and worktree.
- FAIL is reserved for an inability to perform verification itself.
{{"outcome":"COMPLETE"|"BLOCK"|"FAIL","summary":"evidence or feedback","missing_inputs":[]}}
"""
        return f"""{render_context_block(ctx)}
OBJECTIVE:
{ctx.node.objective}

TASK:
{gp}

When you finish, append a fenced code block labeled `turn-result` containing JSON:
{{"outcome": "COMPLETE"|"BLOCK"|"FAIL", "summary": "...", "missing_inputs": [{{"id":"...","label":"...","kind":"text|decision|credential|account|approval|file"}}]}}

If the task is too broad to complete directly, set "outcome": "EXPAND" and ALSO append a
separate fenced block labeled `turn-plan` containing JSON:
{{"nodes":[{{"key":"a","objective":"...","executor":"codex"}}], "edges":[]}}

Never invent facts you do not have. However, do NOT block merely because a
prerequisite step's output is not pasted into this prompt: a "depends_on" edge
means that step already ran first and its artifacts are part of your available
context (read them from the worktree or the provided context blocks).
Only return outcome "BLOCK" with explicit missing_inputs when a genuine
EXTERNAL gate is missing — a credential, account, approval, or a file the human
must supply — something no automated step or your own tools can produce. If you
can proceed using the objective, the provided context, and your tools, do so
and return "COMPLETE".
"""

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
                retry_recommended=True,
            )

        data = result_json
        outcome = Outcome(data.get("outcome", "COMPLETE"))
        children = self._to_plan(plan_json) if (outcome == Outcome.EXPAND and plan_json) else None

        return WorkerResult(
            outcome=outcome,
            summary=data.get("summary", text[:500]),
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

    # -- git worktree ----------------------------------------------------

    def _capture_worktree(self, worktree: str) -> list[ArtifactSpec]:
        return capture_worktree(worktree)
