"""Pi-specific behavioral telemetry integration.

This module owns the small, passive extension required to collect structured
events while Pi keeps its native interactive TUI.  The extension writes a
JSONL sidecar only; it never intercepts tool calls or controls the UI.
"""
from __future__ import annotations

import uuid
from pathlib import Path


_PI_TELEMETRY_EXTENSION = r'''// Turn-owned, fail-open behavioral event sidecar.
import { appendFileSync } from "node:fs";

const target = process.env.TURN_METRICS_FILE;
const write = (value: unknown) => {
  if (!target) return;
  try { appendFileSync(target, JSON.stringify(value) + "\n"); } catch { /* telemetry is optional */ }
};

export default function (pi: any) {
  pi.on("turn_start", (event: any) => write({ type: "turn_start", event }));
  pi.on("before_agent_start", (event: any) => {
    write({ type: "context_access", context: event.context, skills: event.skills });
  });
  pi.on("tool_execution_start", (event: any) => {
    write({ type: "tool_execution_start", toolName: event.toolName, args: event.args ?? event.input });
  });
  pi.on("tool_execution_end", (event: any) => {
    write({ type: "tool_execution_end", toolName: event.toolName, args: event.args ?? event.input,
      result: event.result, isError: event.isError });
  });
  pi.on("message_end", (event: any) => {
    write({ type: "message_end", message: event.message });
  });
  pi.on("turn_end", (event: any) => write({ type: "turn_end", event }));
  pi.on("extension_error", (event: any) => write({ type: "extension_error", error: event.error }));
}
'''


def prepare_interactive_pi_telemetry(cwd: str, node_id: uuid.UUID) -> dict[str, str]:
    """Prepare Pi's extension and return only its launch environment.

    Pi provides the TypeScript extension runtime, so Turn does not require a
    plugin framework or a user-installed telemetry dependency.  All I/O is
    best-effort and a missing/unreadable sidecar remains a metrics-only loss.
    """
    root = Path(cwd) / ".turn" / "metrics"
    root.mkdir(parents=True, exist_ok=True)
    extension = root / "pi-turn-metrics.ts"
    if not extension.exists() or extension.read_text(encoding="utf-8") != _PI_TELEMETRY_EXTENSION:
        extension.write_text(_PI_TELEMETRY_EXTENSION, encoding="utf-8")
    events = root / f"{node_id}.pi.jsonl"
    events.unlink(missing_ok=True)
    return {
        "TURN_METRICS_FILE": str(events),
        "TURN_PI_TELEMETRY_EXTENSION": str(extension),
    }


def with_interactive_pi_telemetry(command: list[str], environment: dict[str, str]) -> list[str]:
    """Attach the passive extension to Pi's normal interactive command."""
    extension = environment.get("TURN_PI_TELEMETRY_EXTENSION")
    if not extension or not command:
        return command
    return [*command[:-1], "-e", extension, command[-1]]
