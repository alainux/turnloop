"""Pure UI/lifecycle projection for nodes.

The local state store records execution facts (status and paused state). This
module is the single policy that turns those facts into coherent user-visible
states and permitted actions. Both REST responses and tests use it, keeping
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
    preparing: bool = False,
) -> NodePresentation:
    """Project one node into a stable UI state and guarded action set."""
    common = (Action.EDIT, Action.REGENERATE)
    if preparing:
        # A planner regeneration can be live while its persisted node remains
        # EXPANDED until the replacement plan is applied. The runner owns the
        # live-task fact, so expose Stop for that state instead of leaving the
        # user with a misleading Run again action.
        state = UIState.RUNNING if node.status in {
            NodeStatus.RUNNING,
            NodeStatus.EXPANDED,
        } else UIState.PREPARING
        return NodePresentation(state, (Action.CANCEL,))
    if node.paused:
        return NodePresentation(UIState.PAUSED, (Action.RESUME, *common))
    if node.status == NodeStatus.RUNNING:
        return NodePresentation(UIState.RUNNING, (Action.CANCEL,))
    if node.status == NodeStatus.FAILED:
        return NodePresentation(UIState.FAILED, (Action.RETRY, *common))
    if node.status == NodeStatus.CANCELLED:
        return NodePresentation(UIState.CANCELLED, (Action.RUN, *common))
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
