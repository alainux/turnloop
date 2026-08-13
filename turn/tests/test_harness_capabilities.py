from __future__ import annotations

import pytest

from turn.domain.schemas import AgentConfig, HarnessKind, ReasoningLevel
from turn.workers.harnesses import (
    HARNESS_CATALOG,
    harness_capabilities,
    reasoning_levels_for,
    validate_agent_capabilities,
)


def test_reasoning_is_resolved_by_harness_and_model_family():
    assert reasoning_levels_for("codex", None) == HARNESS_CATALOG["codex"]["reasoning"]
    assert reasoning_levels_for("codex", "vendor/fast-mini") == ["default", "low", "medium", "high"]
    assert reasoning_levels_for("codex", "text-embedding-3") == ["default"]
    assert "xhigh" not in reasoning_levels_for("opencode", "vendor/small")
    assert reasoning_levels_for("codex", "smalltalk-pro") == HARNESS_CATALOG["codex"]["reasoning"]


def test_invalid_model_effort_pair_is_rejected_at_the_adapter_boundary():
    invalid = AgentConfig(
        harness=HarnessKind.CODEX,
        model="vendor/fast-mini",
        reasoning=ReasoningLevel.XHIGH,
    )
    with pytest.raises(ValueError, match="choose one of"):
        validate_agent_capabilities(invalid)
    validate_agent_capabilities(invalid.model_copy(update={"reasoning": ReasoningLevel.HIGH}))


def test_capability_payload_is_data_driven_and_extensible():
    capabilities = harness_capabilities()
    assert {item["id"] for item in capabilities} == {"codex", "claude", "opencode", "pi"}
    assert all(item["accepts_custom_models"] for item in capabilities)
    assert all(item["reasoning_profiles"] for item in capabilities)
