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
from turn.capabilities.catalog import CapabilityCatalog
from turn.capabilities.plugin import CapabilityPluginError
from turn.config import settings
from turn.workers.capabilities import harness_capability_adapter
from turn.workers.terminal import TerminalResult


class _TerminalAttachmentClosedBeforeInjection(RuntimeError):
    """The transient controller ended while its durable pane still existed."""


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
    cwd: str,
    node_id: uuid.UUID,
    kind: str,
    handoff: Path,
    agent: Any | None = None,
    *,
    data_dir: str | Path | None = None,
    project_repo_path: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, str]:
    """Install loaded capabilities and prepare one native harness launch."""
    capability_ids = list(dict.fromkeys(getattr(agent, "capabilities", None) or []))
    catalog = CapabilityCatalog(Path(data_dir or settings.data_dir) / "capabilities")
    packages = []
    for capability_id in capability_ids:
        try:
            packages.append(catalog.resolve_project(capability_id, cwd))
        except CapabilityPluginError as error:
            raise ValueError(str(error)) from error
    adapter = harness_capability_adapter(getattr(agent, "harness", None))
    for package in packages:
        adapter.install(package, cwd)
        verification = adapter.verify(package, cwd)
        if not verification.installed:
            raise ValueError(
                f"capability {package.id!r} failed {adapter.harness.value} installation verification"
            )
    launch = adapter.prepare_launch(packages, cwd, node_id)

    # These paths are private server-owned protocol locations. Agents receive
    # the metadata only so the Turn CLI can publish into the control plane;
    # prompts explicitly forbid writing them directly.
    status = handoff.parent / f"{node_id}.status.json"
    environment = {
        "TURN_NODE_ID": str(node_id),
        "TURN_RUN_ID": str(run_id or ""),
        "TURN_PROJECT_ID": os.getenv("TURN_PROJECT_ID", ""),
        "TURN_REPO": str(Path(project_repo_path or cwd).resolve()),
        "TURN_DATA_DIR": str(Path(data_dir or settings.data_dir).expanduser().resolve()),
        "TURN_HANDOFF_KIND": kind,
        "TURN_HANDOFF_FILE": str(handoff),
        "TURN_STATUS_FILE": str(status),
        "TURN_HARNESS": str(getattr(getattr(agent, "harness", None), "value", "") or ""),
        "TURN_AGENT_MODEL": str(getattr(agent, "model", None) or ""),
        "TURN_AGENT_REASONING": str(getattr(getattr(agent, "reasoning", None), "value", "") or ""),
        "TURN_AGENT_CAPABILITIES": ",".join(capability_ids),
        "TURN_AGENT_SKILLS": ",".join(launch.skill_paths),
        "TURN_AGENT_MCP_SERVERS": ",".join(
            component.name for package in packages for component in package.mcp_servers
        ),
    }
    if launch.claude_config:
        environment["TURN_AGENT_MCP_CONFIG"] = launch.claude_config
    if launch.pi_mcp_config:
        environment["TURN_AGENT_MCP_CONFIG"] = launch.pi_mcp_config
    if launch.opencode_config:
        environment["OPENCODE_CONFIG_CONTENT"] = launch.opencode_config
    if launch.codex_overrides:
        environment["TURN_AGENT_CODEX_MCP_OVERRIDES"] = json.dumps(list(launch.codex_overrides))
    return environment


def read_result_file(path: Path) -> dict[str, Any] | None:
    """Read an atomically-written JSON handoff, rejecting partial/invalid data."""
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_submission_file(path: Path) -> tuple[bool, dict[str, Any] | None]:
    """Return ``(present, payload)`` so invalid handoffs are distinguishable.

    ``read_result_file`` intentionally remains a tolerant polling helper.
    Provider completion paths need the stronger distinction: a present but
    malformed handoff is correction-required while a missing handoff after a
    dead process is an infrastructure failure.
    """
    try:
        raw = path.read_text()
    except (FileNotFoundError, OSError):
        return False, None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return True, None
    return True, value if isinstance(value, dict) else None


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


