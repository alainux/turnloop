from __future__ import annotations

import json
import asyncio

from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    ArtifactKind,
    ArtifactSpec,
    DocumentRef,
    InputKind,
    InputSpec,
    NodeSpec,
    NodeStatus,
    PlanResult,
    RunPolicy,
)


async def test_pin_store_round_trip_preserves_graph_runs_artifacts_and_policy(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project(
        "Build a round-trip fixture",
        name="Round Trip",
        agent=AgentConfig(type_id=AgentType.PLANNER, model="fixture"),
        run_policy=RunPolicy(auto_run=False, max_retries=2),
    )
    created = await store.apply_plan(
        root,
        PlanResult(
            document_refs=[DocumentRef(ref="ARCHITECTURE.md", title="Architecture")],
            nodes=[
                NodeSpec(
                    key="build",
                    objective="Build the fixture",
                    executor="deterministic",
                    required_inputs=[InputSpec(id="approval", kind=InputKind.APPROVAL, label="Approve")],
                    resource_refs=["README.md"],
                    document_refs=[DocumentRef(ref="DESIGN.md")],
                    artifacts=[ArtifactSpec(kind=ArtifactKind.TEXT, name="plan", content="build")],
                ),
                NodeSpec(
                    key="verify",
                    objective="Verify the fixture",
                    agent_type=AgentType.VERIFIER,
                    follows=["build"],
                ),
            ],
            edges=[],
        ),
    )
    # The plan above intentionally exercises containment/sequence edges from
    # node declarations; add a durable run and a user artifact as well.
    run = await store.create_run(created[0], "deterministic")
    await store.update_run(run.id, summary="fixture", session_id="session-1")
    await store.add_artifacts(
        created[0].id,
        [ArtifactSpec(kind=ArtifactKind.USER_INPUT, name="approval", content="yes")],
    )
    state_path = store.project_path(root.id) / ".turn" / "state.json"
    persisted_before = json.loads(state_path.read_text(encoding="utf-8"))

    reloaded = Store(tmp_path / "turn")
    await reloaded.init()

    nodes, edges, artifacts = await store.get_workgraph(root.id)
    reloaded_nodes, reloaded_edges, reloaded_artifacts = await reloaded.get_workgraph(root.id)

    def semantic_nodes(items):
        result = []
        for item in items:
            payload = item.model_dump(mode="json")
            # Capability package paths are project-local deployment state, not
            # part of the durable graph contract.
            result.append(payload)
        return result

    assert semantic_nodes(reloaded_nodes) == semantic_nodes(nodes)
    assert [item.model_dump(mode="json") for item in reloaded_edges] == [
        item.model_dump(mode="json") for item in edges
    ]
    assert [item.model_dump(mode="json") for item in reloaded_artifacts] == [
        item.model_dump(mode="json") for item in artifacts
    ]
    assert await reloaded.get_project_runs(root.id) == await store.get_project_runs(root.id)
    assert set(persisted_before) == {"version", "project_id", "nodes", "edges", "runs", "artifacts"}
    assert persisted_before["version"] == 3


async def test_pin_status_compare_and_set_has_one_winner_for_concurrent_calls(tmp_path):
    store = Store(tmp_path / "turn")
    await store.init()
    root = await store.create_project("Compare and set")

    results = await asyncio.gather(
        store.set_status_if_current(root.id, NodeStatus.COMPLETE, (root.status,)),
        store.set_status_if_current(root.id, NodeStatus.FAILED, (root.status,)),
    )

    assert sum(result is not None for result in results) == 1


async def test_save_drops_are_visible_not_silent(tmp_path):
    """A dropped node write must return None and be logged, never fake success."""
    from turn.logging import EventLog
    logs = EventLog(tmp_path / "turn")
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects", logs=logs)
    await store.init()
    root = await store.create_project(
        "drop visibility",
        repo_path=str(tmp_path / "projects" / "drop-visibility"),
    )
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="solo", objective="Solo work", executor="deterministic")]),
    )
    node = created[0]

    # A snapshot of a node removed by a plan replacement must not resurrect it
    # nor claim success.
    ghost = node.model_copy(deep=True)
    store._states[root.id].nodes.pop(node.id, None)
    assert await store._save_node(ghost) is None
    assert store._states[root.id].nodes.get(node.id) is None

    # The drops are observable in the event log instead of being silent.
    actions = [record.get("action") for record in logs.read(None) + logs.read(root.id)]
    assert actions.count("node.save.dropped") >= 1

    # Once the project itself is no longer loaded, writes fail visibly too.
    store._states.pop(root.id, None)
    assert await store._save_node(node) is None
    actions = [
        record.get("action")
        for record in logs.read(None)
    ]
    assert "node.save.dropped" in actions
    await store.dispose()
