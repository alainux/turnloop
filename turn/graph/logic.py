"""Pure graph reasoning over Nodes + Edges.

No I/O here — callers load the relevant slice of the workgraph from the store
and pass it in. Keeping this pure makes runnability and progress derivable
without leaking orchestration concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import Optional

from turn.domain.schemas import (
    Edge,
    EdgeType,
    FlowEdge,
    FlowEdgeType,
    Graph,
    Node,
    NodeStatus,
    VerificationDecision,
    VerificationResult,
)


@dataclass
class Indexes:
    node_by_id: dict
    children: dict  # CONTAINS: parent -> [child]
    parents: dict   # child -> parent
    deps: dict      # dependent -> [prerequisite]
    dependents: dict  # prerequisite -> [dependent]


def build_indexes(nodes: list[Node], edges: list[Edge]) -> Indexes:
    node_by_id = {n.id: n for n in nodes}
    children: dict = {}
    parents: dict = {}
    deps: dict = {}
    dependents: dict = {}
    for e in edges:
        if e.type == EdgeType.CONTAINS:
            children.setdefault(e.src, []).append(e.dst)
            parents[e.dst] = e.src
        elif e.type == EdgeType.DEPENDS_ON:
            deps.setdefault(e.dst, []).append(e.src)
            dependents.setdefault(e.src, []).append(e.dst)
    return Indexes(node_by_id, children, parents, deps, dependents)


def _collect_descendants(idx: Indexes, node_id) -> list[Node]:
    out: list[Node] = []
    stack = list(idx.children.get(node_id, []))
    seen = set()
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        n = idx.node_by_id.get(nid)
        if n is None:
            continue
        out.append(n)
        stack.extend(idx.children.get(nid, []))
    return out


def _execution_container_ids(idx: Indexes) -> set:
    """Return every node that contains graph children.

    Verification has no special containment semantics. A verifier is a
    sibling at its planning boundary and becomes ordered solely by its
    ordinary DEPENDS_ON edge.
    """
    return set(idx.children)


def is_runnable(
    node_id,
    idx: Indexes,
    effective_status: dict | None = None,
) -> tuple[bool, str]:
    """A node is runnable when active, not paused, deps satisfied, inputs present."""
    node = idx.node_by_id.get(node_id)
    if node is None:
        return False, "missing"
    if node.paused:
        return False, "paused"
    parent_id = idx.parents.get(node_id)
    seen = set()
    while parent_id is not None and parent_id not in seen:
        seen.add(parent_id)
        parent = idx.node_by_id.get(parent_id)
        if parent is not None and parent.paused:
            return False, "ancestor paused"
        parent_id = idx.parents.get(parent_id)
    if node.status in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED):
        return False, "terminal"
    if node.status == NodeStatus.RUNNING:
        return False, "in flight"
    if node.status == NodeStatus.EXPANDED:
        return False, "container"
    for p in idx.deps.get(node_id, []):
        pn = idx.node_by_id.get(p)
        prerequisite_status = (
            effective_status.get(p, pn.status)
            if effective_status is not None and pn is not None
            else pn.status if pn is not None else None
        )
        if pn is None or prerequisite_status != NodeStatus.COMPLETE:
            return False, "dependency incomplete"
    for inp in node.required_inputs:
        if inp.satisfied_by is None:
            return False, f"missing input: {inp.label}"
    return True, "ok"


@dataclass
class Evaluation:
    # effective status per node (RUNNABLE / BLOCKED for active leaves;
    # EXPANDED / COMPLETE for containers; terminal states preserved)
    status: dict
    runnable: set
    progress: dict  # node_id -> 0..1 (containers only)
    blocked_reason: dict  # node_id -> reason when not runnable


class GraphWalker:
    """Read-only graph traversal service.

    The store owns persistence and the runner owns scheduling, but neither
    should reimplement CONTAINS/DEPENDS_ON traversal.  This object is pure and
    therefore can be used unchanged by the server, CLI, and deterministic
    tests.
    """

    def __init__(self, graph: Graph | list[Node], edges: list[Edge] | None = None):
        if isinstance(graph, Graph):
            nodes = graph.nodes
            edges = graph.edges
        else:
            nodes = graph
            if edges is None:
                raise ValueError("GraphWalker requires graph edges")
        self.nodes = tuple(nodes)
        self.edges = tuple(edges)
        self.indexes = build_indexes(list(self.nodes), list(self.edges))

    def evaluate(self) -> Evaluation:
        return evaluate(list(self.nodes), list(self.edges))

    def ancestors(self, node_id) -> list[Node]:
        return ancestry_path(self.indexes, node_id)

    def descendants(self, node_id) -> list[Node]:
        return _collect_descendants(self.indexes, node_id)

    def prerequisites(self, node_id) -> list[Node]:
        return [
            self.indexes.node_by_id[dependency]
            for dependency in self.indexes.deps.get(node_id, [])
            if dependency in self.indexes.node_by_id
        ]

    def depth(self, node_id) -> int:
        return len(self.ancestors(node_id))

    def topological(self) -> list[Node]:
        return topo_order(list(self.nodes), list(self.edges))


def derive_flow_edges(
    nodes: list[Node],
    edges: list[Edge],
    effective_status: dict | None = None,
) -> list[FlowEdge]:
    """Derive transient edges for a workflow whose next step changed.

    Persistent graph edges describe dependency semantics and must remain a DAG.
    A rejection temporarily sends its selected target back to the worker, so
    that direction is represented separately as a render-only flow edge. The
    edge is present only while that target is the next runnable or active step;
    once the target completes, normal forward flow is restored and this
    projection becomes empty. Verifiers use their single dependency when no
    explicit target is supplied; any node can use ``target_node_id`` to point
    at another node.
    """
    indexes = build_indexes(nodes, edges)
    statuses = effective_status or {node.id: node.status for node in nodes}
    flow_edges: list[FlowEdge] = []
    for reviewer in nodes:
        decision = reviewer.verification
        if decision is None or decision.decision is not VerificationDecision.REJECT:
            continue
        target = rejection_target(reviewer, decision, indexes)
        if target is None:
            continue
        if statuses.get(target.id, target.status) not in {
            NodeStatus.RUNNABLE,
            NodeStatus.RUNNING,
        }:
            continue
        flow_edges.append(
            FlowEdge(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"turn:flow:return:{reviewer.id}:{target.id}",
                ),
                src=reviewer.id,
                dst=target.id,
                type=FlowEdgeType.RETURN,
            )
        )
    return flow_edges


def rejection_target(
    reviewer: Node,
    decision: VerificationResult,
    indexes: Indexes,
) -> Node | None:
    """Resolve a review decision to one node in the current workgraph.

    The explicit target is deliberately an ordinary node id rather than a
    persistent edge: returning work must not mutate the DAG or introduce a
    cycle. Omitting it preserves the original verifier behavior by selecting
    the reviewer's one dependency.
    """
    if decision.target_node_id is not None:
        target = indexes.node_by_id.get(decision.target_node_id)
        if target is None or target.id == reviewer.id:
            return None
        return target

    target_ids = list(dict.fromkeys(indexes.deps.get(reviewer.id, [])))
    if len(target_ids) != 1:
        return None
    return indexes.node_by_id.get(target_ids[0])


def evaluate(nodes: list[Node], edges: list[Edge]) -> Evaluation:
    idx = build_indexes(nodes, edges)
    status: dict = {}
    runnable: set = set()
    progress: dict = {}
    reason: dict = {}

    # A planner/subplanner remains EXPANDED in persisted state because it is a
    # container, but its completion is derived from its descendant leaves.
    # Compute that projection before checking dependency joins so an ordinary
    # integrator can depend on a completed architectural branch.
    container_ids = _execution_container_ids(idx)
    for n in nodes:
        if n.id not in container_ids:
            status[n.id] = n.status
            continue
        desc = _collect_descendants(idx, n.id)
        leaves = [d for d in desc if d.id not in container_ids]
        active_leaves = [d for d in leaves if d.status != NodeStatus.CANCELLED]
        done = [
            d for d in active_leaves
            if d.status == NodeStatus.COMPLETE
        ]
        status[n.id] = (
            NodeStatus.COMPLETE
            if active_leaves and len(done) == len(active_leaves)
            else n.status
        )

    for n in nodes:
        ok, why = is_runnable(n.id, idx, status)
        if ok:
            runnable.add(n.id)
            status[n.id] = NodeStatus.RUNNABLE
            reason[n.id] = "ok"
        else:
            if n.status in (
                NodeStatus.COMPLETE,
                NodeStatus.FAILED,
                NodeStatus.CANCELLED,
                NodeStatus.RUNNING,
            ):
                status[n.id] = n.status
            elif n.status == NodeStatus.EXPANDED:
                # Preserve a derived COMPLETE projection for containers. The
                # persisted node stays EXPANDED so it can retain descendants.
                status[n.id] = (
                    NodeStatus.COMPLETE
                    if status.get(n.id) == NodeStatus.COMPLETE
                    else NodeStatus.EXPANDED
                )
            else:
                status[n.id] = NodeStatus.BLOCKED
            reason[n.id] = why

    # derive container progress + completion from descendants
    for n in nodes:
        if n.id not in container_ids:
            continue
        desc = _collect_descendants(idx, n.id)
        leaves = [d for d in desc if d.id not in container_ids]
        active_leaves = [d for d in leaves if d.status != NodeStatus.CANCELLED]
        done = [
            d for d in active_leaves
            if d.status == NodeStatus.COMPLETE
        ]
        total = len(active_leaves)
        if total == 0:
            progress[n.id] = 1.0 if all(d.status == NodeStatus.COMPLETE for d in leaves) else 0.0
        else:
            progress[n.id] = len(done) / total
        if total > 0 and len(done) == total:
            status[n.id] = NodeStatus.COMPLETE
        elif n.status not in (NodeStatus.CANCELLED, NodeStatus.FAILED):
            # Container completion is derived, never sticky. A newly forked,
            # input-blocked descendant reopens its parents
            # until that work is genuinely settled.
            status[n.id] = NodeStatus.EXPANDED

    return Evaluation(status=status, runnable=runnable, progress=progress, blocked_reason=reason)


def ancestry_path(idx: Indexes, node_id) -> list[Node]:
    """Return ancestors root..parent for a node."""
    out: list[Node] = []
    cur = idx.parents.get(node_id)
    while cur is not None and cur in idx.node_by_id:
        out.append(idx.node_by_id[cur])
        cur = idx.parents.get(cur)
    out.reverse()
    return out


def topo_order(nodes: list[Node], edges: list[Edge]) -> list[Node]:
    """Return nodes ordered so prerequisites come before dependents.

    Used when dispatching, so a dependency join is respected even within a
    single scheduling pass. Falls back to insertion order on cycles.
    """
    idx = build_indexes(nodes, edges)
    visited: set = set()
    order: list[Node] = []

    def visit(nid):
        if nid in visited:
            return
        visited.add(nid)
        for p in idx.deps.get(nid, []):
            visit(p)
        if nid in idx.node_by_id:
            order.append(idx.node_by_id[nid])

    for n in nodes:
        visit(n.id)
    return order
