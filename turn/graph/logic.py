"""Pure graph reasoning over Nodes + Edges.

No I/O here — callers load the relevant slice of the workgraph from the store
and pass it in. Keeping this pure makes runnability and progress derivable
without leaking orchestration concerns.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from turn.domain.schemas import Edge, EdgeType, Node, NodeStatus


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


def is_runnable(node_id, idx: Indexes) -> tuple[bool, str]:
    """A node is runnable when active, not paused, deps satisfied, inputs present."""
    node = idx.node_by_id.get(node_id)
    if node is None:
        return False, "missing"
    if node.paused:
        return False, "paused"
    if node.status in (NodeStatus.COMPLETE, NodeStatus.FAILED, NodeStatus.CANCELLED):
        return False, "terminal"
    if node.status == NodeStatus.EXPANDED:
        return False, "container"
    for p in idx.deps.get(node_id, []):
        pn = idx.node_by_id.get(p)
        if pn is None or pn.status != NodeStatus.COMPLETE:
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


def evaluate(nodes: list[Node], edges: list[Edge]) -> Evaluation:
    idx = build_indexes(nodes, edges)
    status: dict = {}
    runnable: set = set()
    progress: dict = {}
    reason: dict = {}

    for n in nodes:
        ok, why = is_runnable(n.id, idx)
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
                status[n.id] = NodeStatus.EXPANDED
            else:
                status[n.id] = NodeStatus.BLOCKED
            reason[n.id] = why

    # derive container progress + completion from descendants
    for n in nodes:
        if n.id not in idx.children:
            continue
        desc = _collect_descendants(idx, n.id)
        leaves = [d for d in desc if d.id not in idx.children]
        active_leaves = [d for d in leaves if d.status != NodeStatus.CANCELLED]
        done = [d for d in active_leaves if d.status == NodeStatus.COMPLETE]
        total = len(active_leaves)
        if total == 0:
            progress[n.id] = 1.0 if all(d.status == NodeStatus.COMPLETE for d in leaves) else 0.0
        else:
            progress[n.id] = len(done) / total
        if total > 0 and len(done) == total:
            status[n.id] = NodeStatus.COMPLETE

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
