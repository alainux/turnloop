"""Harness catalog and adapters for external coding-agent CLIs.

Harness details stop here. The graph stores only an ``AgentConfig`` and the
runner invokes the corresponding Worker protocol, so adding another harness
does not change scheduling or persistence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from functools import lru_cache
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
from turn.contracts.dag import parse_result
from turn.config import settings
from turn.workers import parsing
from turn.workers.base import NodeExecutionContext, Worker, render_context_block
from turn.workers.terminal import LocalPtyTransport
from turn.workers.harness_catalog import (
    MODEL_DISCOVERY_COMMANDS,
    HarnessCommandFactory,
    REAL_HARNESS_CATALOG,
)
from turn.workers.interactive import (
    agent_environment,
    opencode_session_ids,
    prepare_result_file,
    read_result_file,
    result_handoff,
    run_until_result,
)

logger = logging.getLogger("turn.harnesses")


HARNESS_CATALOG = REAL_HARNESS_CATALOG.as_dict()

_CODEX_REASONING: dict[str, list[str]] = {}

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
    if key == "codex" and normalized in _CODEX_REASONING:
        return [
            level for level in _CODEX_REASONING[normalized]
            if level in base
        ] or ["default"]
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


def _codex_models(binary: str = "codex") -> list[str]:
    command = [binary, "app-server", "--listen", "stdio://"]
    requests = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "turn", "title": "Turn", "version": "0.1.0"}, "capabilities": {}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "model/list", "params": {}},
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None
    messages: queue.Queue[str] = queue.Queue()

    def read_messages() -> None:
        for line in process.stdout:
            messages.put(line)

    response: dict | None = None
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
        process.stdin.flush()
        threading.Thread(target=read_messages, daemon=True).start()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            try:
                line = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 2:
                response = message
                break
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    models: list[str] = []
    if response and isinstance(response.get("result"), dict):
        records = response["result"].get("data") or response["result"].get("models") or []
        for record in records:
            if isinstance(record, dict):
                value = record.get("id") or record.get("model") or record.get("slug")
                if isinstance(value, str):
                    models.append(value)
                    efforts = record.get("supportedReasoningEfforts") or []
                    levels = ["default"] + [
                        effort.get("reasoningEffort")
                        for effort in efforts
                        if isinstance(effort, dict)
                        and isinstance(effort.get("reasoningEffort"), str)
                    ]
                    _CODEX_REASONING[value.lower()] = list(dict.fromkeys(levels))
    return list(dict.fromkeys(models))


def _resolve_binary(binary: str) -> str | None:
    """Resolve a CLI even when the server was launched without the user's PATH."""
    explicit = Path(binary).expanduser()
    if explicit.is_file() and os.access(explicit, os.X_OK):
        return str(explicit)
    resolved = shutil.which(binary)
    if resolved:
        return resolved

    # Desktop-launched processes on macOS often omit user-local bin folders
    # from PATH. These are the standard install locations for the supported
    # agent CLIs, so discovery still works without requiring a shell restart.
    for directory in (
        Path.home() / ".local" / "bin",
        Path.home() / ".opencode" / "bin",
        Path.home() / ".cargo" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ):
        candidate = directory / binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


@lru_cache(maxsize=16)
def _discover_models(harness: str, binary: str | None = None) -> list[str]:
    """Best-effort local catalog discovery; never contacts a cloud API here."""
    configured_binary = binary or HARNESS_CATALOG.get(harness, {}).get("binary")
    if not isinstance(configured_binary, str):
        return []
    resolved_binary = _resolve_binary(configured_binary)
    if resolved_binary is None:
        return []
    if harness == "codex":
        try:
            return _codex_models(resolved_binary)
        except OSError:
            return []
    command = MODEL_DISCOVERY_COMMANDS.get(harness)
    if not command:
        return []
    command = [resolved_binary, *command[1:]]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return []
    models: list[str] = []
    for line in completed.stdout.splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if not clean or clean.lower().startswith(("provider", "model", "available")):
            continue
        columns = clean.split()
        if harness in {"pi", "opencode"}:
            # Pi and OpenCode print fixed-width tables: provider, model, then
            # context metadata. Model IDs are provider-qualified, and the
            # model column may itself contain additional slash-separated
            # segments.
            if len(columns) < 2:
                continue
            candidate = f"{columns[0]}/{columns[1]}"
        else:
            candidate = columns[0]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{1,160}", candidate):
            models.append(candidate)
    return list(dict.fromkeys(models))[:250]


