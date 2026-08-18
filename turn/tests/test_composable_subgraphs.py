from __future__ import annotations

import json
from pathlib import Path

import pytest

from turn.__main__ import agent_command, parser
from turn.contracts.dag import parse_plan, validate_subgraph_sources
from turn.domain.schemas import (
    AgentConfig,
    ArtifactKind,
    ArtifactSpec,
    DocumentRef,
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
from turn.runner.runner import _plan_submission_artifact
from turn.graph.logic import validate_single_workflow_leaf, workflow_leaves


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


def test_recursive_subgraph_validation_checks_each_composition_boundary(tmp_path: Path):
    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps({
        "nodes": [
            {"key": "left", "objective": "Left lane", "parent_key": "branch"},
            {"key": "right", "objective": "Right lane", "parent_key": "branch"},
            {
                "key": "join",
                "objective": "Join lanes",
                "parent_key": "branch",
                "follows": ["left", "right"],
            },
            {"key": "branch", "objective": "Parallel branch"},
            {"key": "done", "objective": "Complete branch", "follows": ["branch"]},
        ]
    }))
    root = PlanResult(
        nodes=[],
        subgraph_refs=[SubgraphRef(ref="nested.json")],
    )

    validate_subgraph_sources(root, tmp_path)

    nested.write_text(json.dumps({
        "nodes": [
            {"key": "branch", "objective": "Parallel branch"},
            {"key": "left", "objective": "Left lane", "parent_key": "branch"},
            {"key": "right", "objective": "Right lane", "parent_key": "branch"},
        ]
    }))
    with pytest.raises(ValueError, match="exactly one leaf"):
        validate_subgraph_sources(root, tmp_path)


def test_workflow_shape_has_one_leaf_per_composition_boundary():
    validate_single_workflow_leaf(PlanResult(nodes=[]))
    validate_single_workflow_leaf(
        PlanResult(nodes=[NodeSpec(key="only", objective="Only stage")])
    )
    validate_single_workflow_leaf(
        PlanResult(nodes=[
            NodeSpec(key="start", objective="Start"),
            NodeSpec(key="left", objective="Left", follows=["start"]),
            NodeSpec(key="right", objective="Right", follows=["start"]),
            NodeSpec(
                key="join",
                objective="Join",
                follows=["left", "right"],
            ),
        ])
    )

    with pytest.raises(ValueError, match="exactly one leaf"):
        validate_single_workflow_leaf(PlanResult(nodes=[
            NodeSpec(key="start", objective="Start"),
            NodeSpec(key="left", objective="Left", follows=["start"]),
            NodeSpec(key="right", objective="Right", follows=["start"]),
        ]))

    with pytest.raises(ValueError, match="exactly one leaf"):
        validate_single_workflow_leaf(PlanResult(nodes=[
            NodeSpec(key="left", objective="Left"),
            NodeSpec(key="right", objective="Right"),
        ]))


def test_workflow_shape_accepts_nested_sequence_diamonds_per_boundary():
    plan = PlanResult(nodes=[
        NodeSpec(key="setup", objective="Set up the work"),
        NodeSpec(key="branch", objective="Coordinate the parallel work", follows=["setup"]),
        NodeSpec(key="left", objective="Complete the left lane", parent_key="branch"),
        NodeSpec(key="right", objective="Complete the right lane", parent_key="branch"),
        NodeSpec(
            key="join",
            objective="Reintegrate the parallel lanes",
            parent_key="branch",
            follows=["left", "right"],
        ),
        NodeSpec(key="final", objective="Finish the product", follows=["branch"]),
    ])

    validate_single_workflow_leaf(plan)

    assert workflow_leaves(plan) == {
        None: ("final",),
        "branch": ("join",),
    }


