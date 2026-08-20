"""Passive structured telemetry for native harness sessions.

The native terminal remains the harness' own TUI.  These adapters only use
documented provider side channels and write an attempt-scoped JSONL file that
Turn tails through its existing structured-event boundary.  Nothing here
intercepts terminal bytes, tools, or prompt delivery.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from turn.metrics import HarnessEvent, HarnessEventKind


@dataclass(frozen=True)
class NativeTelemetrySidecar:
    path: Path
    source: str
    detail: str

    @property
    def environment(self) -> dict[str, str]:
        return {"TURN_METRICS_FILE": str(self.path)}


async def emit_telemetry_status(
    emit: Callable[[HarnessEvent], Awaitable[None]] | None,
    *,
    harness: str,
    source: str,
    status: str,
    detail: str,
) -> None:
    """Publish health through the ordinary ``harness.event`` log.

    A status is evidence about observability, not an inferred agent action.
    The caller intentionally ignores all failures so telemetry can never
    affect the project run.
    """
    if emit is None:
        return
    try:
        await emit(HarnessEvent(
            kind=HarnessEventKind.STATUS,
            harness=harness,
            name="telemetry",
            status=status,
            data={"source": source, "detail": detail},
        ))
    except Exception:
        return


def _root(cwd: str) -> Path:
    root = Path(cwd) / ".turn" / "metrics"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prepare_sidecar(cwd: str, node_id: uuid.UUID, suffix: str, source: str, detail: str) -> NativeTelemetrySidecar:
    path = _root(cwd) / f"{node_id}.{suffix}.jsonl"
    path.unlink(missing_ok=True)
    return NativeTelemetrySidecar(path=path, source=source, detail=detail)


_CODEX_NOTIFY = r'''# Turn-owned Codex notify handler. It is intentionally fail-open.
import json
import os
import subprocess
import sys
from pathlib import Path

target = os.environ.get("TURN_METRICS_FILE")
if not target:
    raise SystemExit(0)

original = []
raw_notice = "{}"
try:
    # Codex's ``notify`` setting is a single command and its arguments, not a
    # list of callback commands. Turn receives the user-owned command argv as
    # a serialized argument and forwards the same provider notification after
    # it writes the passive telemetry sidecar.
    original = json.loads(sys.argv[-2]) if len(sys.argv) > 2 else []
    raw_notice = sys.argv[-1] if len(sys.argv) > 1 else "{}"
    notice = json.loads(raw_notice)
    if not isinstance(notice, dict):
        notice = {}
    thread_id = str(notice.get("thread-id") or notice.get("thread_id") or "")
    turn_id = str(notice.get("turn-id") or notice.get("turn_id") or "")
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    # The notification identifies exactly one persisted session and turn.
    # Do not fall back to "most recent": concurrent Codex sessions would
    # turn someone else's behavior into evidence for this Turn run.
    chosen = next(root.rglob(f"rollout-*-{thread_id}.jsonl"), None) if thread_id else None
    records = []
    collecting = False
    if chosen is not None and turn_id:
        for line in chosen.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except Exception:
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            outer_type = record.get("type")
            payload_type = payload.get("type")
            record_turn_id = str(payload.get("turn_id") or "")
            if outer_type == "event_msg" and payload_type == "task_started":
                if collecting:
                    break
                collecting = record_turn_id == turn_id
            if outer_type == "turn_context":
                if record_turn_id == turn_id:
                    records.append(record)
                continue
            if not collecting:
                continue
            records.append(record)
            if outer_type == "event_msg" and payload_type == "task_complete":
                if not record_turn_id or record_turn_id == turn_id:
                    break
    if records:
        batch = {
            "type": "turn.codex.rollout",
            "payload": {"type": "turn_rollout", "records": records},
            "notify": notice,
        }
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(batch) + "\n")
except Exception:
    pass
finally:
    # Telemetry must never replace or accidentally suppress a user's own
    # notification command, including when the provider data is unavailable.
    if isinstance(original, list) and all(isinstance(value, str) for value in original) and original:
        try:
            subprocess.run(
                [*original, raw_notice],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
        except Exception:
            pass
'''


def _write_if_changed(path: Path, text: str) -> None:
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def prepare_codex_notify_telemetry(cwd: str, node_id: uuid.UUID) -> NativeTelemetrySidecar:
    """Configure one Codex TUI process with a passive ``notify`` callback."""
    sidecar = _prepare_sidecar(
        # ``notify`` runs when Codex closes a turn, which can be slightly
        # after Turn's file handoff. Use an attempt-scoped file so a new Run
        # can never consume late records from the prior attempt.
        cwd, node_id, f"{uuid.uuid4().hex}.codex", "codex.notify-rollout",
        "Codex notify callback will export its structured rollout after each completed turn.",
    )
    _write_if_changed(_root(cwd) / "codex-turn-notify.py", _CODEX_NOTIFY)
    return sidecar


def codex_notify_flags(cwd: str, *, home: Path | None = None) -> list[str]:
    """Return a per-process notify wrapper without replacing user behaviour.

    Codex models ``notify`` as one argv array. The wrapper preserves that
    user-owned argv exactly, receives the provider notification itself, and
    writes the sidecar before forwarding the same payload to the existing
    callback. It is scoped to this Turn process with ``-c`` only.
    """
    script = _root(cwd) / "codex-turn-notify.py"
    existing: list[str] = []
    try:
        config_path = (home or Path.home()) / ".codex" / "config.toml"
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        configured = parsed.get("notify") if isinstance(parsed, dict) else None
        if isinstance(configured, str):
            existing.append(configured)
        elif isinstance(configured, list):
            existing.extend(value for value in configured if isinstance(value, str))
    except Exception:
        # A malformed user config is Codex's concern; do not make metrics a
        # second config parser or prevent the original launch.
        pass
    # This array is one command and its arguments, not a callback list. Codex
    # appends its structured notification as the final argv item.
    command = [sys.executable, str(script), json.dumps(existing)]
    return ["-c", f"notify={json.dumps(command)}"]


async def drain_late_sidecar(
    path: Path,
    handler: Callable[[str], Awaitable[None]],
    *,
    timeout_seconds: float = 10.0,
    quiet_seconds: float = 0.3,
) -> int:
    """Drain a provider sidecar that arrives after Turn accepts a handoff.

    Each record flows through the worker's existing normalized-event emitter;
    the collector only outlives the worker so provider notification latency
    cannot delay or change the project result.
    """
    offset = 0
    buffer = ""
    delivered = 0
    last_data_at: float | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        while True:
            now = loop.time()
            try:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read()
            except OSError:
                chunk = b""
            if chunk:
                offset += len(chunk)
                last_data_at = now
                buffer += chunk.decode("utf-8", errors="replace")
                lines = buffer.splitlines(keepends=True)
                buffer = ""
                for line in lines:
                    if not line.endswith(("\n", "\r")):
                        buffer += line
                        continue
                    try:
                        await handler(line)
                        delivered += 1
                    except Exception:
                        # Telemetry is passive; malformed data must never
                        # affect an already-completed project run.
                        continue
            if delivered and last_data_at is not None and now - last_data_at >= quiet_seconds:
                return delivered
            if now >= deadline:
                if buffer:
                    try:
                        await handler(buffer)
                        delivered += 1
                    except Exception:
                        pass
                return delivered
            await asyncio.sleep(0.1)
    finally:
        path.unlink(missing_ok=True)


def schedule_late_sidecar_collection(
    sidecar: NativeTelemetrySidecar,
    handler: Callable[[str], Awaitable[None]],
    *,
    emit: Callable[[HarnessEvent], Awaitable[None]] | None,
    harness: str,
    unavailable_detail: str,
) -> asyncio.Task[None]:
    """Collect a late native completion without blocking graph execution."""
    async def collect() -> None:
        delivered = await drain_late_sidecar(sidecar.path, handler)
        if delivered == 0:
            await emit_telemetry_status(
                emit,
                harness=harness,
                source=sidecar.source,
                status="degraded",
                detail=unavailable_detail,
            )

    return asyncio.create_task(collect())


_CLAUDE_HOOK = r'''# Turn-owned Claude hook handler. It never changes Claude's hook response.
import json
import os
import sys
from pathlib import Path

try:
    target = os.environ.get("TURN_METRICS_FILE")
    if not target:
        raise SystemExit(0)
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        payload = {}
    with Path(target).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "turn.claude.hook", "payload": payload}) + "\n")
except Exception:
    pass
'''


def prepare_claude_hook_telemetry(cwd: str, node_id: uuid.UUID) -> tuple[NativeTelemetrySidecar, Path]:
    """Create additive Claude settings containing passive lifecycle hooks."""
    sidecar = _prepare_sidecar(
        cwd, node_id, "claude", "claude.hooks",
        "Claude lifecycle hooks will export structured tool and session events.",
    )
    root = _root(cwd)
    script = root / "claude-turn-hook.py"
    _write_if_changed(script, _CLAUDE_HOOK)
    command = " ".join(shlex.quote(value) for value in (sys.executable, str(script)))
    hook = {"type": "command", "command": command}
    settings = {
        "hooks": {
            "SessionStart": [{"hooks": [hook]}],
            "UserPromptSubmit": [{"hooks": [hook]}],
            "PreToolUse": [{"matcher": ".*", "hooks": [hook]}],
            "PostToolUse": [{"matcher": ".*", "hooks": [hook]}],
            "PostToolUseFailure": [{"matcher": ".*", "hooks": [hook]}],
            "PermissionRequest": [{"matcher": ".*", "hooks": [hook]}],
            "Stop": [{"hooks": [hook]}],
        },
    }
    settings_path = root / f"{node_id}.claude-settings.json"
    settings_path.write_text(json.dumps(settings, sort_keys=True), encoding="utf-8")
    return sidecar, settings_path


def with_claude_telemetry(command: list[str], settings_path: Path | None) -> list[str]:
    """Attach additive settings without moving the normal positional prompt."""
    if settings_path is None or not command:
        return command
    return [*command[:-1], "--settings", str(settings_path), command[-1]]