def harness_capabilities(
    configured_models: dict[str, str] | None = None,
    configured_binaries: dict[str, str] | None = None,
) -> list[dict]:
    configured_models = configured_models or {}
    configured_binaries = configured_binaries or {}
    results: list[dict] = []
    for key, meta in HARNESS_CATALOG.items():
        binary = configured_binaries.get(key)
        discovered = _discover_models(key, binary)
        discovered = list(dict.fromkeys(discovered))
        configured = configured_models.get(key)
        if configured and configured not in discovered:
            discovered.insert(0, configured)
        models = [
            {
                "id": model,
                "label": model,
                "reasoning": reasoning_levels_for(key, model),
                "source": "configured" if model == configured else "harness",
            }
            for model in discovered
        ]
        results.append(
            {
                "id": key,
                **meta,
                "models": models,
                "accepts_custom_models": True,
                "reasoning_profiles": MODEL_REASONING_PROFILES,
                "available": _resolve_binary(binary or meta["binary"]) is not None,
            }
        )
    return results
def _structured_prompt(ctx: NodeExecutionContext, result_path: Path) -> str:
    task = ctx.node.generated_prompt or "Complete the objective using the available tools."
    prompt = f"""{render_context_block(ctx)}
OBJECTIVE:
{ctx.node.objective}

TASK:
{task}

Use BLOCK only for genuinely external human input. Continue the existing
session when a node is rerun; preserve prior context and files.
"""
    return f"{prompt}\n\n{result_handoff()}"


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
        part = item.get("part")
        if isinstance(part, dict):
            candidate = candidate or part.get("text") or part.get("content")
        nested = item.get("item")
        if isinstance(nested, dict):
            candidate = candidate or nested.get("text") or nested.get("content")
        if isinstance(candidate, str):
            texts.append(candidate)
        raw_usage = item.get("usage")
        if raw_usage is None and isinstance(part, dict):
            raw_usage = part.get("tokens")
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
            cache = raw_usage.get("cache") if isinstance(raw_usage.get("cache"), dict) else {}
            usage = Usage(
                input_tokens=int(raw_usage.get("input_tokens") or raw_usage.get("inputTokens") or raw_usage.get("input") or usage.input_tokens),
                cached_input_tokens=int(raw_usage.get("cached_input_tokens") or raw_usage.get("cache_read_input_tokens") or raw_usage.get("cacheRead") or cache.get("read") or usage.cached_input_tokens),
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
    def __init__(self, harness: HarnessKind, settings=settings):
        self.harness = harness
        self.name = harness.value
        self.s = settings
        self.commands = HarnessCommandFactory()

    def _command(
        self,
        agent: AgentConfig,
        prompt: str,
        cwd: str,
        *,
        resume: bool = False,
        native: bool = False,
    ) -> list[str]:
        return self.commands.worker_command(
            self.harness, agent, prompt, cwd, resume=resume, native=native
        )

    async def execute(self, ctx: NodeExecutionContext) -> WorkerResult:
        repo = ctx.repo_path
        if not repo or not Path(repo).is_dir():
            return WorkerResult(outcome=Outcome.FAIL, summary="assigned project directory unavailable")
        binary = HARNESS_CATALOG[self.harness.value]["binary"]
        if _resolve_binary(binary) is None:
            # Do not start a generic shell and hope an unavailable selected
            # harness turns into something else. The selected harness is the
            # contract, so fail before allocating a terminal.
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.harness.value} is not installed",
                error=f"selected harness '{self.harness.value}' is unavailable",
            )
        cwd = repo
        agent = (ctx.node.agent or AgentConfig(harness=self.harness)).model_copy(deep=True)
        try:
            validate_agent_capabilities(agent)
        except ValueError as error:
            return WorkerResult(outcome=Outcome.FAIL, summary=str(error), error=str(error))
        transport = ctx.terminal or LocalPtyTransport()
        native = isinstance(transport, LocalPtyTransport)
        result_path: Path
        environment: dict[str, str]
        had_session = bool(agent.session_id)
        if native:
            # Pi supports an exact caller-provided id. OpenCode persists its
            # own id, which can still be recovered from a machine stream or
            # supplied by a later reconnect implementation.
            if self.harness == HarnessKind.PI and not agent.session_id:
                agent.session_id = str(uuid.uuid4())
        result_path = prepare_result_file(cwd, ctx.node.id, "result")
        environment = agent_environment(cwd, ctx.node.id, "result", result_path, agent)
        environment["TURN_PROJECT_ID"] = str(ctx.node.project_id)
        resume = had_session
        if not agent.session_id and self.harness == HarnessKind.CLAUDE:
            agent.session_id = str(ctx.node.id)
        observed_session = agent.session_id
        live_buffer = ""

        async def stream_live(nid, chunk):
            nonlocal observed_session, live_buffer
            if not native:
                live_buffer = (live_buffer + chunk)[-128_000:]
                _, discovered, _ = _json_text_and_session(live_buffer)
                if discovered and discovered != observed_session:
                    observed_session = discovered
                    if ctx.session_callback is not None:
                        await ctx.session_callback(discovered)
            if ctx.stream is not None:
                await ctx.stream(nid, chunk)

        async def remember_session(session: str) -> None:
            nonlocal observed_session
            observed_session = session
            agent.session_id = session
            ctx.node.agent = agent
            if ctx.session_callback is not None:
                await ctx.session_callback(session)

        if ctx.session_callback is not None and observed_session:
            await ctx.session_callback(observed_session)
        known_opencode_sessions = (
            set(opencode_session_ids())
            if native and self.harness == HarnessKind.OPENCODE
            else set()
        )

        async def probe_session() -> str | None:
            if self.harness != HarnessKind.OPENCODE:
                return None
            current = await asyncio.to_thread(opencode_session_ids)
            return next((item for item in current if item not in known_opencode_sessions), None)

        prompt = _structured_prompt(ctx, result_path)
        cmd = self._command(
            agent, prompt, cwd, resume=resume, native=native
        )
        try:
            if native:
                terminal = await run_until_result(
                    transport,
                    ctx.node.id,
                    cmd,
                    cwd=cwd,
                    result_path=result_path,
                    stream=stream_live,
                    timeout=ctx.timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                    session_callback=remember_session,
                    session_probe=probe_session if self.harness == HarnessKind.OPENCODE else None,
                    initial_input=prompt,
                    environment=environment,
                )
            else:
                terminal = await transport.run(
                    ctx.node.id,
                    cmd,
                    cwd=cwd,
                    environment=environment,
                    stream=stream_live,
                    timeout=ctx.timeout_seconds,
                    stall_timeout=ctx.stall_timeout_seconds,
                    idle_warning=self.s.terminal_idle_warning_seconds,
                    idle_reap=self.s.terminal_idle_reap_seconds,
                )
        except FileNotFoundError:
            return WorkerResult(outcome=Outcome.FAIL, summary=f"{self.name} is not installed")
        except asyncio.TimeoutError:
            return WorkerResult(outcome=Outcome.FAIL, summary=f"{self.name} exceeded the run timeout", error="execution timeout")
        raw_out = terminal.output.decode(errors="replace")
        raw_err = ""
        if terminal.idle_reaped:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} terminal was reaped after being idle while detached",
                error="detached idle terminal",
                retry_recommended=False,
                session_id=observed_session or agent.session_id,
                artifacts=[ArtifactSpec(
                    kind=ArtifactKind.TEXT,
                    name="transcript",
                    content=terminal.display_output.decode(errors="replace") or raw_out,
                )],
            )
        if terminal.stalled:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} stopped producing output for {ctx.stall_timeout_seconds:g} seconds",
                error="stalled terminal output",
                retry_recommended=False,
                artifacts=[ArtifactSpec(kind=ArtifactKind.TEXT, name="transcript", content=terminal.display_output.decode(errors="replace") or raw_out)],
            )
        submitted = read_result_file(result_path)
        text = json.dumps(submitted) if submitted is not None else ""
        session = observed_session or agent.session_id
        usage = Usage()
        data = parsing.first_result_json(text) or {}
        if terminal.returncode != 0 and not data:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} exited {terminal.returncode}",
                error=raw_err or text,
                retry_recommended=False,
                session_id=session or agent.session_id,
                usage=usage,
            )
        if not data:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} stopped without a structured result",
                error=text[-2000:] or raw_err,
                retry_recommended=False,
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
            result = parse_result(data)
        except (TypeError, ValueError) as error:
            return WorkerResult(
                outcome=Outcome.FAIL,
                summary=f"{self.name} returned an invalid Turn result",
                error=str(error),
                retry_recommended=False,
                session_id=session or agent.session_id,
                usage=usage,
            )
        result.summary = parsing.clean_summary(result.summary)
        result.session_id = session or agent.session_id
        result.usage = usage
        display_out = (
            terminal.output.decode(errors="replace")
            if native
            else terminal.display_output.decode(errors="replace")
        )
        result.artifacts.append(
            ArtifactSpec(
                kind=ArtifactKind.TEXT,
                name="transcript",
                content=display_out or text,
            )
        )
        return result
