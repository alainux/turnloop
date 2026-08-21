"""General data passing between nodes and decision-based routing.

Covers the two graph-level data-flow features:

- Variables: nodes declare ``provides``/``consumes``; a completed node
  publishes declared outputs that downstream predecessors resolve into their
  launch context and ``${name}`` prompt references.
- Routing: FOLLOWS edges may carry a route label (``"key@route"`` in plans or
  an explicit ``EdgeSpec.route``); a completed node's ``route_taken`` decides
  which labeled branch stays active.
"""
from __future__ import annotations

import uuid

import pytest

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    Edge,
    EdgeType,
    HarnessKind,
    Node,
    NodeSpec,
    NodeStatus,
    Outcome,
    PlanResult,
    WorkerResult,
    parse_follows_reference,
)
from turn.graph.logic import GraphWalker, resolve_variables
from turn.workers.base import (
    NodeExecutionContext,
    render_context_block,
    substitute_prompt_variables,
)


async def _project(tmp_path, *, name="data passing"):
    store = Store(tmp_path / "turn", projects_dir=tmp_path / "projects")
    await store.init()
    root = await store.create_project(
        "Data passing project",
        repo_path=str(tmp_path / "projects" / name.replace(" ", "-")),
        agent=AgentConfig(harness=HarnessKind.MOCK, type_id=AgentType.PLANNER),
    )
    return store, root


# ---------------------------------------------------------------------------
# Variables / general data passing
# ---------------------------------------------------------------------------


def test_parse_follows_reference_splits_route():
    assert parse_follows_reference("review@approve") == ("review", "approve")
    assert parse_follows_reference("review") == ("review", None)
    with pytest.raises(ValueError):
        parse_follows_reference("@route-only")


def test_variable_name_validation_rejects_malformed_names():
    with pytest.raises(Exception):
        NodeSpec(key="a", objective="A", provides=["has space"])
    spec = NodeSpec(key="a", objective="A", provides=["api_key", "api_key", "v2"], consumes=["api_key"])
    assert spec.provides == ["api_key", "v2"]
    assert spec.consumes == ["api_key"]


async def test_apply_plan_persists_provides_and_consumes(tmp_path):
    store, root = await _project(tmp_path)
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="produce", objective="Produce", executor="deterministic", provides=["spec"]),
            NodeSpec(key="consume", objective="Consume", executor="deterministic", follows=["produce"], consumes=["spec"]),
        ]),
    )
    producer, consumer = created
    assert producer.provides == ["spec"]
    assert consumer.consumes == ["spec"]
    await store.dispose()


def test_substitute_prompt_variables_only_replaces_resolved_names():
    prompt = "Build ${component} using ${missing_value} unchanged"
    result = substitute_prompt_variables(prompt, {"component": "auth-service"})
    assert result == "Build auth-service using ${missing_value} unchanged"


def test_resolve_variables_prefers_nearest_predecessor():
    near = uuid.uuid4()
    far = uuid.uuid4()
    target = uuid.uuid4()

    def node(node_id, outputs):
        return Node(id=node_id, project_id=target, objective="x", outputs=outputs)

    nodes = [node(near, {"token": "near"}), node(far, {"token": "far", "extra": "far"})]
    edges = [
        Edge(src=far, dst=near, type=EdgeType.FOLLOWS),
        Edge(src=near, dst=target, type=EdgeType.FOLLOWS),
    ]
    walker = GraphWalker(nodes, edges)
    resolved = resolve_variables(target, walker.indexes, ["token", "extra", "absent"])
    assert resolved == {"token": "near", "extra": "far"}


def test_context_block_includes_resolved_variables():
    ctx = NodeExecutionContext(
        node=Node(id=uuid.uuid4(), project_id=uuid.uuid4(), objective="x"),
        variables={"endpoint": "https://api.example.com"},
    )
    block = render_context_block(ctx)
    assert 'variables={"endpoint": "https://api.example.com"}' in block


async def test_publish_outputs_keeps_only_declared_provides(tmp_path):
    store, root = await _project(tmp_path)
    (producer,) = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="produce", objective="Produce", executor="deterministic", provides=["spec"]),
        ]),
    )
    saved = await store.publish_outputs(
        producer.id,
        outputs={"spec": "v1", "undeclared": "dropped"},
        route=None,
    )
    assert saved.outputs == {"spec": "v1"}
    reread = await store.get_node(producer.id)
    assert reread.outputs == {"spec": "v1"}
    await store.dispose()