@pytest.mark.asyncio
async def test_apply_plan_materializes_nested_diamond_without_flattening(tmp_path: Path):
    store = Store(tmp_path / "turn-data", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "Materialize a nested workflow",
        agent=AgentConfig(harness=HarnessKind.ECHO),
        run_policy=RunPolicy(auto_run=False),
    )
    plan = PlanResult(nodes=[
        NodeSpec(key="setup", objective="Set up the work"),
        NodeSpec(key="branch", objective="Coordinate parallel work", follows=["setup"]),
        NodeSpec(key="left", objective="Complete the left lane", parent_key="branch"),
        NodeSpec(key="right", objective="Complete the right lane", parent_key="branch"),
        NodeSpec(
            key="join",
            objective="Reintegrate the parallel lanes",
            parent_key="branch",
            follows=["left", "right"],
        ),
        NodeSpec(key="final", objective="Finish the product", follows=["branch"]),
    ])
    validate_single_workflow_leaf(plan)

    await store.apply_plan(root, plan)
    nodes, edges, _ = await store.get_workgraph(root.id)
    nodes_by_id = {node.id: node for node in nodes}
    by_objective = {node.objective: node for node in nodes}
    contains = {
        (nodes_by_id[edge.src].objective, nodes_by_id[edge.dst].objective)
        for edge in edges
        if edge.type.value == "CONTAINS"
    }
    follows = {
        (nodes_by_id[edge.src].objective, nodes_by_id[edge.dst].objective)
        for edge in edges
        if edge.type.value == "FOLLOWS"
    }

    assert by_objective["Complete the left lane"].parent_id == by_objective["Coordinate parallel work"].id
    assert by_objective["Complete the right lane"].parent_id == by_objective["Coordinate parallel work"].id
    assert contains == {
        ("Materialize a nested workflow", "Set up the work"),
        ("Materialize a nested workflow", "Coordinate parallel work"),
        ("Coordinate parallel work", "Complete the left lane"),
        ("Coordinate parallel work", "Complete the right lane"),
        ("Coordinate parallel work", "Reintegrate the parallel lanes"),
        ("Materialize a nested workflow", "Finish the product"),
    }
    assert follows == {
        ("Set up the work", "Coordinate parallel work"),
        ("Complete the left lane", "Reintegrate the parallel lanes"),
        ("Complete the right lane", "Reintegrate the parallel lanes"),
        ("Coordinate parallel work", "Finish the product"),
    }
    assert all(
        nodes_by_id[edge.src].parent_id == nodes_by_id[edge.dst].parent_id
        for edge in edges
        if edge.type.value == "FOLLOWS"
    )
    await store.dispose()


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

    source.write_text(json.dumps({
        "nodes": [
            {"key": "left", "objective": "Left"},
            {"key": "right", "objective": "Right"},
        ],
    }))
    with pytest.raises(SystemExit, match="exactly one leaf"):
        agent_command(args)

    source.write_text("not json")
    with pytest.raises(SystemExit, match="invalid agent submission"):
        agent_command(args)
    assert json.loads(handoff.read_text()) == submitted


def test_plan_submission_receipt_keeps_the_composition_source_link():
    artifact = _plan_submission_artifact(
        PlanResult(
            nodes=[
                NodeSpec(key="foundation", objective="Foundation"),
                NodeSpec(
                    key="integration",
                    objective="Integration",
                    follows=["foundation"],
                ),
            ],
            subgraph_refs=[SubgraphRef(ref=".turn/graphs/planner.json")],
        )
    )

    assert artifact.name == "plan-submission"
    assert artifact.ref == ".turn/graphs/planner.json"
    assert artifact.content["subgraph_refs"][0]["ref"] == ".turn/graphs/planner.json"
    assert artifact.content["node_count"] == 2
    assert artifact.content["edge_count"] == 1
    assert artifact.content["sequence_edge_count"] == 1
    assert artifact.content["composition_edge_count"] == 0
    assert "nodes" not in artifact.content


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


@pytest.mark.asyncio
async def test_empty_normal_handoff_preserves_children_but_explicit_empty_replaces_them(
    tmp_path: Path,
):
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
            "No-op planner handoff",
            agent=AgentConfig(harness=HarnessKind.ECHO),
            run_policy=RunPolicy(auto_run=False),
        )
        source = SubgraphRef(ref="graphs/root.json")
        [child] = await core.store.apply_plan(
            root,
            PlanResult(
                nodes=[NodeSpec(key="work", objective="Work", executor="echo")],
                subgraph_refs=[source],
            ),
        )

        current = await core.store.get_node(root.id)
        assert current is not None
        await core.store.apply_plan(
            current,
            PlanResult(
                nodes=[],
                document_refs=[DocumentRef(ref="docs/notes.md")],
                artifacts=[
                    ArtifactSpec(
                        kind=ArtifactKind.TEXT,
                        name="planning-notes",
                        content="No graph change was needed.",
                    )
                ],
            ),
        )

        unchanged = await core.store.get_node(root.id)
        assert unchanged is not None
        assert unchanged.status is NodeStatus.EXPANDED
        assert unchanged.subgraph_refs[0].ref == "graphs/root.json"
        assert unchanged.document_refs[0].ref == "docs/notes.md"
        assert [item.id for item in await core.store.children_of(root.id)] == [child.id]

        with pytest.raises(RuntimeError, match="--force"):
            await core.runner._apply_plan_revision(
                root.id,
                root.project_id,
                PlanResult(nodes=[]),
            )

        await core.runner._apply_plan_revision(
            root.id,
            root.project_id,
            PlanResult(nodes=[]),
            force=True,
        )
        cleared = await core.store.get_node(root.id)
        assert cleared is not None
        assert cleared.status is NodeStatus.COMPLETE
        assert await core.store.children_of(root.id) == []
        assert cleared.subgraph_refs == []
