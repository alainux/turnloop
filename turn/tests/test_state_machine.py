from __future__ import annotations

import pytest

from turn.domain.schemas import InputSpec, Node, NodeStatus
from turn.domain.state_machine import Action, UIState, present_node, review_blocked_ids
from turn.graph.logic import evaluate
from turn.runner.recovery import DamageKind, backoff_seconds, classify_failure, should_retry


@pytest.mark.parametrize(
    "node,expected,actions",
    [
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNING), UIState.RUNNING, {Action.CANCEL}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNABLE), UIState.READY, {Action.RUN, Action.PAUSE}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.FAILED), UIState.FAILED, {Action.RETRY}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.CANCELLED), UIState.CANCELLED, {Action.RUN}),
        (Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.COMPLETE, merge_accepted=True), UIState.ACCEPTED, {Action.EDIT}),
    ],
)
def test_node_state_matrix(node, expected, actions):
    projected = present_node(node)
    assert projected.state == expected
    assert actions.issubset(set(projected.actions))


def test_pause_and_review_override_execution_status_without_destroying_it():
    paused = Node(project_id="00000000-0000-0000-0000-000000000001", objective="x", status=NodeStatus.RUNNABLE, paused=True)
    assert present_node(paused).state == UIState.PAUSED
    assert paused.status == NodeStatus.RUNNABLE

    review = paused.model_copy(update={"paused": False, "status": NodeStatus.COMPLETE, "needs_review": True})
    state = present_node(review)
    assert state.state == UIState.REVIEW
    assert {Action.ACCEPT, Action.REJECT}.issubset(set(state.actions))


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


def test_review_propagates_to_parent_only_as_a_projection():
    root = Node(id="00000000-0000-0000-0000-000000000001", project_id="00000000-0000-0000-0000-000000000001", objective="root", status=NodeStatus.EXPANDED)
    child = Node(project_id=root.id, parent_id=root.id, objective="child", status=NodeStatus.COMPLETE, needs_review=True)
    blocked = review_blocked_ids([root, child])
    assert root.id in blocked and child.id in blocked
    assert present_node(root, subtree_needs_review=True).state == UIState.REVIEW
    assert root.status == NodeStatus.EXPANDED


def test_parent_review_owner_never_exposes_human_accept_reject():
    node = Node(project_id="00000000-0000-0000-0000-000000000001", parent_id="00000000-0000-0000-0000-000000000001", objective="child", status=NodeStatus.COMPLETE, needs_review=True)
    manual = present_node(node, review_owner="manual")
    parent = present_node(node, review_owner="parent")
    assert {Action.ACCEPT, Action.REJECT}.issubset(set(manual.actions))
    assert Action.ACCEPT not in parent.actions and Action.REJECT not in parent.actions


def test_unaccepted_dependency_blocks_dispatch_and_reopens_complete_parent():
    root = Node(id="00000000-0000-0000-0000-000000000001", project_id="00000000-0000-0000-0000-000000000001", objective="root", status=NodeStatus.COMPLETE)
    prerequisite = Node(id="00000000-0000-0000-0000-000000000002", project_id=root.id, parent_id=root.id, objective="review me", status=NodeStatus.COMPLETE, needs_review=True)
    dependent = Node(id="00000000-0000-0000-0000-000000000003", project_id=root.id, parent_id=root.id, objective="use accepted result", status=NodeStatus.PENDING)
    from turn.domain.schemas import Edge, EdgeType
    edges = [
        Edge(src=root.id, dst=prerequisite.id, type=EdgeType.CONTAINS),
        Edge(src=root.id, dst=dependent.id, type=EdgeType.CONTAINS),
        Edge(src=prerequisite.id, dst=dependent.id, type=EdgeType.DEPENDS_ON),
    ]
    result = evaluate([root, prerequisite, dependent], edges)
    assert dependent.id not in result.runnable
    assert result.blocked_reason[dependent.id] == "dependency awaits review"
    assert result.status[root.id] == NodeStatus.EXPANDED


def test_recovery_classification_and_backoff():
    assert classify_failure("context window exceeded") == DamageKind.CONTEXT_PRESSURE
    assert classify_failure("429 rate limit") == DamageKind.RATE_LIMIT
    assert classify_failure("service overloaded") == DamageKind.CHOKED
    assert should_retry("overloaded", False, True)
    assert not should_retry("invalid credentials", False, True)
    assert [backoff_seconds(n, 500) for n in (1, 2, 3)] == [0.5, 1.0, 2.0]