async def test_publish_outputs_persists_chosen_route(tmp_path):
    store, root = await _project(tmp_path)
    (node,) = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(key="decide", objective="Decide", executor="deterministic")]),
    )
    await store.publish_outputs(node.id, route="approve")
    assert (await store.get_node(node.id)).route_taken == "approve"
    await store.dispose()


# ---------------------------------------------------------------------------
# Decision-based routing
# ---------------------------------------------------------------------------


def _routing_graph(taken):
    decide, fast, thorough = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    def make(node_id, key):
        return Node(id=node_id, project_id=decide, objective=key)

    nodes = [make(decide, "decide"), make(fast, "fast"), make(thorough, "thorough")]
    edges = [
        Edge(src=decide, dst=fast, type=EdgeType.FOLLOWS, route="small"),
        Edge(src=decide, dst=thorough, type=EdgeType.FOLLOWS, route="large"),
    ]
    decide_node = Node(
        id=decide,
        project_id=decide,
        objective="decide",
        status=NodeStatus.COMPLETE,
        route_taken=taken,
    )
    nodes[0] = decide_node
    return GraphWalker(nodes, edges), decide, fast, thorough


def test_routing_activates_only_the_taken_branch():
    walker, decide, fast, thorough = _routing_graph("small")
    evaluation = walker.evaluate()
    assert evaluation.status[fast] is NodeStatus.RUNNABLE
    assert evaluation.status[thorough] is NodeStatus.BLOCKED
    assert "route 'large' not taken" in evaluation.blocked_reason[thorough]


def test_unrouted_completion_keeps_every_branch_open():
    walker, decide, fast, thorough = _routing_graph(None)
    evaluation = walker.evaluate()
    assert evaluation.status[fast] is NodeStatus.RUNNABLE
    assert evaluation.status[thorough] is NodeStatus.RUNNABLE


async def test_labeled_edges_round_trip_through_a_plan(tmp_path):
    store, root = await _project(tmp_path)
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(key="review", objective="Review", executor="deterministic"),
            NodeSpec(key="ship", objective="Ship", executor="deterministic", follows=["review@approve"]),
            NodeSpec(key="fix", objective="Fix", executor="deterministic", follows=["review@reject"]),
        ]),
    )
    review, ship, fix = created
    _, edges, _ = await store.get_workgraph(root.id)
    labeled = {(edge.src, edge.dst): edge.route for edge in edges if edge.type is EdgeType.FOLLOWS}
    assert labeled[(review.id, ship.id)] == "approve"
    assert labeled[(review.id, fix.id)] == "reject"
    await store.dispose()


async def test_deterministic_worker_directive_carries_outputs_and_route(tmp_path):
    """The deterministic fixture can drive both features end to end."""
    from turn.workers.deterministic_worker import DeterministicWorker
    from turn.domain.schemas import Node

    worker = DeterministicWorker()
    ctx = NodeExecutionContext(
        node=Node(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            objective="directive",
            generated_prompt='{"outcome": "COMPLETE", "summary": "done", "outputs": {"spec": "v1"}, "route": "approve"}',
        ),
    )
    result = await worker.execute(ctx)
    assert result.outcome is Outcome.COMPLETE
    assert result.outputs == {"spec": "v1"}
    assert result.route == "approve"


# ---------------------------------------------------------------------------
# End-to-end through the Runner
# ---------------------------------------------------------------------------


