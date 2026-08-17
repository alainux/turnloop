"""Helpers for native, file-completing interactive harness sessions."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
import uuid
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Awaitable, Callable

from turn.contracts.dag import plan_handoff_example
from turn.domain.schemas import Usage, VerificationResult
from turn.mcp.runtime import prepare_runtime
from turn.workers.terminal import TerminalResult
from turn.skills.library import resolve_skill_paths


def format_verification_result(result: VerificationResult) -> str:
    """Render the exact submitted verification payload for human inspection.

    The PTY transcript is an execution trace, not the verifier's result.  It
    contains prompts, terminal control sequences, and provider UI chrome, so
    it must not be used as the verification artifact shown in the inspector.
    """
    return json.dumps(
        result.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def _new_codex_session_id(
    cwd: str,
    started_at: float,
    session_marker: str | None = None,
    excluded_session_ids: set[str] | None = None,
) -> str | None:
    """Find the session file Codex creates for a newly launched TUI.

    Codex does not print its conversation id in the native screen, but it
    writes a small metadata record before the first turn. Observing that
    local file keeps the lifecycle code provider-neutral while still making
    fresh sessions resumable after an idle reap.
    """
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    root = codex_home / "sessions"
    try:
        candidates = sorted(
            root.rglob("rollout-*.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except (FileNotFoundError, OSError):
        return None
    target = os.path.realpath(cwd)
    for path in candidates:
        try:
            if path.stat().st_mtime < started_at - 2:
                break
            with path.open() as handle:
                first = json.loads(handle.readline())
                payload = first.get("payload") if isinstance(first, dict) else None
                if not isinstance(payload, dict):
                    continue
                if os.path.realpath(str(payload.get("cwd") or "")) != target:
                    continue
                if session_marker:
                    # Concurrent native workers share a cwd.  mtime alone is
                    # not an identity signal because Codex touches another
                    # session's rollout while it is active.  The worker puts
                    # a unique node marker in its first user prompt; only
                    # accept the rollout whose early transcript contains it.
                    found_marker = any(session_marker in line for line in handle)
                    if not found_marker:
                        continue
                identifier = payload.get("session_id")
            if (
                isinstance(identifier, str)
                and identifier
                and identifier not in (excluded_session_ids or set())
            ):
                return identifier
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def read_codex_session_usage(session_id: str | None) -> Usage:
    """Read the latest per-turn usage from a native Codex session record."""
    if not session_id:
        return Usage()
    codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    root = codex_home / "sessions"
    try:
        candidates = sorted(
            root.rglob(f"rollout-*-{session_id}.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except (FileNotFoundError, OSError):
        return Usage()
    for path in candidates:
        latest: dict[str, Any] | None = None
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    payload = record.get("payload") if isinstance(record, dict) else None
                    if not isinstance(payload, dict) or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    per_turn = info.get("last_token_usage")
                    total = info.get("total_token_usage")
                    candidate = per_turn if isinstance(per_turn, dict) else total
                    if isinstance(candidate, dict):
                        latest = candidate
        except (OSError, UnicodeDecodeError):
            continue
        if latest is not None:
            return Usage(
                input_tokens=int(latest.get("input_tokens") or 0),
                cached_input_tokens=int(latest.get("cached_input_tokens") or 0),
                output_tokens=int(latest.get("output_tokens") or 0),
            )
    return Usage()


def prepare_result_file(cwd: str, node_id: uuid.UUID, kind: str) -> Path:
    """Return the generated handoff path for one assigned project/node."""
    path = Path(cwd) / ".turn" / "interactive" / f"{node_id}.{kind}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


def agent_environment(
    cwd: str, node_id: uuid.UUID, kind: str, handoff: Path, agent: Any | None = None
) -> dict[str, str]:
    """Build the small, harness-neutral control-plane environment.

    The ``TURN_AGENT_*`` values are launch metadata, not a second terminal
    protocol. They let a harness adapter or a project-local wrapper configure
    skills, tools, and MCP servers without inspecting terminal output.
    """
    skill_ids = list(dict.fromkeys(getattr(agent, "skill_ids", None) or []))
    scoped_skills = (
        resolve_skill_paths(skill_ids, cwd, allow_library=kind == "plan")
        if skill_ids
        else {}
    )
    mcp_runtime = prepare_runtime(cwd, node_id, agent)

    def csv(name: str) -> str:
        return ",".join(
            str(getattr(value, "name", value))
            for value in (getattr(agent, name, None) or [])
        )

    # These paths are private server-owned protocol locations. Agents receive
    # the metadata only so the Turn CLI can publish into the control plane;
    # prompts explicitly forbid writing them directly.
    status = handoff.parent / f"{node_id}.status.json"
    environment = {
        "TURN_NODE_ID": str(node_id),
        "TURN_PROJECT_ID": os.getenv("TURN_PROJECT_ID", ""),
        "TURN_REPO": str(Path(cwd).resolve()),
        "TURN_HANDOFF_KIND": kind,
        "TURN_HANDOFF_FILE": str(handoff),
        "TURN_STATUS_FILE": str(status),
        "TURN_HARNESS": str(getattr(getattr(agent, "harness", None), "value", "") or ""),
        "TURN_AGENT_MODEL": str(getattr(agent, "model", None) or ""),
        "TURN_AGENT_REASONING": str(getattr(getattr(agent, "reasoning", None), "value", "") or ""),
        "TURN_AGENT_SKILLS": ",".join(str(path) for path in scoped_skills.values()),
        "TURN_AGENT_SKILL_IDS": ",".join(skill_ids),
        "TURN_AGENT_SKILL_ROOT": str(Path(cwd) / ".turn" / "skills"),
        "TURN_AGENT_TOOLS": csv("tools"),
        "TURN_AGENT_MCP_SERVERS": csv("mcp_servers"),
    }
    environment.update(mcp_runtime.environment)
    return environment


def read_result_file(path: Path) -> dict[str, Any] | None:
    """Read an atomically-written JSON handoff, rejecting partial/invalid data."""
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def opencode_session_ids(binary: str = "opencode") -> list[str]:
    """Read OpenCode's own session index without coupling Turn to its storage."""
    if shutil.which(binary) is None:
        return []
    try:
        completed = subprocess.run(
            [binary, "session", "list"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return list(dict.fromkeys(re.findall(r"ses_[A-Za-z0-9]+", completed.stdout)))


async def run_until_result(
    transport: Any,
    node_id: uuid.UUID,
    command: list[str],
    *,
    cwd: str,
    result_path: Path,
    stream=None,
    timeout: float | None = None,
    idle_warning: float | None = None,
    idle_reap: float | None = None,
    session_callback=None,
    session_probe: Callable[[], Awaitable[str | None]] | None = None,
    session_marker: str | None = None,
    excluded_session_ids: set[str] | None = None,
    harness_name: str | None = None,
    initial_input: str | None = None,
    initial_input_mode: str = "native",
    environment: dict[str, str] | None = None,
):
    """Keep a native TUI alive until it submits its result file.

    The process remains a normal interactive PTY while the agent works. The
    file is only a completion signal; all terminal bytes continue flowing to
    the browser and browser input continues flowing back to the PTY. Native
    sessions have no whole-run timeout. A detached, quiet process is reaped
    only after the shared terminal idle grace period so forgotten processes do
    not accumulate.
    """
    owns_attachment = True
    task: asyncio.Task | None = None

    async def abort_owned_attachment() -> None:
        if not owns_attachment:
            return
        try:
            await transport.stop(node_id)
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def wait_for_attachment() -> None:
        """Do not type until the terminal provider reports real readiness."""
        if task is None:
            return
        wait_until_ready = getattr(transport, "wait_until_ready", None)
        if wait_until_ready is not None:
            ready_task = asyncio.create_task(wait_until_ready(node_id))
            done, _ = await asyncio.wait(
                {task, ready_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done and ready_task not in done:
                ready_task.cancel()
                await asyncio.gather(ready_task, return_exceptions=True)
                try:
                    await task
                except Exception as error:
                    raise RuntimeError(
                        "harness terminal closed before Turn could inject its command"
                    ) from error
                raise RuntimeError(
                    "harness terminal closed before Turn could inject its command"
                )
            try:
                await ready_task
            except Exception as error:
                raise RuntimeError(
                    "harness pane was not ready before Turn injected its command"
                ) from error
            return
        deadline = time.monotonic() + 10.0
        while True:
            if transport.snapshot(node_id).get("active"):
                return
            if task.done():
                try:
                    await task
                except Exception as error:
                    raise RuntimeError(
                        "harness terminal closed before Turn could inject its command"
                    ) from error
                raise RuntimeError(
                    "harness terminal closed before Turn could inject its command"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "harness terminal did not become ready before Turn injected its command"
                )
            await asyncio.sleep(0.05)

    async def wait_for_foreground_harness() -> None:
        """Wait for the shell to hand the PTY to the requested harness.

        A durable Herdr pane is initially owned by the shell. The command
        injection call only confirms that Herdr accepted bytes; it does not
        mean the child process is ready to consume its stdin. Process-info is
        the transport-level readiness signal, so prompt delivery does not
        depend on an arbitrary startup sleep.
        """
        reader = getattr(transport, "foreground_process_names", None)
        if reader is None or not harness_name:
            return
        expected = harness_name.rsplit("/", 1)[-1]
        deadline = time.monotonic() + 10.0
        while True:
            if task is not None and task.done():
                try:
                    await task
                except Exception as error:
                    raise RuntimeError(
                        "harness terminal closed before Turn could send its message"
                    ) from error
                raise RuntimeError(
                    "harness terminal closed before Turn could send its message"
                )
            names = await reader(node_id)
            if expected in names:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"harness '{expected}' did not become the foreground process "
                    "before Turn sent its message"
                )
            await asyncio.sleep(0.05)

    async def wait_for_local_process() -> None:
        """Wait for a local PTY process to produce its first terminal frame."""
        snapshot_reader = getattr(transport, "snapshot", None)
        if snapshot_reader is None:
            return
        deadline = time.monotonic() + 10.0
        while True:
            snapshot = snapshot_reader(node_id)
            if snapshot.get("active") and snapshot.get("output"):
                return
            if task is not None and task.done():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("harness did not produce a terminal frame before Turn sent its message")
            await asyncio.sleep(0.05)

    async def reset_injected_session() -> None:
        """Make the durable pane safe for a new provider command.

        Herdr panes outlive a Turn attachment. Reusing one here can leave a
        completed TUI in the foreground, which would interpret the shell
        exports and command as user messages. The provider conversation is not
        discarded: when a session id exists, the command includes it and the
        provider resumes the saved conversation. Only the runner's explicit
        fresh-run path clears that id.

        A plain shell is already the correct launch surface, so preserve it.
        Only replace a pane whose foreground process is not a shell. This is
        important for the first launch and for a normal continuation: closing
        the durable pane unconditionally can race Herdr's control stream and
        leave the subsequent command with no injectable session.
        """
        if not getattr(transport, "supports_inject", False):
            return
        process_reader = getattr(transport, "foreground_process_names", None)
        if process_reader is not None:
            names = await process_reader(node_id)
            shell_names = {"sh", "bash", "zsh", "fish", "ksh"}
            if not names or all(name in shell_names for name in names):
                return
        close_persistent = getattr(transport, "close_persistent_session", None)
        if close_persistent is not None:
            await close_persistent(node_id)
            return
        snapshot_reader = getattr(transport, "snapshot", None)
        if snapshot_reader is not None and snapshot_reader(node_id).get("active"):
            await transport.stop(node_id)

    started_at = time.time()
    try:
        if getattr(transport, "supports_inject", False):
            # A provider-native command carries its initial prompt. Reset any
            # durable Herdr pane first so shell setup can never be interpreted
            # by an old foreground harness as a new user message.
            await reset_injected_session()
            task = asyncio.create_task(
                transport.ensure_session(
                    node_id,
                    cwd=cwd,
                    environment=environment,
                    stream=stream,
                    idle_warning=idle_warning,
                    idle_reap=idle_reap,
                )
            )
            await wait_for_attachment()
            injected = await transport.inject_command(
                node_id,
                " ".join(shlex.quote(part) for part in command),
                environment=environment,
            )
            if not injected:
                raise RuntimeError("Turn could not inject the harness command into Herdr")
        else:
            task = asyncio.create_task(
                transport.run(
                    node_id,
                    command,
                    cwd=cwd,
                    environment=environment,
                    stream=stream,
                    # A native TUI can legitimately be quiet while the model thinks
                    # or while it waits for the user. The run stays open until the
                    # harness submits its result file; completion is signaled by the
                    # file, not by a whole-run timeout.
                    timeout=None,
                    stall_timeout=None,
                    idle_warning=idle_warning,
                    idle_reap=idle_reap,
                )
            )
        if initial_input:
            # Machine transports receive their prompt through stdin after the
            # command starts. Native provider commands carry their prompt in
            # the launch command and never enter this branch.
            if getattr(transport, "supports_inject", False):
                await wait_for_foreground_harness()
            else:
                await wait_for_local_process()
            if task is None or not task.done():
                if initial_input_mode == "stdin":
                    # Non-interactive harnesses launched through Herdr read the
                    # prompt from stdin. Sending it as a shell argument is not
                    # safe: embedded newlines are interpreted by the shell
                    # before the harness process owns the PTY. Send the bytes
                    # only after the sentinel command has started, then close
                    # stdin with EOF.
                    payload = initial_input
                    if not payload.endswith(("\n", "\r")):
                        payload += "\n"
                    for offset in range(0, len(payload), 512):
                        sent = await transport.write(node_id, payload[offset : offset + 512])
                        if getattr(transport, "supports_inject", False) and not sent:
                            raise RuntimeError("Turn could not inject the harness prompt into Herdr")
                        await asyncio.sleep(0.01)
                    sent = await transport.write(node_id, "\x04")
                    if getattr(transport, "supports_inject", False) and not sent:
                        raise RuntimeError("Turn could not close the harness prompt input in Herdr")
                else:
                    # Keep the submit key as a separate PTY event. Codex renders
                    # a long first message as pasted content; appending Enter to
                    # that same write can leave the message in the composer
                    # instead of submitting it.
                    paste = f"\x1b[200~{initial_input}\x1b[201~"
                    for offset in range(0, len(paste), 512):
                        sent = await transport.write(node_id, paste[offset : offset + 512])
                        if getattr(transport, "supports_inject", False) and not sent:
                            raise RuntimeError("Turn could not inject the harness prompt into Herdr")
                        await asyncio.sleep(0.01)
                    sent = await transport.write(node_id, "\r")
                    if getattr(transport, "supports_inject", False) and not sent:
                        raise RuntimeError("Turn could not submit the harness prompt in Herdr")
    except BaseException:
        await abort_owned_attachment()
        raise
    discovered_session: str | None = None
    last_probe = 0.0
    try:
        while task is None or not task.done():
            if session_callback is not None and discovered_session is None:
                discovered_session = _new_codex_session_id(
                    cwd,
                    started_at,
                    session_marker,
                    excluded_session_ids,
                )
                if discovered_session:
                    await session_callback(discovered_session)
            if session_probe is not None and discovered_session is None:
                now = time.monotonic()
                if now - last_probe >= 0.5:
                    last_probe = now
                    try:
                        candidate = await session_probe()
                    except Exception:
                        candidate = None
                    if candidate:
                        discovered_session = candidate
                        if session_callback is not None:
                            await session_callback(candidate)
            if read_result_file(result_path) is not None:
                # A provider may finish its first turn before the session
                # marker is visible in the rollout file. At the completion
                # boundary the newest session for this cwd is the only new
                # candidate, so use it as a final discovery pass. The marker
                # path remains the normal concurrency-safe route.
                if session_callback is not None and discovered_session is None:
                    candidate = _new_codex_session_id(
                        cwd,
                        started_at,
                        session_marker=None,
                        excluded_session_ids=excluded_session_ids,
                    )
                    if candidate:
                        discovered_session = candidate
                        await session_callback(candidate)
                if getattr(transport, "supports_inject", False):
                    # The Herdr pane remains attached and inspectable after
                    # a handoff. Only an explicit cancel/close may stop it.
                    snapshot = transport.snapshot(node_id)
                    return TerminalResult(
                        returncode=0,
                        output=str(snapshot.get("output", "")).encode(),
                    )
                # Non-persistent transports retain their original lifecycle:
                # finish the child PTY once the handoff file is valid.
                await transport.stop(node_id)
                break
            await asyncio.sleep(0.1)
        if owns_attachment:
            return await task
        snapshot = transport.snapshot(node_id)
        return TerminalResult(returncode=0, output=str(snapshot.get("output", "")).encode())
    except asyncio.CancelledError:
        await transport.stop(node_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        raise


def result_handoff(*, plan: bool = False, verification: bool = False) -> str:
    """Prompt fragment for the harness-neutral Turn control plane."""
    turn_cli = "turn"
    if verification:
        shape = '{"decision":"APPROVE","summary":"What was verified","findings":[],"required_changes":[],"evidence_refs":[],"target_node_id":null}'
    elif plan:
        shape = plan_handoff_example()
    else:
        shape = '{"outcome":"COMPLETE","summary":"What happened","missing_inputs":[]}'
    kind = "verification" if verification else "plan" if plan else "result"
    completion = (
        "When the verification is complete, submit the decision and let Turn route it. Omit target_node_id to return to the single dependency, or set it to any earlier node id from `turn graph --format json` to return work there."
        if verification
        else "When the plan is complete, submit it and let Turn continue."
        if plan
        else "When the work is complete, submit the result through the Turn CLI. Keep the terminal available for follow-up conversation after submission."
    )
    artifact_guidance = "" if plan or verification else """
For execution results, include only a small `artifacts` array of repo-relative
files or directories that represent the work. Do not list every changed file;
one directory is better for a large area. Turn will not infer artifacts from
git status or filesystem scans.
"""
    plan_artifact_guidance = "" if not plan else """
For a planning handoff, include `document_refs` or `artifacts` only for files
that already exist and were created or linked during this planning turn. The
simple canonical form is a relative path string, for example:
`"document_refs":["ARCHITECTURE.md"],"artifacts":["ARCHITECTURE.md"]`.
An explicit artifact object uses `kind`, `name`, and `ref`; `path` is not a
schema field. Leave these arrays empty or omitted when no file was created.
"""
    return f"""
TURN CONTROL PLANE:
This is an ordinary terminal session. Use the harness normally: type, run
commands, inspect files, and communicate with the user here. Turn does not
parse or summarize your terminal output, and the harness must not use a
structured-output or JSON-output mode for this protocol.
The installed `turn` command is available on PATH; invoke `turn` directly for
all Turn status and handoff commands. Do not type `TURN_CLI` as a command.

Publish status when useful:
  {turn_cli} agent status --state working --message "..."
Keep the status message short: a few words describing the current action.

For the final {kind}, submit one JSON object matching this shape through the
Turn CLI. The CLI is the only submission interface and writes Turn's internal
record. Do not use filesystem output as a protocol:
{shape}

Submit through stdin using this shell-safe heredoc form. Replace the example
object with the actual single-line JSON object and keep `TURN_PAYLOAD` on its
own line:
{turn_cli} agent {'verify' if verification else 'submit --kind ' + kind} --stdin <<'TURN_PAYLOAD'
{shape}
TURN_PAYLOAD
{artifact_guidance}
{plan_artifact_guidance}

The CLI submission is the only completion signal. Do not finish by printing a
fenced result block or by relying on provider output formatting.

{completion}
""".strip()