def _injected_command_with_markers(
    command: list[str],
    process_start_path: Path,
    process_exit_path: Path,
    machine_output_path: Path | None = None,
) -> str:
    """Wrap a shell command with an attempt-scoped process lifecycle marker.

    Herdr keeps the shell pane alive after a command exits, so its control
    stream is not a process-exit signal. The wrapper records the child status
    while leaving the durable shell available for inspection and later runs.
    ``set +e`` is scoped to the subshell so a user's shell ``errexit`` setting
    cannot skip the exit marker after a failed harness launch.
    """
    quoted_command = " ".join(shlex.quote(part) for part in command)
    start = shlex.quote(str(process_start_path))
    exit_path = shlex.quote(str(process_exit_path))
    stdout = (
        f" > {shlex.quote(str(machine_output_path))}"
        if machine_output_path is not None
        else ""
    )
    return (
        "( set +e; "
        f"printf '%s\\n' \"$$\" > {start}; "
        f"{quoted_command}{stdout}; "
        "__turn_exit=$?; "
        f"printf '%s\\n' \"$__turn_exit\" > {exit_path} )"
    )


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
    known_session_id: str | None = None,
    session_probe: Callable[[], Awaitable[str | None]] | None = None,
    session_marker: str | None = None,
    excluded_session_ids: set[str] | None = None,
    harness_name: str | None = None,
    initial_input: str | None = None,
    initial_input_mode: str = "native",
    environment: dict[str, str] | None = None,
    process_start_path: Path | None = None,
    process_exit_path: Path | None = None,
    machine_output_path: Path | None = None,
    machine_output_handler: Callable[[str], Awaitable[None]] | None = None,
    capture_machine_output: bool = False,
    keep_attached: bool = True,
    input_delivery_timeout: float = 5.0,
):
    """Keep a native TUI alive until its Turn handoff file is available.

    The process remains a normal interactive PTY while the agent works. The
    handoff JSON is the file-backed Turn payload; all terminal bytes continue
    flowing to the browser and browser input continues flowing back to the PTY.
    Native sessions have no
    whole-run timeout. A detached, quiet process is reaped only after the
    shared terminal idle grace period so forgotten processes do not accumulate.
    """
    owns_attachment = True
    task: asyncio.Task | None = None
    # A persistent shell is intentionally longer-lived than the provider
    # command it runs. When an injected attempt has no explicit exit marker,
    # create the missing marker and make the command publish its actual child
    # exit code. Without this boundary a failed provider returns to the shell
    # while ``ensure_session`` remains alive, leaving the scheduler's task (and
    # the node's RUNNING projection) stuck forever.
    injected_markers = (
        getattr(transport, "supports_inject", False)
        and process_exit_path is None
    )
    if injected_markers:
        if process_start_path is None:
            process_start_path = result_path.with_suffix(".started")
            process_start_path.unlink(missing_ok=True)
        process_exit_path = result_path.with_suffix(".exit")
        process_exit_path.unlink(missing_ok=True)
    if machine_output_path is not None:
        machine_output_path.unlink(missing_ok=True)
    machine_output_offset = 0
    machine_output_buffer = ""

    async def drain_machine_output(*, final: bool = False) -> None:
        """Forward complete JSONL records from an explicit structured channel.

        The channel can be captured stdout for a headless process or a
        provider-owned sidecar written independently of a native TUI. Native
        sidecars deliberately leave ``capture_machine_output`` false, so this
        helper never redirects, reads, or repackages PTY output.
        """
        nonlocal machine_output_offset, machine_output_buffer
        if machine_output_path is None or machine_output_handler is None:
            return
        try:
            with machine_output_path.open("rb") as stream:
                stream.seek(machine_output_offset)
                chunk = stream.read()
            machine_output_offset += len(chunk)
            if chunk:
                machine_output_buffer += chunk.decode("utf-8", errors="replace")
            lines = machine_output_buffer.splitlines(keepends=True)
            machine_output_buffer = ""
            for line in lines:
                if line.endswith(("\n", "\r")):
                    try:
                        await machine_output_handler(line)
                    except Exception:
                        pass
                else:
                    machine_output_buffer += line
            if final and machine_output_buffer:
                try:
                    await machine_output_handler(machine_output_buffer)
                except Exception:
                    pass
                machine_output_buffer = ""
        except OSError:
            return

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
                    raise _TerminalAttachmentClosedBeforeInjection(
                        "harness terminal closed before Turn could inject its command"
                    ) from error
                raise _TerminalAttachmentClosedBeforeInjection(
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

    async def wait_for_process_start() -> None:
        """Wait until an injected process has actually begun executing.

        Some terminal providers acknowledge a pane command as soon as it is
        queued. That acknowledgement is not process execution: a worker can
        otherwise clean up an attempt-scoped launcher before the pane's shell
        has opened it. The launcher writes this marker as its first operation,
        giving the lifecycle code a provider-neutral start boundary.
        """
        if process_start_path is None:
            return
        deadline = time.monotonic() + 10.0
        while not process_start_path.exists():
            if task is not None and task.done():
                try:
                    result = await task
                except Exception as error:
                    raise RuntimeError(
                        "harness terminal closed before the process started"
                    ) from error
                raise RuntimeError(
                    f"harness terminal exited with code {result.returncode} before the process started"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "harness launch was accepted but the process did not start"
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

    async def open_injected_attachment() -> None:
        """Attach to a durable pane before injecting exactly one command.

        Herdr's control client is deliberately disposable: its process can
        exit during a workspace/takeover handoff even though the pane and its
        shell remain live.  Retrying one *pre-injection* attachment is safe --
        no provider command has reached the pane yet -- and prevents that
        transient control disconnect from turning a fresh UI run into a false
        harness failure.  A missing pane, a second disconnect, or any later
        failure still remains visible to the scheduler.
        """
        nonlocal task
        for attempt in range(2):
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
            try:
                await asyncio.wait_for(wait_for_attachment(), timeout=10.0)
                return
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    "harness pane did not become ready before Turn launched the command"
                ) from error
            except _TerminalAttachmentClosedBeforeInjection:
                persistent = getattr(transport, "has_persistent_session", None)
                pane_is_live = False
                if persistent is not None:
                    try:
                        pane_is_live = bool(await persistent(node_id))
                    except Exception:
                        pane_is_live = False
                if attempt or not pane_is_live:
                    raise
                await asyncio.gather(task, return_exceptions=True)
                task = None
                # Let Herdr finish releasing its just-ended control client
                # before asking it to take over the same durable pane again.
                await asyncio.sleep(0.05)

    started_at = time.time()
    process_completion_deadline: float | None = None
    post_submit_observation_deadline: float | None = None
    handoff_stop_requested = False
    try:
        if getattr(transport, "supports_inject", False):
            # A provider-native command carries its initial prompt. Reset any
            # durable Herdr pane first so shell setup can never be interpreted
            # by an old foreground harness as a new user message.
            await reset_injected_session()
            await open_injected_attachment()
            injected_command = " ".join(shlex.quote(part) for part in command)
            if injected_markers:
                assert process_start_path is not None
                assert process_exit_path is not None
                injected_command = _injected_command_with_markers(
                    command,
                    process_start_path,
                    process_exit_path,
                    machine_output_path if capture_machine_output else None,
                )
            injected = await transport.inject_command(
                node_id,
                injected_command,
                environment=environment,
            )
            if not injected:
                raise RuntimeError("Turn could not inject the harness command into Herdr")
            try:
                await asyncio.wait_for(wait_for_process_start(), timeout=10.0)
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    "harness command did not start after Herdr accepted it"
                ) from error
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
            await wait_for_process_start()
        if process_start_path is not None and timeout is not None:
            process_completion_deadline = time.monotonic() + timeout
        if initial_input:
            # Machine transports receive their prompt through stdin after the
            # command starts. Native provider commands carry their prompt in
            # the launch command and never enter this branch.
            if getattr(transport, "supports_inject", False):
                try:
                    await asyncio.wait_for(wait_for_foreground_harness(), timeout=10.0)
                except asyncio.TimeoutError as error:
                    raise RuntimeError(
                        "harness did not become ready to receive the Turn prompt"
                    ) from error
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
                        try:
                            sent = await asyncio.wait_for(
                                transport.write(node_id, payload[offset : offset + 512]),
                                timeout=input_delivery_timeout,
                            )
                        except asyncio.TimeoutError as error:
                            raise RuntimeError(
                                "Herdr did not acknowledge Turn prompt delivery"
                            ) from error
                        if getattr(transport, "supports_inject", False) and not sent:
                            raise RuntimeError("Turn could not inject the harness prompt into Herdr")
                        await asyncio.sleep(0.01)
                    try:
                        sent = await asyncio.wait_for(
                            transport.write(node_id, "\x04"), timeout=input_delivery_timeout
                        )
                    except asyncio.TimeoutError as error:
                        raise RuntimeError(
                            "Herdr did not acknowledge Turn prompt EOF"
                        ) from error
                    if getattr(transport, "supports_inject", False) and not sent:
                        raise RuntimeError("Turn could not close the harness prompt input in Herdr")
                else:
                    # Keep the submit key as a separate PTY event. Codex renders
                    # a long first message as pasted content; appending Enter to
                    # that same write can leave the message in the composer
                    # instead of submitting it.
                    paste = f"\x1b[200~{initial_input}\x1b[201~"
                    for offset in range(0, len(paste), 512):
                        try:
                            sent = await asyncio.wait_for(
                                transport.write(node_id, paste[offset : offset + 512]),
                                timeout=input_delivery_timeout,
                            )
                        except asyncio.TimeoutError as error:
                            raise RuntimeError(
                                "Herdr did not acknowledge Turn prompt delivery"
                            ) from error
                        if getattr(transport, "supports_inject", False) and not sent:
                            raise RuntimeError("Turn could not inject the harness prompt into Herdr")
                        await asyncio.sleep(0.01)
                    try:
                        sent = await asyncio.wait_for(
                            transport.write(node_id, "\r"), timeout=input_delivery_timeout
                        )
                    except asyncio.TimeoutError as error:
                        raise RuntimeError(
                            "Herdr did not acknowledge Turn prompt submission"
                        ) from error
                    if getattr(transport, "supports_inject", False) and not sent:
                        raise RuntimeError("Turn could not submit the harness prompt in Herdr")
    except BaseException:
        await abort_owned_attachment()
        raise
    # A resume command already has the provider's canonical conversation id.
    # Do not rediscover it from a shared cwd: concurrently active native
    # workers can touch each other's Codex rollout files, and a mistaken
    # replacement makes the next automatic continuation resume the wrong
    # conversation. Discovery remains only for a genuinely fresh launch.
    discovered_session: str | None = known_session_id
    last_probe = 0.0

    def process_exit_code() -> int | None:
        if process_exit_path is None:
            return None
        try:
            value = process_exit_path.read_text(encoding="utf-8").strip()
            return int(value) if value else None
        except (FileNotFoundError, OSError, ValueError):
            return None

    try:
        while (
            task is None
            or not task.done()
            # A process-backed launch must enter the marker-handling branch at
            # least once after its control task ends. Otherwise a provider
            # that closes its control stream early can skip the detach/return
            # code even though the child has only just submitted its result.
            or process_start_path is not None
        ):
            if (
                process_completion_deadline is not None
                and time.monotonic() >= process_completion_deadline
            ):
                await abort_owned_attachment()
                raise asyncio.TimeoutError
            await drain_machine_output()
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
            submitted = read_result_file(result_path)
            exit_code = process_exit_code()
            # A process-backed provider can crash after writing its start
            # marker but before publishing the optional exit marker. Once the
            # control task has ended there is no future event that can make
            # this attempt complete, so return its real status immediately.
            if task is not None and task.done() and process_start_path is not None:
                result = await task
                # A queued provider command can legitimately have a successful
                # control task while its child is still running; keep waiting
                # for its exit marker in that case. Any nonzero control result
                # is a terminal failure and must never be hidden by a missing
                # marker.
                if process_exit_path is None or result.returncode != 0:
                    if getattr(transport, "supports_inject", False):
                        await transport.detach(node_id)
                    return result
            # A process harness may publish its CLI handoff just before its
            # shell process exits. Persistent native harnesses intentionally
            # remain open after a successful handoff, so observe them only for
            # a short crash window before preserving the attached process.
            if submitted is not None and process_exit_path is not None and exit_code is None:
                if keep_attached:
                    if post_submit_observation_deadline is None:
                        post_submit_observation_deadline = time.monotonic() + 0.5
                    if time.monotonic() < post_submit_observation_deadline:
                        await asyncio.sleep(0.05)
                        continue
                else:
                    await asyncio.sleep(0.05)
                    continue
            if submitted is not None:
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
                    if keep_attached:
                        # The Herdr pane remains attached and inspectable after
                        # a handoff. Only an explicit cancel/close may stop it.
                        snapshot = transport.snapshot(node_id)
                        return TerminalResult(
                            returncode=exit_code if exit_code is not None else 0,
                            output=str(snapshot.get("output", "")).encode(),
                        )
                    # Short-lived process harnesses have completed once the
                    # CLI submission and process exit are both observed.
                    # Release only Turn's control client; the durable Herdr
                    # shell pane and its scrollback remain available.
                    await transport.detach(node_id)
                    result = await task
                    return TerminalResult(
                        returncode=exit_code if exit_code is not None else result.returncode,
                        output=result.output,
                        display_output=result.display_output,
                        stalled=result.stalled,
                        idle_reaped=result.idle_reaped,
                    )
                # Non-persistent transports retain their original lifecycle:
                # finish the child PTY once the handoff file is valid.
                handoff_stop_requested = task is not None and not task.done()
                await transport.stop(node_id)
                break
            if exit_code is not None:
                # A process exited without publishing the CLI handoff. Return
                # its real status so the worker can report a failed launch
                # instead of waiting forever on an artifact that will never
                # arrive.
                if getattr(transport, "supports_inject", False):
                    await transport.detach(node_id)
                    result = await task
                    return TerminalResult(
                        returncode=exit_code,
                        output=result.output,
                        display_output=result.display_output,
                        stalled=result.stalled,
                        idle_reaped=result.idle_reaped,
                    )
                await transport.stop(node_id)
                break
            await asyncio.sleep(0.1)
        if owns_attachment:
            result = await task
            exit_code = process_exit_code()
            if exit_code is not None:
                return TerminalResult(
                    returncode=exit_code,
                    output=result.output,
                    display_output=result.display_output,
                    stalled=result.stalled,
                    idle_reaped=result.idle_reaped,
                )
            if handoff_stop_requested:
                return TerminalResult(
                    returncode=0,
                    output=result.output,
                    display_output=result.display_output,
                    stalled=result.stalled,
                    idle_reaped=result.idle_reaped,
                )
            return result
        snapshot = transport.snapshot(node_id)
        return TerminalResult(returncode=0, output=str(snapshot.get("output", "")).encode())
    except asyncio.CancelledError:
        await transport.stop(node_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        await drain_machine_output(final=True)
