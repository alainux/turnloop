"""Harness catalog and adapters for external coding-agent CLIs.

Harness details stop here. The graph stores only an ``AgentConfig`` and the
runner invokes the corresponding Worker protocol, so adding another harness
does not change scheduling or persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path

from turn.domain.schemas import (
    AgentConfig,
    ArtifactKind,
    ArtifactSpec,
    HarnessKind,
    InputSpec,
    Outcome,
    Usage,
    WorkerResult,
)
from turn.workers import parsing, worktree
from turn.workers.artifacts import (
    capture_worktree,
    has_material_change,
    missing_declared_files,
    requires_material_change,
)
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.terminal import LocalPtyTransport

logger = logging.getLogger("turn.harnesses")


HARNESS_CATALOG = {
    "codex": {
        "label": "Codex",
        "binary": "codex",
        "reasoning": ["default", "low", "medium", "high", "xhigh", "max"],
        "supports_sessions": True,
        "supports_tools": True,
    },
    "claude": {
        "label": "Claude Code",
        "binary": "claude",
        "reasoning": ["default", "low", "medium", "high", "xhigh", "max"],
        "supports_sessions": True,
        "supports_tools": True,
    },
    "opencode": {
        "label": "OpenCode",
        "binary": "opencode",
        "reasoning": ["default", "low", "medium", "high", "max"],
        "supports_sessions": True,
        "supports_tools": True,
    },
    "pi": {
        "label": "Pi",
        "binary": "pi",
        "reasoning": ["default", "low", "medium", "high", "xhigh", "max"],
        "supports_sessions": True,
        "supports_tools": True,
    },
}

# Ordered model-family refinements. Unknown model IDs inherit the harness
# contract so new provider releases do not require a Turn deployment.
MODEL_REASONING_PROFILES = [
    {
        "id": "non_reasoning",
        "match_any": ["embedding", "audio", "image", "vision-only"],
        "reasoning": ["default"],
    },
    {
        "id": "compact",
        "match_any": ["mini", "nano", "haiku", "flash", "small"],
        "reasoning": ["default", "low", "medium", "high"],
    },
]


def reasoning_levels_for(harness: str | HarnessKind, model: str | None = None) -> list[str]:
    """Resolve supported effort values from the central capability catalog."""
    key = harness.value if isinstance(harness, HarnessKind) else str(harness)
    base = list(HARNESS_CATALOG.get(key, {}).get("reasoning", ["default"]))
    normalized = (model or "").strip().lower()
    if normalized:
        for profile in MODEL_REASONING_PROFILES:
            # Model-family names are delimited identifiers (for example,
            # ``gpt-5-mini``). Boundary matching avoids classifying an
            # unrelated custom ID such as ``smalltalk-pro`` as ``small``.
            if any(
                re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", normalized)
                for token in profile["match_any"]
            ):
                return [level for level in profile["reasoning"] if level in base]
    return base


def validate_agent_capabilities(agent: AgentConfig) -> None:
    supported = reasoning_levels_for(agent.harness, agent.model)
    if agent.reasoning.value not in supported:
        model = agent.model or "the harness default model"
        raise ValueError(
            f"reasoning '{agent.reasoning.value}' is not supported by {agent.harness.value} model "
            f"'{model}'; choose one of: {', '.join(supported)}"
        )


def harness_capabilities() -> list[dict]:
    return [
        {
            "id": key,
            **meta,
            "models": [],
            "accepts_custom_models": True,
            "reasoning_profiles": MODEL_REASONING_PROFILES,
            "available": shutil.which(meta["binary"]) is not None,
        }
        for key, meta in HARNESS_CATALOG.items()
    ]


def _structured_prompt(ctx: NodeExecutionContext) -> str:
    task = ctx.node.generated_prompt or "Complete the objective using the available tools."
    if ctx.purpose == "verify":
        return f"""{render_context_block(ctx)}
PARENT VERIFICATION TASK:
{task}

Act as the parent agent responsible for this child result. Inspect the actual
files and git history and run focused, non-destructive checks where possible.
Do not edit files.

Finish with exactly one fenced `turn-result` JSON block:
- COMPLETE accepts the child based on concrete evidence.
- BLOCK rejects it with actionable feedback for the same child session.
- FAIL means verification itself could not be completed.
{{"outcome":"COMPLETE"|"BLOCK"|"FAIL","summary":"evidence or feedback","missing_inputs":[],"artifacts":[]}}
"""
    return f"""{render_context_block(ctx)}
