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
    PlanResult,
    VerificationDecision,
    VerificationResult,
    parse_follows_reference,
)
from turn.domain.organization import ManagerPhase, OrganizationScale


@dataclass
class Indexes:
    node_by_id: dict
    children: dict       # CONTAINS: anchor -> [owned node]
    parents: dict        # owned node -> anchor
    predecessors: dict   # FOLLOWS: next node -> [previous node]
    successors: dict     # FOLLOWS: previous node -> [next node]
    follows_routes: dict  # (src, dst) -> route label | None for FOLLOWS edges


def workflow_leaves(plan: PlanResult) -> dict[str | None, tuple[str, ...]]:
    """Return terminal workflow keys for each composition boundary.

    ``CONTAINS`` describes ownership, not workflow progress. A leaf is
    therefore a node with no local ``FOLLOWS`` successor. Boundaries are
    checked independently so a composed anchor may own its own one-leaf
    workflow while still being one stage in its parent's workflow.
    """
    keys = {node.key for node in plan.nodes}
    successors: dict[str, set[str]] = {key: set() for key in keys}
    boundaries: dict[str | None, list[str]] = {}
    for node in plan.nodes:
        boundaries.setdefault(node.parent_key, []).append(node.key)
        for predecessor in node.follows:
            predecessor_key, _ = parse_follows_reference(predecessor)
            if predecessor_key in keys:
                successors[predecessor_key].add(node.key)
    for edge in plan.edges:
        if edge.type is EdgeType.FOLLOWS and edge.src in keys and edge.dst in keys:
            successors[edge.src].add(edge.dst)
    return {
        boundary: tuple(key for key in members if not successors[key])
        for boundary, members in boundaries.items()
    }


def validate_single_workflow_leaf(plan: PlanResult) -> None:
    """Reject a non-empty boundary that leaves multiple workflow terminals.

    This is deliberately the smallest submission-time shape guard. It does
    not prescribe the final role, require a particular number of branches, or
    reject a valid one-node plan. It only catches the fundamental planning
    mistake of fanning out and stopping without a single fan-in destination.
    Empty plans remain valid no-op/document-only handoffs.
    """
    invalid = [
        (boundary, leaves)
        for boundary, leaves in workflow_leaves(plan).items()
        if len(leaves) != 1
    ]
    if not invalid:
        return
    details = "; ".join(
        f"{boundary or 'root'} has {len(leaves)} leaves ({', '.join(leaves)})"
        for boundary, leaves in invalid
    )
    raise ValueError(
        "each non-empty workflow boundary must end in exactly one leaf; "
        + details
    )


def build_indexes(nodes: list[Node], edges: list[Edge]) -> Indexes:
    node_by_id = {n.id: n for n in nodes}
    children: dict = {}
    parents: dict = {}
    predecessors: dict = {}
    successors: dict = {}
    follows_routes: dict = {}
    for e in edges:
        if e.type == EdgeType.CONTAINS:
            children.setdefault(e.src, []).append(e.dst)
            parents[e.dst] = e.src
        elif e.type == EdgeType.FOLLOWS:
            predecessors.setdefault(e.dst, []).append(e.src)
            successors.setdefault(e.src, []).append(e.dst)
            follows_routes[(e.src, e.dst)] = e.route
    return Indexes(node_by_id, children, parents, predecessors, successors, follows_routes)


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
    """Return every node that contains a nested workflow."""
    return set(idx.children)


def _requires_manager_acceptance(node: Node) -> bool:
    """Whether a derived container completion must be approved by its manager.

    Material planner boundaries are organizations, not ordinary structural
    containers. Their completed leaves only prove that the current frontier
    settled; the boundary is a usable predecessor after its manager accepts
    the charter and evidence.
    """
    contract = node.organization_contract
    return (
        node.executor == "planner"
        and contract is not None
        and contract.scale is not OrganizationScale.FOCUSED
    )


