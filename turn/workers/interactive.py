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

from turn.workers.terminal import TerminalResult


def _new_codex_session_id(cwd: str, started_at: float) -> str | None:
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
            identifier = payload.get("session_id")
            if isinstance(identifier, str) and identifier:
                return identifier
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


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
    def csv(name: str) -> str:
        return ",".join(str(value) for value in (getattr(agent, name, None) or []))

    # These paths are private server-owned protocol locations. Agents receive
    # the metadata only so the Turn CLI can publish into the control plane;
    # prompts explicitly forbid writing them directly.
    status = handoff.parent / f"{node_id}.status.json"
    return {
        "TURN_NODE_ID": str(node_id),
        "TURN_PROJECT_ID": os.getenv("TURN_PROJECT_ID", ""),
        "TURN_REPO": str(Path(cwd).resolve()),
        "TURN_HANDOFF_KIND": kind,
        "TURN_HANDOFF_FILE": str(handoff),
        "TURN_STATUS_FILE": str(status),
        "TURN_CLI": f"{sys.executable} -m turn",
        "TURN_HARNESS": str(getattr(getattr(agent, "harness", None), "value", "") or ""),
        "TURN_AGENT_MODEL": str(getattr(agent, "model", None) or ""),
        "TURN_AGENT_REASONING": str(getattr(getattr(agent, "reasoning", None), "value", "") or ""),
        "TURN_AGENT_SKILLS": csv("skills"),
        "TURN_AGENT_TOOLS": csv("tools"),
        "TURN_AGENT_MCP_SERVERS": csv("mcp_servers"),
    }


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
    initial_input: str | None = None,
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
    if getattr(transport, "supports_inject", False):
        # The persistent session is a plain shell. Launch the harness by typing
        # its command, exactly as a user would. The native harness then
        # receives its first message through the PTY below.
        # The inspector may already be attached to this node's durable Herdr
        # shell.  Do not create a second outer attachment: LocalPtyTransport
        # stores one client per node and a second one used to replace the
        # browser client, yielding a misleading "[terminated]" panel.
        if transport.snapshot(node_id).get("active"):
            task = None
            owns_attachment = False
        else:
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
            for _ in range(50):
                snapshot = transport.snapshot(node_id)
                if task.done() or snapshot.get("active"):
                    break
                await asyncio.sleep(0.02)
        if task is None or not task.done():
            # Shell-quote each argument so multi-word payloads (e.g. a
            # `-c "..."` script) are passed to the shell intact.
            await transport.inject_command(
                node_id,
                " ".join(shlex.quote(part) for part in command),
                environment=environment,
            )
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
        # Native Codex must be started without a positional prompt. Passing
        # one on the command line makes the CLI run a single non-interactive
        # turn and exit at the very moment the user should be able to follow up
        # or correct the result. Send the first message through the PTY so the
        # process remains the same interactive conversation as a normal `codex`
        # invocation.
        for _ in range(50):
            snapshot = transport.snapshot(node_id)
            if task is not None and task.done():
                break
            if snapshot.get("active") and snapshot.get("output"):
                # Codex's native process becomes writable before its composer
                # is painted. Give the TUI a short settling window so the
                # first message is not lost in startup noise.
                # PTY output starts with terminal capability probes. Allow
                # the interactive program to finish its first paint before
                # injecting the initial message; sending while a TUI is still
                # negotiating capabilities is indistinguishable from a user
                # typing into a half-started terminal and is flaky across
                # harnesses.
                await asyncio.sleep(2.5)
                break
            await asyncio.sleep(0.1)
        if task is None or not task.done():
            # Keep the submit key as a separate PTY event.  Codex renders a
            # long first message as pasted content; appending Enter to that
            # same write can leave the message in the composer instead of
            # submitting it.
            # This is ordinary PTY input. Preserve line breaks and spacing so
            # the harness receives exactly the same instruction the user
            # selected in Turn; the transport does not parse it.
            # Deliver the instruction as a standard terminal bracketed paste,
            # in bounded chunks. A single large write can overflow the PTY's
            # canonical input buffer (and has caused native TUIs to crash),
            # while bracketed paste preserves newlines without submitting
            # them as separate messages. The transport still treats every
            # byte as opaque terminal input.
            paste = f"\x1b[200~{initial_input}\x1b[201~"
            for offset in range(0, len(paste), 512):
                await transport.write(node_id, paste[offset : offset + 512])
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.25)
            await transport.write(node_id, "\r")
    started_at = time.time()
    discovered_session: str | None = None
    last_probe = 0.0
    try:
        while task is None or not task.done():
            if session_callback is not None and discovered_session is None:
                discovered_session = _new_codex_session_id(cwd, started_at)
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


def result_handoff(*, plan: bool = False) -> str:
    """Prompt fragment for the harness-neutral Turn control plane."""
    if plan:
        shape = '{"nodes":[{"key":"unique","objective":"Short title","executor":"codex","generated_prompt":"Detailed instructions","depends_on":[]}],"edges":[]}'
    else:
        shape = '{"outcome":"COMPLETE","summary":"What happened","missing_inputs":[]}'
    kind = "plan" if plan else "result"
    completion = (
        "When the plan is complete, submit it and let Turn continue."
        if plan
        else "When the work is complete, submit the result through the Turn CLI. Keep the terminal available for follow-up conversation after submission."
    )
    artifact_guidance = "" if plan else """
For execution results, include only a small `artifacts` array of repo-relative
files or directories that represent the work. Do not list every changed file;
one directory is better for a large area. Turn will not infer artifacts from
git status or filesystem scans.
"""
    return f"""
TURN CONTROL PLANE:
This is an ordinary terminal session. Use the harness normally: type, run
commands, inspect files, and communicate with the user here. Turn does not
parse or summarize your terminal output, and the harness must not use a
structured-output or JSON-output mode for this protocol.

Publish status when useful:
  {sys.executable} -m turn agent status --state working --message "..."
Keep the status message short: a few words describing the current action.

For the final {kind}, submit one JSON object matching this shape through the
Turn CLI. The CLI is the only submission interface and writes Turn's internal
record. Do not use filesystem output as a protocol:
{shape}
  {sys.executable} -m turn agent submit --kind {kind} --payload '<JSON_OBJECT>'
Replace `<JSON_OBJECT>` with the actual single-line JSON object.
{artifact_guidance}

The CLI submission is the only completion signal. Do not finish by printing a
fenced result block or by relying on provider output formatting.

{completion}
""".strip()
