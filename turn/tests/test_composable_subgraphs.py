from __future__ import annotations

import json
from pathlib import Path

import pytest

from turn.__main__ import agent_command, parser
from turn.contracts.dag import parse_plan, validate_subgraph_sources
from turn.domain.schemas import (
    AgentConfig,
    HarnessKind,
    NodeStatus,
    NodeSpec,
    PlanResult,
    RunPolicy,
    SubgraphRef,
)
from turn.tests.fakes import FakeHerdrAdapter, FakeTerminalTransport
from turn.core import TurnCore
from turn.config import Settings
from turn.db.store import Store
from turn.logging import EventLog


def test_subgraph_sources_are_strict_and_nested_without_ingestion(tmp_path: Path):
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps({"nodes": [{"key": "leaf", "objective": "Leaf"}]}))
    source = tmp_path / "root.json"
    source.write_text(json.dumps({
        "nodes": [{
            "key": "branch",
            "objective": "Branch",
            "subgraph_refs": [{"ref": "nested.json"}],
        }],
    }))

    plan = parse_plan({"graph_file": "root.json", "nodes": []})
    validate_subgraph_sources(plan, tmp_path)
    assert plan.subgraph_refs[0].ref == "root.json"

    with pytest.raises(ValueError, match="invalid subgraph source"):
        broken = tmp_path / "broken.json"
        broken.write_text("not json")
        validate_subgraph_sources(
            PlanResult(
                nodes=[],
                subgraph_refs=[SubgraphRef(ref="broken.json")],
            ),
            tmp_path,
        )


def test_cli_graph_file_submission_links_source_and_preserves_atomic_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "graphs" / "planner.json"
    source.parent.mkdir()
    source.write_text(json.dumps({
        "nodes": [{"key": "work", "objective": "Work"}],
    }))
    handoff = tmp_path / ".turn" / "interactive" / "node.plan.json"
    status = tmp_path / ".turn" / "interactive" / "node.status.json"
    monkeypatch.setenv("TURN_REPO", str(tmp_path))
    monkeypatch.setenv("TURN_HANDOFF_FILE", str(handoff))
    monkeypatch.setenv("TURN_STATUS_FILE", str(status))
    monkeypatch.setenv("TURN_NODE_ID", "node")
    monkeypatch.setenv("TURN_DATA_DIR", str(tmp_path / "turn-data"))

    args = parser().parse_args([
        "agent", "submit", "--kind", "plan", "--graph-file", "graphs/planner.json",
    ])
    assert agent_command(args) == 0
    submitted = json.loads(handoff.read_text())
    assert submitted["subgraph_refs"] == [{
        "ref": "graphs/planner.json",
        "title": "planner.json",
    }]
    assert submitted["nodes"][0]["key"] == "work"

    source.write_text("not json")
    with pytest.raises(SystemExit, match="invalid agent submission"):
        agent_command(args)
    assert json.loads(handoff.read_text()) == submitted


@pytest.mark.asyncio
async def test_store_repairs_missing_handoff_links_from_accepted_cli_history(tmp_path: Path):
    data_dir = tmp_path / "turn-data"
    projects_dir = tmp_path / "projects"
    log = EventLog(data_dir)
    store = Store(data_dir=data_dir, projects_dir=projects_dir, logs=log)
    await store.init()
    root = await store.create_project("Recover composed source")
    log.emit_sync(
        root.id,
        kind="agent.action",
        action="agent.submit",
        status="started",
        source="cli",
        data={
            "node_id": str(root.id),
            "kind": "plan",
            "graph_file": ".turn/graphs/recovered.json",
        },
    )
    log.emit_sync(
        root.id,
        kind="agent.action",
        action="agent.submit",
        status="ok",
        source="cli",
        data={"node_id": str(root.id), "kind": "plan"},
    )

    reloaded = Store(data_dir=data_dir, projects_dir=projects_dir, logs=EventLog(data_dir))
    await reloaded.init()
    recovered = await reloaded.get_node(root.id)
    assert recovered is not None
    assert [item.ref for item in recovered.subgraph_refs] == [
        ".turn/graphs/recovered.json"
    ]


@pytest.mark.asyncio
async def test_graph_state_keeps_composition_anchor_links_without_flattening(tmp_path: Path):
    settings = Settings()
    settings.data_dir = str(tmp_path / "turn-data")
    settings.projects_dir = str(tmp_path / "projects")
    settings.runner_tick_seconds = 0.001
    async with TurnCore(
        settings,
        test_mode=True,
        herdr_adapter=FakeHerdrAdapter(),
        terminal_transport=FakeTerminalTransport(),
    ) as core:
        root = await core.create_project(
            "Composable graph",
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=False),
        )
        source = SubgraphRef(ref="graphs/root.json")
        created = await core.store.apply_plan(
            root,
            PlanResult(
                nodes=[NodeSpec(key="work", objective="Work", executor="echo")],
                subgraph_refs=[source],
            ),
        )
        graph = await core.store.get_graph(root.id)
        assert len(graph.nodes) == 2
        assert graph.nodes[0].subgraph_refs[0].ref == "graphs/root.json"
        assert all(node.objective != "Leaf from nested source" for node in graph.nodes)

        running = created[0]
        await core.store.set_status(running.id, NodeStatus.RUNNING)
        with pytest.raises(RuntimeError, match="running nodes"):
            await core.runner._remove_descendants_before_replan(root.id, force=True)

        await core.store.set_status(running.id, NodeStatus.COMPLETE)
        with pytest.raises(RuntimeError, match="--force"):
            await core.runner._remove_descendants_before_replan(root.id)
        removed = await core.runner._remove_descendants_before_replan(root.id, force=True)
        assert removed == [running.id]