def is_runnable(
    node_id,
    idx: Indexes,
    effective_status: dict | None = None,
) -> tuple[bool, str]:
    """A node is runnable when active, sequence satisfied, inputs present."""
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
    for p in idx.predecessors.get(node_id, []):
        pn = idx.node_by_id.get(p)
        predecessor_status = (
            effective_status.get(p, pn.status)
            if effective_status is not None and pn is not None
            else pn.status if pn is not None else None
        )
        if pn is None or predecessor_status != NodeStatus.COMPLETE:
            return False, "sequence incomplete"
        # Decision-based routing: a labeled FOLLOWS edge stays active only
        # when its source selected that route. An unlabeled edge remains
        # unconditional, and a source that took no route keeps every branch
        # open (backward-compatible default).
        route = idx.follows_routes.get((p, node_id))
        if route is not None and pn.route_taken is not None and pn.route_taken != route:
            return False, f"route '{route}' not taken (source took '{pn.route_taken}')"
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
    should reimplement CONTAINS/FOLLOWS traversal.  This object is pure and
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

    def predecessors(self, node_id) -> list[Node]:
        return [
            self.indexes.node_by_id[predecessor]
            for predecessor in self.indexes.predecessors.get(node_id, [])
            if predecessor in self.indexes.node_by_id
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

    Persistent graph edges describe forward workflow semantics and must remain a DAG.
    A rejection temporarily sends its selected target back to the worker, so
    that direction is represented separately as a render-only flow edge. The
    edge is present only while that target is the next runnable or active step;
    once the target completes, normal forward flow is restored and this
    projection becomes empty. Verifiers use their only predecessor when no
    explicit target is supplied; a verifier with multiple predecessors must
    use ``target_node_id`` to point at the node that needs correction.
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
    cycle. Omitting it selects the reviewer's only predecessor; a verifier with
    multiple predecessors must identify its correction target explicitly.
    """
    if decision.target_node_id is not None:
        target = indexes.node_by_id.get(decision.target_node_id)
        if target is None or target.id == reviewer.id:
            return None
        return target

    target_ids = list(dict.fromkeys(indexes.predecessors.get(reviewer.id, [])))
    if len(target_ids) != 1:
        return None
    return indexes.node_by_id.get(target_ids[0])


def resolve_variables(
    node_id,
    idx: Indexes,
    consumes: list[str],
) -> dict[str, str]:
    """Resolve declared ``consumes`` names from upstream predecessor outputs.

    Predecessors are visited nearest-first (BFS over FOLLOWS edges), so the
    closest producer wins when several upstream nodes publish the same name.
    Unresolved names are simply absent from the result — callers render them
    as missing instead of guessing a value.
    """
    if not consumes:
        return {}
    wanted = set(consumes)
    resolved: dict[str, str] = {}
    visited: set = set()
    queue: list = list(idx.predecessors.get(node_id, []))
    while queue and wanted:
        current_id = queue.pop(0)
        if current_id in visited:
            continue
        visited.add(current_id)
        producer = idx.node_by_id.get(current_id)
        if producer is not None and producer.outputs:
            for name in list(wanted):
                value = producer.outputs.get(name)
                if value is not None:
                    resolved[name] = value
                    wanted.discard(name)
        queue.extend(idx.predecessors.get(current_id, []))
    return resolved


def evaluate(nodes: list[Node], edges: list[Edge]) -> Evaluation:
    idx = build_indexes(nodes, edges)
    status: dict = {}
    runnable: set = set()
    progress: dict = {}
    reason: dict = {}

    # A planner/subplanner remains EXPANDED in persisted state because it is a
    # container, but its completion is derived from its descendant leaves.
    # Compute that projection before checking sequence joins so an ordinary
    # integrator can follow a completed architectural branch.
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
        settled = active_leaves and len(done) == len(active_leaves)
        manager_accepted = n.manager_phase is ManagerPhase.ACCEPTED
        status[n.id] = (
            NodeStatus.COMPLETE
            if settled and (
                not _requires_manager_acceptance(n) or manager_accepted
            )
            else n.status
        )

    for n in nodes:
        ok, why = is_runnable(n.id, idx, status)
        if ok:
            runnable.add(n.id)
            status[n.id] = NodeStatus.RUNNABLE
            reason[n.id] = "ok"
        else:
            if (
                n.id in container_ids
                and _requires_manager_acceptance(n)
                and n.manager_phase is not ManagerPhase.ACCEPTED
                and n.status not in {
                    NodeStatus.FAILED,
                    NodeStatus.BLOCKED,
                    NodeStatus.CANCELLED,
                }
            ):
                # A persisted COMPLETE can be stale when the manager review
                # is still pending. Keep the boundary visibly expanded so a
                # parent integrator cannot consume an unaccepted organization.
                status[n.id] = NodeStatus.EXPANDED
            elif n.status in (
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
        manager_gate_open = (
            not _requires_manager_acceptance(n)
            or n.manager_phase is ManagerPhase.ACCEPTED
        )
        if total > 0 and len(done) == total and manager_gate_open:
            status[n.id] = NodeStatus.COMPLETE
        elif n.status not in (
            NodeStatus.CANCELLED,
            NodeStatus.FAILED,
            NodeStatus.BLOCKED,
        ):
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
    """Return nodes ordered so predecessors come before successors.

    Used when dispatching, so a fan-in is respected even within a
    single scheduling pass. Falls back to insertion order on cycles.
    """
    idx = build_indexes(nodes, edges)
    visited: set = set()
    order: list[Node] = []

    def visit(nid):
        if nid in visited:
            return
        visited.add(nid)
        for p in idx.predecessors.get(nid, []):
            visit(p)
        if nid in idx.node_by_id:
            order.append(idx.node_by_id[nid])

    for n in nodes:
        visit(n.id)
    return order
