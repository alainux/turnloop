from __future__ import annotations

import pytest

from turn.config import Settings, validate_server_settings
from turn.workers.registry import build_registry


def test_served_configuration_rejects_test_only_modes():
    with pytest.raises(RuntimeError, match="heuristic planning is test-only"):
        validate_server_settings(
            Settings(planner="heuristic", default_executor="echo")
        )


def test_production_registry_cannot_load_deterministic_modes():
    config = Settings(planner="heuristic", default_executor="echo")
    with pytest.raises(RuntimeError, match="non-Codex planners are test-only"):
        build_registry(config)

    test_registry = build_registry(config, test_mode=True)
    assert test_registry.get("echo") is not None
    assert test_registry.planner is not None
    assert test_registry.planner.name == "heuristic"
