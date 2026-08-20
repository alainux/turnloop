from __future__ import annotations

import uuid

from turn.workers.pi_telemetry import (
    prepare_interactive_pi_telemetry,
    with_interactive_pi_telemetry,
)


def test_pi_telemetry_is_encapsulated_in_a_harness_launch_sidecar(tmp_path):
    node_id = uuid.uuid4()

    environment = prepare_interactive_pi_telemetry(str(tmp_path), node_id)

    extension = tmp_path / ".turn" / "metrics" / "pi-turn-metrics.ts"
    events = tmp_path / ".turn" / "metrics" / f"{node_id}.pi.jsonl"
    assert environment == {
        "TURN_METRICS_FILE": str(events),
        "TURN_PI_TELEMETRY_EXTENSION": str(extension),
    }
    assert "tool_execution_start" in extension.read_text(encoding="utf-8")
    assert not events.exists()


def test_pi_telemetry_extension_preserves_the_native_prompt_position(tmp_path):
    environment = prepare_interactive_pi_telemetry(str(tmp_path), uuid.uuid4())

    command = with_interactive_pi_telemetry(["pi", "--model", "test", "prompt"], environment)

    assert command[-1] == "prompt"
    assert command[-3:-1] == ["-e", environment["TURN_PI_TELEMETRY_EXTENSION"]]
