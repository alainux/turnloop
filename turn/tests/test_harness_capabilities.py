from __future__ import annotations

from types import SimpleNamespace

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


def test_model_discovery_uses_configured_binary_when_not_on_path(monkeypatch):
    discovered_with = []

    def resolve(binary):
        discovered_with.append(binary)
        return "/user/local/bin/codex"

    monkeypatch.setattr("turn.workers.harnesses._resolve_binary", resolve)
    monkeypatch.setattr(
        "turn.workers.harnesses._codex_models",
        lambda binary: [binary],
    )
    from turn.workers.harnesses import _discover_models

    _discover_models.cache_clear()
    try:
        assert _discover_models("codex", "codex") == ["/user/local/bin/codex"]
        assert discovered_with == ["codex"]
    finally:
        _discover_models.cache_clear()


def test_pi_model_discovery_preserves_provider_qualified_ids(monkeypatch):
    output = """provider       model                                               context
freeinference  deepseek-v4-flash                                   128K
nous           inclusionai/ling-3.0-flash:free                     262.1K
nvidia         vendor/a/b/c-model                                  128K
"""
    monkeypatch.setattr("turn.workers.harnesses._resolve_binary", lambda _: "/bin/pi")
    monkeypatch.setattr(
        "turn.workers.harnesses.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output),
    )
    from turn.workers.harnesses import _discover_models

    _discover_models.cache_clear()
    try:
        assert _discover_models("pi", "pi") == [
            "freeinference/deepseek-v4-flash",
            "nous/inclusionai/ling-3.0-flash:free",
            "nvidia/vendor/a/b/c-model",
        ]
    finally:
        _discover_models.cache_clear()


def test_opencode_model_discovery_preserves_provider_qualified_ids(monkeypatch):
    output = """provider       model                                               context
opencode       deepseek-v4-flash-free                            200K
nous           tencent/hy3:free                                  262.1K
"""
    monkeypatch.setattr("turn.workers.harnesses._resolve_binary", lambda _: "/bin/opencode")
    monkeypatch.setattr(
        "turn.workers.harnesses.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output),
    )
    from turn.workers.harnesses import _discover_models

    _discover_models.cache_clear()
    try:
        assert _discover_models("opencode", "opencode") == [
            "opencode/deepseek-v4-flash-free",
            "nous/tencent/hy3:free",
        ]
    finally:
        _discover_models.cache_clear()


def test_pi_and_opencode_do_not_invent_models_when_catalog_is_empty(monkeypatch):
    monkeypatch.setattr("turn.workers.harnesses._discover_models", lambda *args: [])

    capabilities = {item["id"]: item for item in harness_capabilities()}

    assert capabilities["pi"]["models"] == []
    assert capabilities["opencode"]["models"] == []
