from __future__ import annotations

import uuid

import pytest

from turn.config import Settings, validate_server_settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, HarnessKind, Node
from turn.runner.runner import Runner
from turn.workers.registry import build_registry


def test_served_configuration_rejects_test_only_modes():
    with pytest.raises(RuntimeError, match="heuristic planning is test-only"):
        validate_server_settings(
            Settings(planner="heuristic", default_executor="deterministic")
        )


def test_production_registry_cannot_load_deterministic_modes():
    config = Settings(planner="heuristic", default_executor="deterministic")
    with pytest.raises(RuntimeError, match="non-Codex planners are test-only"):
        build_registry(config)

    test_registry = build_registry(config, test_mode=True)
    assert test_registry.get("deterministic") is not None
    assert test_registry.planner is not None
    assert test_registry.planner.name == "heuristic"


def test_process_mock_provider_is_test_only():
    config = Settings(planner="mock", default_executor="mock")
    with pytest.raises(RuntimeError, match="mock planning is test-only"):
        validate_server_settings(config)
    with pytest.raises(RuntimeError, match="non-Codex planners are test-only"):
        build_registry(config)

    test_registry = build_registry(config, test_mode=True)
    assert test_registry.get("mock") is not None
    assert test_registry.planner is not None
    assert test_registry.planner.name == "mock-planner"


def test_test_registry_keeps_real_planner_separate_from_mock_planner():
    registry = build_registry(
        Settings(planner="mock", default_executor="mock"),
        test_mode=True,
    )

    assert registry.get_planner("mock") is not None
    assert registry.get_planner("mock").name == "mock-planner"
    assert registry.get_planner("real") is not None
    assert registry.get_planner("real").name == "agent-planner"


def test_codex_test_registry_also_serves_explicit_mock_nodes():
    registry = build_registry(
        Settings(planner="codex", default_executor="codex"),
        test_mode=True,
    )

    assert registry.get_planner("mock") is not None
    assert registry.get_planner("mock").name == "mock-planner"
    assert registry.get_planner("real") is not None


def test_runner_selects_planner_from_the_node_harness(tmp_path):
    settings = Settings(
        data_dir=str(tmp_path / "turn"),
        projects_dir=str(tmp_path / "projects"),
        planner="mock",
        default_executor="mock",
    )
    registry = build_registry(settings, test_mode=True)
    runner = Runner(Store(settings.data_dir), registry=registry, settings=settings)
    project_id = uuid.uuid4()

    mock_node = Node(
        project_id=project_id,
        objective="mock",
        executor="mock",
        agent=AgentConfig(harness=HarnessKind.MOCK),
    )
    real_node = Node(
        project_id=project_id,
        objective="real",
        executor="codex",
        agent=AgentConfig(harness=HarnessKind.CODEX),
    )

    assert runner._planner_for(mock_node).name == "mock-planner"
    assert runner._planner_for(real_node).name == "agent-planner"
