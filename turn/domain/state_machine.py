"""Pure UI/lifecycle projection for nodes.

The local state store records execution facts (status, paused, review flags).  This
module is the single policy that turns those facts into coherent user-visible
states and permitted actions.  Both REST responses and tests use it, keeping
the browser free of orchestration guesses.
"""
from __future__ import annotations

from dataclasses import dataclass

from turn.domain.schemas import Node, NodeAction, NodeStatus, NodeUIState


UIState = NodeUIState
Action = NodeAction


@dataclass(frozen=True)
class NodePresentation:
    state: UIState
    actions: tuple[Action, ...]
    reason: str | None = None


def present_node(
    node: Node,
    *,
    blocked_reason: str | None = None,
    subtree_needs_review: bool = False,
) -> NodePresentation:
    """Project one node into a stable UI state and guarded action set."""
    common = (Action.EDIT, Action.REGENERATE)
    if node.needs_review and not node.merge_accepted:
        return NodePresentation(
            UIState.REVIEW,
            (Action.ACCEPT, Action.REJECT, *common),
            "Waiting for your review",
        )
    if subtree_needs_review:
        return NodePresentation(UIState.REVIEW, common, "A descendant awaits review")
    if node.paused:
        # Resuming never implicitly resets completion or acceptance. The runner
        # will only schedule it when the graph evaluation says it is runnable.
        return NodePresentation(UIState.PAUSED, (Action.RESUME, *common))
    if node.status == NodeStatus.RUNNING:
        return NodePresentation(UIState.RUNNING, (Action.CANCEL,))
    if node.status == NodeStatus.FAILED:
        return NodePresentation(UIState.FAILED, (Action.RETRY, *common))
    if node.status == NodeStatus.CANCELLED:
        return NodePresentation(UIState.CANCELLED, (Action.RUN, *common))
    if node.merge_accepted:
        return NodePresentation(UIState.ACCEPTED, common)
    if node.status == NodeStatus.COMPLETE:
        return NodePresentation(UIState.COMPLETE, common)
    if node.status == NodeStatus.EXPANDED:
        return NodePresentation(UIState.CONTAINER, (Action.PAUSE, *common))
    missing = [i for i in node.required_inputs if i.satisfied_by is None]
    if missing:
        return NodePresentation(
            UIState.WAITING_INPUT,
            (Action.PROVIDE_INPUT, Action.PAUSE, *common),
            f"Needs {len(missing)} human input{'s' if len(missing) != 1 else ''}",
        )
    if node.status == NodeStatus.BLOCKED:
        return NodePresentation(
            UIState.WAITING_DEPENDENCY,
            (Action.PAUSE, *common),
            blocked_reason,
        )
    if node.status == NodeStatus.RUNNABLE:
        return NodePresentation(UIState.READY, (Action.RUN, Action.PAUSE, *common))
    return NodePresentation(UIState.QUEUED, (Action.PAUSE, *common))


def review_blocked_ids(nodes: list[Node]) -> set:
    """Return every node whose subtree contains an unaccepted review."""
    children: dict = {}
    by_id = {n.id: n for n in nodes}
    for n in nodes:
        if n.parent_id is not None:
            children.setdefault(n.parent_id, []).append(n.id)
    blocked: set = set()
    visiting: set = set()

    def visit(node_id) -> bool:
        if node_id in visiting:
            return False
        visiting.add(node_id)
        node = by_id.get(node_id)
        pending = bool(node and node.parent_id and node.needs_review and not node.merge_accepted)
        pending = any(visit(child) for child in children.get(node_id, [])) or pending
        if pending:
            blocked.add(node_id)
        return pending

    for node in nodes:
        if node.parent_id is None:
            visit(node.id)
    return blocked
