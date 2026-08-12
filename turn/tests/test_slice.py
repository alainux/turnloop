"""Vertical slice test.

Proves the smallest useful path works across an unrelated objective:

  prompt -> root node -> initial planner -> visible graph -> execute ready
  leaves -> block -> provide input -> complete -> edit parent -> regenerate
  descendants -> execution resumes.

Runs entirely offline against a temp SQLite store with deterministic workers.
"""
from __future__ import annotations

import asyncio
import tempfile
import uuid

from turn.config import settings
from turn.db.store import Store
from turn.domain.schemas import (
    EdgeSpec,
    EdgeType,
    InputKind,
    InputSpec,
    NodeStatus,
    PlanResult,
    NodeSpec,
)
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.base import NodeExecutionContext, Planner
from turn.workers.codex_worker import CodexWorker
from turn.workers.echo_worker import EchoWorker
from turn.workers.registry import WorkerRegistry
from turn.workers.shell_worker import ShellWorker


# --- deterministic planner ------------------------------------------------


class ScriptedPlanner(Planner):
    name = "scripted"

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        # a: runnable, completes
        # b: depends on a, blocks requesting a decision
        # c: depends on b, completes (dependency join)
        return PlanResult(
            nodes=[
                NodeSpec(
                    key="a",
                    objective="Investigate the objective",
                    executor="echo",
                    generated_prompt='{"outcome":"COMPLETE","summary":"investigated"}',
                ),
                NodeSpec(
                    key="b",
                    objective="Confirm the key decision",
                    executor="echo",
                    depends_on=["a"],
                    required_inputs=[
                        InputSpec(
                            id="decision_x",
                            label="Which direction?",
                            kind=InputKind.DECISION,
                        )
                    ],
                    generated_prompt='{"outcome":"COMPLETE","summary":"decided"}',
                ),
                NodeSpec(
                    key="c",
                    objective="Produce the deliverable",
                    executor="echo",
                    depends_on=["b"],
                    generated_prompt='{"outcome":"COMPLETE","summary":"produced"}',
                ),
            ],
            edges=[
                EdgeSpec(type=EdgeType.DEPENDS_ON, src="a", dst="b"),
                EdgeSpec(type=EdgeType.DEPENDS_ON, src="b", dst="c"),
            ],
        )


def build_registry() -> WorkerRegistry:
    reg = WorkerRegistry()
    reg.register(EchoWorker())
    reg.register(ShellWorker())
    reg.register(CodexWorker(settings))
    reg.register_planner(ScriptedPlanner())
    return reg


# --- helpers --------------------------------------------------------------


async def drain(runner: Runner, max_rounds: int = 40) -> None:
    """Run ticks until the workgraph is stable (no running tasks, nothing runnable)."""
    for _ in range(max_rounds):
        await runner.tick()
        if runner._running:
            await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
        else:
            # one more tick to settle derived container states
            await runner.tick()
            if not runner._running:
                break


def find_node(nodes, objective_substr: str):
    for n in nodes:
        if objective_substr in n.objective:
            return n
    return None


# --- the slice ------------------------------------------------------------


async def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    store = Store(f"sqlite+aiosqlite:///{tmp}")
    await store.init()
    events = EventBus()
    captured: list[dict] = []

    async def collector():
        q = events.subscribe()
        while True:
            ev = await q.get()
            captured.append(ev)

    asyncio.create_task(collector())

    runner = Runner(store, registry=build_registry(), events=events, settings=settings)

    # 1) prompt -> root node -> initial planner
    root = await store.create_project("Ship a tiny landing page for turnloop.tech")
    assert root.executor == "planner"
    await drain(runner)

    nodes, edges, _ = await store.get_workgraph(root.id)
    by_obj = {n.objective: n for n in nodes}
    assert "Investigate the objective" in by_obj, "planner did not create children"
    assert len([n for n in nodes if n.parent_id == root.id]) == 3, "expected 3 children"

    a = by_obj["Investigate the objective"]
    b = by_obj["Confirm the key decision"]
    c = by_obj["Produce the deliverable"]

    # 2) execute ready leaves; b blocks, c waits on b
    assert a.status == NodeStatus.COMPLETE, a.status
    assert b.status == NodeStatus.BLOCKED, b.status
    assert c.status == NodeStatus.BLOCKED, c.status

    # 3) provide the missing input -> b becomes runnable and completes
    await runner.provide_input(b.id, "decision_x", "Go with a single hero section.")
    await drain(runner)
    b = await store.get_node(b.id)
    c = await store.get_node(c.id)
    assert b.status == NodeStatus.COMPLETE, b.status
    assert c.status == NodeStatus.COMPLETE, c.status

    # 4) root graph derived as complete
    root = await store.get_node(root.id)
    assert root.status == NodeStatus.COMPLETE, root.status

    # 5) edit the parent, then regenerate descendants
    await runner.edit_node(root.id, objective="Ship a tiny landing page (v2, with pricing)")
    await runner.regenerate_descendants(root.id)
    # old branch should be superseded
    old_a = await store.get_node(a.id)
    old_b = await store.get_node(b.id)
    assert old_a.status == NodeStatus.CANCELLED, old_a.status
    assert old_b.status == NodeStatus.CANCELLED, old_b.status

    # new branch was created and resumes execution
    nodes, _, _ = await store.get_workgraph(root.id)
    new_b = find_node([n for n in nodes if n.status != NodeStatus.CANCELLED], "Confirm the key decision")
    assert new_b is not None, "regenerated branch missing"
    await runner.provide_input(new_b.id, "decision_x", "Add a pricing table too.")
    await drain(runner)
    new_b = await store.get_node(new_b.id)
    new_c = find_node([n for n in nodes if n.status != NodeStatus.CANCELLED], "Produce the deliverable")
    new_c = await store.get_node(new_c.id)
    assert new_b.status == NodeStatus.COMPLETE, new_b.status
    assert new_c.status == NodeStatus.COMPLETE, new_c.status

    # 6) fork creates an independent alternative branch
    fork = await runner.fork(new_b.id)
    assert fork is not None and fork.forked_from == new_b.id
    await drain(runner)

    # events were emitted for the key transitions
    types = {e["type"] for e in captured}
    assert "node.created" in types
    assert "plan.applied" in types
    assert "graph.replaced" in types

    await store.dispose()
    print("VERTICAL SLICE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