OBJECTIVE:
{ctx.node.objective}

TASK:
{task}

Finish with a fenced `turn-result` JSON block:
{{"outcome":"COMPLETE"|"BLOCK"|"FAIL","summary":"...","missing_inputs":[],"artifacts":[]}}
Use BLOCK only for genuinely external human input. Continue the existing
session when reviewer feedback is supplied; preserve prior context and files.
"""


def _json_text_and_session(raw: str) -> tuple[str, str | None, Usage]:
    texts: list[str] = []
    session = None
    usage = Usage()
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        session = session or item.get("session_id") or item.get("sessionID") or item.get("thread_id")
        if item.get("type") == "session":
            session = session or item.get("id")
        candidate = item.get("result") or item.get("text") or item.get("content")
        nested = item.get("item")
        if isinstance(nested, dict):
            candidate = candidate or nested.get("text") or nested.get("content")
        if isinstance(candidate, str):
            texts.append(candidate)
        raw_usage = item.get("usage")
        message = item.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
        if not isinstance(raw_usage, dict) and isinstance(message, dict):
            raw_usage = message.get("usage")
        if isinstance(raw_usage, dict):
            raw_cost = raw_usage.get("cost_usd") or raw_usage.get("cost")
            if isinstance(raw_cost, dict):
                raw_cost = raw_cost.get("total")
            usage = Usage(
                input_tokens=int(raw_usage.get("input_tokens") or raw_usage.get("inputTokens") or raw_usage.get("input") or usage.input_tokens),
                cached_input_tokens=int(raw_usage.get("cached_input_tokens") or raw_usage.get("cache_read_input_tokens") or raw_usage.get("cacheRead") or usage.cached_input_tokens),
                output_tokens=int(raw_usage.get("output_tokens") or raw_usage.get("outputTokens") or raw_usage.get("output") or usage.output_tokens),
                cost_usd=raw_cost,
            )
    return "\n".join(texts) or raw, session, usage


def recover_session_id(raw: str) -> str | None:
    """Recover a provider session from a stored JSON-lines transcript."""
    for line in (raw or "").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        candidate = item.get("session_id") or item.get("sessionID") or item.get("thread_id")
        if item.get("type") == "session":
            candidate = candidate or item.get("id")
        if candidate:
            return str(candidate)
    return None


class CLIHarnessWorker(Worker):
    def __init__(self, harness: HarnessKind):
        self.harness = harness
        self.name = harness.value

    def _command(
        self, agent: AgentConfig, prompt: str, cwd: str, *, resume: bool = False
    ) -> list[str]:
        model = agent.model
        reasoning = agent.reasoning.value
        session = agent.session_id
        if self.harness == HarnessKind.CLAUDE:
            cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose"]
            if not session:
                raise ValueError("Claude requires a session id")
            cmd += ["--resume", session] if resume else ["--session-id", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--effort", reasoning]
            if agent.permission.value == "full":
                cmd.append("--dangerously-skip-permissions")
            elif agent.permission.value == "workspace":
                cmd += ["--permission-mode", "acceptEdits"]
            if agent.tools:
                cmd += ["--allowedTools", *agent.tools]
            cmd.append(prompt)
            return cmd
        if self.harness == HarnessKind.OPENCODE:
            cmd = ["opencode", "run", "--format", "json", "--dir", cwd]
            if session:
                cmd += ["--session", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--variant", reasoning]
            if agent.permission.value != "ask":
                cmd.append("--auto")
            return [*cmd, prompt]
        if self.harness == HarnessKind.PI:
            cmd = ["pi", "-p", "--mode", "json"]
            if session:
                cmd += ["--session", session]
            if model:
                cmd += ["--model", model]
            if reasoning != "default":
                cmd += ["--thinking", reasoning]
            if agent.tools:
                cmd += ["--tools", ",".join(agent.tools)]
            return [*cmd, prompt]
        raise ValueError(f"unsupported generic harness: {self.harness}")

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        repo = ctx.repo_path
        if not repo or not (Path(repo) / ".git").exists():
            return WorkerResult(outcome=Outcome.FAIL, summary="project repository unavailable")
        is_verification = (
            ctx.purpose == "verify"
            or bool(ctx.node.agent and ctx.node.agent.type_id == "validator")
        )
        cwd = worktree.get_or_create_worktree(
            ctx.node.id,
            ctx.node.parent_id,
            # Verification must inspect the exact child evidence. Recreating
            # the worktree here would erase it before the parent can review.
            force=not is_verification,
            repo_path=repo,
        )
        if cwd is None:
            return WorkerResult(outcome=Outcome.FAIL, summary="could not create isolated worktree")
        agent = (ctx.node.agent or AgentConfig(harness=self.harness)).model_copy(deep=True)
        try:
            validate_agent_capabilities(agent)
        except ValueError as error:
            return WorkerResult(outcome=Outcome.FAIL, summary=str(error), error=str(error))
        resume = bool(agent.session_id)
        if not agent.session_id and self.harness == HarnessKind.CLAUDE:
            agent.session_id = str(ctx.node.id)
        cmd = self._command(agent, _structured_prompt(ctx), cwd, resume=resume)
        try:
            terminal = await (ctx.terminal or LocalPtyTransport()).run(
                ctx.node.id,
                cmd,
                cwd=cwd,
                stream=ctx.stream,
                timeout=ctx.timeout_seconds,
                stall_timeout=ctx.stall_timeout_seconds,
            )
        except FileNotFoundError:
            return WorkerResult(outcome=Outcome.FAIL, summary=f"{self.name} is not installed")
        except asyncio.TimeoutError:
            return WorkerResult(outcome=Outcome.FAIL, summary=f"{self.name} exceeded the run timeout", error="execution timeout")
        raw_out = terminal.output.decode(errors="replace")
        raw_err = ""
        if terminal.stalled:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                error="stalled terminal output",
                retry_recommended=True,
                artifacts=[ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=raw_out)],
            )
        text, session, usage = _json_text_and_session(raw_out)
        data = parsing.first_result_json(text) or {}
        if terminal.returncode != 0 and not data:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} exited {terminal.returncode}",
                error=raw_err or text,
                retry_recommended=True,
                session_id=session or agent.session_id,
                usage=usage,
            )
        if not data:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} stopped without a structured result",
                error=text[-2000:] or raw_err,
                retry_recommended=True,
                session_id=session or agent.session_id,
                usage=usage,
                artifacts=[
                    ArtifactSpec(
                        kind=ArtifactKind.TEXT,
                        name="transcript",
                        content=raw_out,
                    )
                ],
            )
        try:
            outcome = Outcome(data.get("outcome", "COMPLETE"))
        except ValueError:
            outcome = Outcome.COMPLETE
        result = WorkerResult(
            outcome=outcome,
            summary=data.get("summary", text[-2000:] or f"{self.name} completed"),
            missing_inputs=[InputSpec(**i) for i in data.get("missing_inputs", [])],
            artifacts=parsing.artifact_specs(data.get("artifacts", [])),
            error=data.get("error"),
            retry_recommended=bool(data.get("retry_recommended", False)),
            session_id=session or agent.session_id,
            usage=usage,
        )
        missing_files = missing_declared_files(result.artifacts, cwd)
        if missing_files:
            result.outcome = Outcome.FAIL
            result.summary = f"{self.name} reported missing file outputs: {', '.join(missing_files)}"
            result.error = result.summary
            result.retry_recommended = True
        result.artifacts.append(ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=raw_out))
        captured = capture_worktree(cwd)
        result.artifacts.extend(captured)
        missing_material = (
            result.outcome == Outcome.COMPLETE
            and not is_verification
            and requires_material_change(ctx.node.objective, ctx.node.generated_prompt)
            and not has_material_change(captured)
        )
        if missing_material:
            result.outcome = Outcome.FAIL
            result.summary = f"{self.name} completed a file-writing objective without a material worktree change"
            result.error = result.summary
            result.retry_recommended = True
        # A parent verifier reports a decision only. It must never publish
        # incidental changes into the parent branch, regardless of provider.
        if not is_verification and not missing_files and not missing_material:
            try:
                worktree.merge_into_parent(ctx.node.id, ctx.node.parent_id, repo_path=repo)
            except Exception as exc:  # housekeeping must not erase useful output
                logger.warning("merge-up failed for %s: %s", ctx.node.id, exc)
        return result
