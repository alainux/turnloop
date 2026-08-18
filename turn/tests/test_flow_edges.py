from __future__ import annotations

import uuid

from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    Edge,
    EdgeType,
    FlowEdgeType,
    Node,
    NodeStatus,
    VerificationDecision,
    VerificationResult,
    HarnessKind,
)
from turn.graph.logic import derive_flow_edges


def _scenario(
    *,
    target_status: NodeStatus = NodeStatus.RUNNABLE,
    verifier_type: AgentType = AgentType.VERIFIER,
    decision: VerificationDecision = VerificationDecision.REJECT,
    extra_target: bool = False,
):
    project_id = uuid.uuid4()
    target = Node(
        id=uuid.uuid4(),
        project_id=project_id,
        objective="Repair the implementation",
        status=target_status,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    verifier = Node(
        id=uuid.uuid4(),
        project_id=project_id,
        objective="Verify the implementation",
        status=NodeStatus.PENDING,
        agent=AgentConfig(harness=HarnessKind.ECHO, type_id=verifier_type),
        verification=VerificationResult(
            decision=decision,
            summary="The implementation needs another pass",
        ),
    )
    nodes = [target, verifier]
    edges = [Edge(src=target.id, dst=verifier.id, type=EdgeType.FOLLOWS)]
    if extra_target:
        other = Node(
            id=uuid.uuid4(),
            project_id=project_id,
            objective="Another prerequisite",
            status=NodeStatus.RUNNABLE,
            agent=AgentConfig(harness=HarnessKind.ECHO),
        )
        nodes.append(other)
        edges.append(Edge(src=other.id, dst=verifier.id, type=EdgeType.FOLLOWS))
    return nodes, edges, target, verifier


def test_rejected_verifier_derives_a_stable_transient_return_edge():
    nodes, edges, target, verifier = _scenario()
    edges.append(Edge(src=target.id, dst=verifier.id, type=EdgeType.FOLLOWS))

    first = derive_flow_edges(nodes, edges)
    second = derive_flow_edges(nodes, edges)

    assert len(first) == 1
    assert first[0].type is FlowEdgeType.RETURN
    assert (first[0].src, first[0].dst) == (verifier.id, target.id)
    assert first[0].id == second[0].id
    assert all(edge.type is EdgeType.FOLLOWS for edge in edges)


def test_return_edge_stays_visible_while_repair_is_active_and_disappears_afterward():
    nodes, edges, target, _ = _scenario(target_status=NodeStatus.RUNNING)
    assert len(derive_flow_edges(nodes, edges)) == 1

    target.status = NodeStatus.COMPLETE
    assert derive_flow_edges(nodes, edges) == []


def test_return_edge_requires_a_rejected_verifier_and_one_active_target():
    nodes, edges, _, _ = _scenario(decision=VerificationDecision.APPROVE)
    assert derive_flow_edges(nodes, edges) == []

    nodes, edges, _, _ = _scenario(verifier_type=AgentType.EXECUTOR)
    assert len(derive_flow_edges(nodes, edges)) == 1

    nodes, edges, _, _ = _scenario(extra_target=True)
    assert derive_flow_edges(nodes, edges) == []


def test_return_edge_is_not_shown_when_the_target_is_not_the_next_worker_step():
    for status in (NodeStatus.PENDING, NodeStatus.BLOCKED, NodeStatus.COMPLETE, NodeStatus.FAILED):
        nodes, edges, target, _ = _scenario(target_status=status)
        effective = {target.id: NodeStatus.RUNNABLE} if status is NodeStatus.PENDING else None
        expected = 1 if status is NodeStatus.PENDING else 0
        assert len(derive_flow_edges(nodes, edges, effective)) == expected


def test_any_node_can_return_to_an_explicit_arbitrary_target():
    nodes, edges, dependency, reviewer = _scenario(verifier_type=AgentType.EXECUTOR)
    arbitrary = Node(
        project_id=dependency.project_id,
        objective="Repair the earlier foundation",
        status=NodeStatus.RUNNABLE,
        agent=AgentConfig(harness=HarnessKind.ECHO),
    )
    nodes.append(arbitrary)
    reviewer.verification = VerificationResult(
        decision=VerificationDecision.REJECT,
        summary="The earlier foundation needs correction",
        target_node_id=arbitrary.id,
    )

    flow = derive_flow_edges(nodes, edges)

    assert len(flow) == 1
    assert (flow[0].src, flow[0].dst) == (reviewer.id, arbitrary.id)
