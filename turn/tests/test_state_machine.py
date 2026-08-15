from __future__ import annotations

import pytest

from turn.domain.schemas import InputSpec, Node, NodeStatus
from turn.domain.state_machine import Action, UIState, present_node
from turn.graph.logic import evaluate
from turn.runner.recovery import DamageKind, backoff_seconds, classify_failure, should_retry


@pytest.mark.parametrize(
    "node,expected,actions",
    [
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNING), UIState.RUNNING, {Action.CANCEL}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNABLE), UIState.READY, {Action.RUN, Action.PAUSE}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.FAILED), UIState.FAILED, {Action.RETRY}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.CANCELLED), UIState.CANCELLED, {Action.RUN}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.COMPLETE), UIState.COMPLETE, {Action.EDIT}),
    ],
)
def test_node_state_matrix(node, expected, actions):
    projected = present_node(node)
    assert projected.state == expected
    assert actions.issubset(set(projected.actions))


def test_running_nodes_expose_only_stop_to_every_surface():
    node = Node(
        project_id="00000000-0000-0000-0000-000000000001",
        objective="in flight",
        status=NodeStatus.RUNNING,
    )
    assert present_node(node).actions == (Action.CANCEL,)


def test_pause_overrides_execution_status_without_destroying_it():
    paused = Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNABLE, paused=True)
    assert present_node(paused).state == UIState.PAUSED
    assert paused.status == NodeStatus.RUNNABLE


def test_human_input_is_distinct_from_dependency_waiting():
    node = Node(
        project_id="00000000-0000-0000-0000-000000000001",
        objective="x",
        status=NodeStatus.BLOCKED,
        required_inputs=[InputSpec(id="scope", label="Choose scope")],
    )
    assert present_node(node).state == UIState.WAITING_INPUT
    dependency = node.model_copy(update={"required_inputs": []})
    assert present_node(dependency, blocked_reason="dependency incomplete").state == UIState.WAITING_DEPENDENCY


def test_graph_projection_never_reclassifies_a_running_node_as_runnable():
    node = Node(
        project_id="00000000-0000-0000-0000-000000000001",
        objective="in flight",
        status=NodeStatus.RUNNING,
    )
    result = evaluate([node], [])
    assert result.status[node.id] == NodeStatus.RUNNING
    assert node.id not in result.runnable


def test_incomplete_dependency_blocks_dispatch_and_reopens_complete_parent():
    root = Node(id="00000000-0000-0000-0000-000000000001", project_id="00000000-0000-0000-0000-000000000001", objective="root", status=NodeStatus.COMPLETE)
    prerequisite = Node(id="00000000-0000-0000-0000-000000000002", project_id=root.id, parent_id=root.id, objective="incomplete prerequisite", status=NodeStatus.PENDING)
    dependent = Node(id="00000000-0000-0000-0000-000000000003", project_id=root.id, parent_id=root.id, objective="use accepted result", status=NodeStatus.PENDING)
    from turn.domain.schemas import Edge, EdgeType
    edges = [
        Edge(src=root.id, dst=prerequisite.id, type=EdgeType.CONTAINS),
        Edge(src=root.id, dst=dependent.id, type=EdgeType.CONTAINS),
        Edge(src=prerequisite.id, dst=dependent.id, type=EdgeType.DEPENDS_ON),
    ]
    result = evaluate([root, prerequisite, dependent], edges)
    assert dependent.id not in result.runnable
    assert result.blocked_reason[dependent.id] == "dependency incomplete"
    assert result.status[root.id] == NodeStatus.EXPANDED


def test_completed_container_satisfies_integrator_dependency():
    root = Node(
        id="00000000-0000-0000-0000-000000000011",
        project_id="00000000-0000-0000-0000-000000000011",
        objective="root",
        status=NodeStatus.EXPANDED,
    )
    branch = Node(
        id="00000000-0000-0000-0000-000000000012",
        project_id=root.id,
        parent_id=root.id,
        objective="subplanner",
        status=NodeStatus.EXPANDED,
    )
    executor = Node(
        id="00000000-0000-0000-0000-000000000013",
        project_id=root.id,
        parent_id=branch.id,
        objective="executor",
        status=NodeStatus.COMPLETE,
    )
    integrator = Node(
        id="00000000-0000-0000-0000-000000000014",
        project_id=root.id,
        parent_id=root.id,
        objective="integrator",
        status=NodeStatus.PENDING,
    )
    from turn.domain.schemas import Edge, EdgeType
    edges = [
        Edge(src=root.id, dst=branch.id, type=EdgeType.CONTAINS),
        Edge(src=branch.id, dst=executor.id, type=EdgeType.CONTAINS),
        Edge(src=root.id, dst=integrator.id, type=EdgeType.CONTAINS),
        Edge(src=branch.id, dst=integrator.id, type=EdgeType.DEPENDS_ON),
    ]

    result = evaluate([root, branch, executor, integrator], edges)

    assert result.status[branch.id] == NodeStatus.COMPLETE
    assert integrator.id in result.runnable


def test_integrator_waits_for_every_nested_branch_output():
    root = Node(
        id="00000000-0000-0000-0000-000000000021",
        project_id="00000000-0000-0000-0000-000000000021",
        objective="root",
        status=NodeStatus.EXPANDED,
    )
    branch = Node(
        id="00000000-0000-0000-0000-000000000022",
        project_id=root.id,
        parent_id=root.id,
        objective="nested branch",
        status=NodeStatus.EXPANDED,
    )
    first = Node(
        id="00000000-0000-0000-0000-000000000023",
        project_id=root.id,
        parent_id=branch.id,
        objective="first output",
        status=NodeStatus.COMPLETE,
    )
    second = Node(
        id="00000000-0000-0000-0000-000000000024",
        project_id=root.id,
        parent_id=branch.id,
        objective="second output",
        status=NodeStatus.PENDING,
    )
    integrator = Node(
        id="00000000-0000-0000-0000-000000000025",
        project_id=root.id,
        parent_id=root.id,
        objective="final product",
        status=NodeStatus.PENDING,
    )
    from turn.domain.schemas import Edge, EdgeType

    edges = [
        Edge(src=root.id, dst=branch.id, type=EdgeType.CONTAINS),
        Edge(src=branch.id, dst=first.id, type=EdgeType.CONTAINS),
        Edge(src=branch.id, dst=second.id, type=EdgeType.CONTAINS),
        Edge(src=root.id, dst=integrator.id, type=EdgeType.CONTAINS),
        Edge(src=branch.id, dst=integrator.id, type=EdgeType.DEPENDS_ON),
    ]

    waiting = evaluate([root, branch, first, second, integrator], edges)
    assert waiting.status[branch.id] == NodeStatus.EXPANDED
    assert integrator.id not in waiting.runnable

    second.status = NodeStatus.COMPLETE
    settled = evaluate([root, branch, first, second, integrator], edges)
    assert settled.status[branch.id] == NodeStatus.COMPLETE
    assert integrator.id in settled.runnable


def test_recovery_classification_and_backoff():
    assert classify_failure("context window exceeded") == DamageKind.CONTEXT_PRESSURE
    assert classify_failure("429 rate limit") == DamageKind.RATE_LIMIT
    assert classify_failure("service overloaded") == DamageKind.CHOKED
    # Auto-respawn is disabled by design: a node is only re-run on an explicit
    # user action, so even a choked error is not retried unless the worker
    # also recommended it.
    assert not should_retry("overloaded", False, True)
    assert not should_retry("invalid credentials", False, True)
    assert [backoff_seconds(n, 500) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]