async def test_runner_passes_variables_and_routing_end_to_end(tmp_path):
    """Producer publishes outputs; consumer prompt receives them; routing gates."""
    import asyncio

    from turn.runner.events import EventBus
    from turn.runner.runner import Runner
    from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
    from turn.workers.deterministic_worker import DeterministicWorker
    from turn.workers.registry import WorkerRegistry

    store, root = await _project(tmp_path, name="runner passing")
    created = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(
                key="produce",
                objective="Produce the spec",
                executor="deterministic",
                provides=["spec"],
                generated_prompt='{"outcome": "COMPLETE", "summary": "spec ready", "outputs": {"spec": "auth-service v2"}}',
            ),
            NodeSpec(
                key="route",
                objective="Choose the rollout",
                executor="deterministic",
                follows=["produce"],
                consumes=["spec"],
                generated_prompt='{"outcome": "COMPLETE", "summary": "decided", "outputs": {}, "route": "small"}',
            ),
            NodeSpec(
                key="big",
                objective="Big rollout",
                executor="deterministic",
                follows=["route@large"],
                generated_prompt='{"outcome": "COMPLETE", "summary": "big done"}',
            ),
            NodeSpec(
                key="small",
                objective="Small rollout for ${spec}",
                executor="deterministic",
                follows=["route@small"],
                consumes=["spec"],
                generated_prompt='{"outcome": "COMPLETE", "summary": "launched ${spec}"}',
            ),
        ]),
        enforce_organization_audit=False,
    )
    produce, route, big, small = created
    # Step mode is the product default; this scenario exercises auto-run.
    await store.set_auto_run(root.id, True)

    registry = WorkerRegistry()

    launched_prompts: list[str] = []

    class CapturingWorker(DeterministicWorker):
        async def execute(self, ctx):
            if ctx.node.generated_prompt:
                launched_prompts.append(ctx.node.generated_prompt)
            return await super().execute(ctx)

    registry.register(CapturingWorker())
    runner = Runner(
        store,
        registry,
        EventBus(),
        Settings(data_dir=str(tmp_path / "turn"), projects_dir=str(tmp_path / "projects")),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    )
    await runner.start()
    try:
        for _ in range(600):
            small_fresh = await store.get_node(small.id)
            big_fresh = await store.get_node(big.id)
            if (
                small_fresh.status is NodeStatus.COMPLETE
                and big_fresh.status is NodeStatus.BLOCKED
            ):
                break
            await asyncio.sleep(0.05)
        assert (await store.get_node(produce.id)).outputs == {"spec": "auth-service v2"}
        assert (await store.get_node(route.id)).route_taken == "small"
        # The routed branch that was not taken stays visibly blocked.
        assert big_fresh.status is NodeStatus.BLOCKED
        nodes, edges, _ = await store.get_workgraph(root.id)
        evaluation = GraphWalker(nodes, edges).evaluate()
        assert "route 'large' not taken" in evaluation.blocked_reason[big.id]
        # The taken branch completed and its prompt received the variable.
        small_final = await store.get_node(small.id)
        assert small_final.status is NodeStatus.COMPLETE
        assert any("launched auth-service v2" in prompt for prompt in launched_prompts)
    finally:
        await runner.stop()
        await store.dispose()


async def test_cli_vars_reports_variables_and_routes(tmp_path, capsys):
    """`turn vars` exposes the data-passing state through the local CLI."""
    import asyncio
    from pathlib import Path

    from turn.__main__ import parser, local_vars_command
    from turn.runner.events import EventBus
    from turn.runner.runner import Runner
    from turn.tests.mocks import MockHerdrAdapter, MockTerminalTransport
    from turn.workers.deterministic_worker import DeterministicWorker
    from turn.workers.registry import WorkerRegistry

    store, root = await _project(tmp_path, name="cli vars")
    (producer,) = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(
                key="produce",
                objective="Produce",
                executor="deterministic",
                provides=["spec"],
                generated_prompt='{"outcome": "COMPLETE", "summary": "ok", "outputs": {"spec": "v1"}, "route": "small"}',
            ),
        ]),
        enforce_organization_audit=False,
    )
    await store.set_auto_run(root.id, True)
    registry = WorkerRegistry()
    registry.register(DeterministicWorker())
    runner = Runner(
        store,
        registry,
        EventBus(),
        Settings(data_dir=str(tmp_path / "turn"), projects_dir=str(tmp_path / "projects")),
        herdr_adapter=MockHerdrAdapter(),
        terminal_transport=MockTerminalTransport(),
    )
    await runner.start()
    try:
        for _ in range(200):
            if (await store.get_node(producer.id)).status is NodeStatus.COMPLETE:
                break
            await asyncio.sleep(0.05)
    finally:
        await runner.stop()
        await store.dispose()

    state_file = tmp_path / "projects" / "cli-vars" / ".turn" / "state.json"
    assert state_file.exists()
    args = parser().parse_args(["vars", str(root.id), "--state-file", str(state_file)])
    await local_vars_command(args)
    out = capsys.readouterr().out
    assert f"node {producer.id}" in out
    assert "provides: spec" in out
    assert "output spec = v1" in out
    assert "route_taken: small" in out
