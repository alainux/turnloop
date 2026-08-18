"""Vertical slice test.

Proves the smallest useful path works across an unrelated objective:

  prompt -> root node -> initial planner -> visible graph -> execute ready
  leaves -> block -> provide input -> complete -> edit parent -> regenerate
  descendants -> execution resumes.

Runs entirely offline against a temporary local-file store with deterministic workers.
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
from turn.tests.fakes import FakeHerdrAdapter


# --- deterministic planner ------------------------------------------------


class ScriptedPlanner(Planner):
    name = "scripted"

    async def plan(self, ctx: NodeExecutionContext) -> PlanResult:
        # a: runnable, completes
        # b: follows a, blocks requesting a decision
        # c: follows b, completes (sequence join)
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
                    follows=["a"],
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
                    follows=["b"],
                    generated_prompt='{"outcome":"COMPLETE","summary":"produced"}',
                ),
            ],
            edges=[
                EdgeSpec(type=EdgeType.FOLLOWS, src="a", dst="b"),
                EdgeSpec(type=EdgeType.FOLLOWS, src="b", dst="c"),
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
    tmp = tempfile.mkdtemp()
    store = Store(tmp)
    await store.init()
    events = EventBus()
    captured: list[dict] = []

    async def collector():
        q = events.subscribe()
        while True:
            ev = await q.get()
            captured.append(ev)

    asyncio.create_task(collector())

    runner = Runner(
        store,
        registry=build_registry(),
        events=events,
        settings=settings,
        herdr_adapter=FakeHerdrAdapter(),
    )

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
    # regeneration replaces the old branch instead of retaining revisions
    assert await store.get_node(a.id) is None
    assert await store.get_node(b.id) is None

    # new branch was created and resumes execution
    nodes, _, _ = await store.get_workgraph(root.id)
    new_b = find_node(nodes, "Confirm the key decision")
    assert new_b is not None, "regenerated branch missing"
    await runner.provide_input(new_b.id, "decision_x", "Add a pricing table too.")
    await drain(runner)
    new_b = await store.get_node(new_b.id)
    new_c = find_node(nodes, "Produce the deliverable")
    new_c = await store.get_node(new_c.id)
    assert new_b.status == NodeStatus.COMPLETE, new_b.status
    assert new_c.status == NodeStatus.COMPLETE, new_c.status

    # events were emitted for the key transitions
    types = {e["type"] for e in captured}
    assert "node.created" in types
    assert "plan.applied" in types
    assert "graph.replaced" in types

    await store.dispose()
    print("VERTICAL SLICE TEST PASSED")


async def test_manual_mode() -> None:
    """Manual stepping: the runner plans but never auto-executes; each step
    runs exactly one runnable node, and a plain tick does nothing."""
    tmp = tempfile.mkdtemp()
    store = Store(tmp)
    await store.init()
    events = EventBus()
    runner = Runner(
        store,
        registry=build_registry(),
        events=events,
        settings=settings,
        herdr_adapter=FakeHerdrAdapter(),
    )

    root = await store.create_project("Ship a tiny landing page for turnloop.tech (manual)")
    await store.set_auto_run(root.id, False)

    # In manual mode a tick must NOT auto-run anything.
    await runner.tick()
    assert runner._running == {}, "manual mode must not auto-run on tick"
    nodes, _, _ = await store.get_workgraph(root.id)
    assert len(nodes) == 1, "planner should not have run yet"

    # Step the planner -> it builds the graph.
    stepped = await runner.step(root.id)
    assert stepped == [root.id], "first step should run the planner root"
    if runner._running:
        await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    assert runner._running == {}, "step task should have finished"
    nodes, _, _ = await store.get_workgraph(root.id)
    children = [n for n in nodes if n.parent_id == root.id]
    assert len(children) == 3, "planner should have created 3 children"

    # Still manual: a tick runs nothing; leaves are merely RUNNABLE.
    await runner.tick()
    assert runner._running == {}
    a = find_node(children, "Investigate the objective")
    a = await store.get_node(a.id)
    assert a.status == NodeStatus.RUNNABLE

    # Step one leaf at a time; shallowest (a) runs first.
    stepped = await runner.step(root.id)
    assert stepped == [a.id]
    if runner._running:
        await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    a = await store.get_node(a.id)
    assert a.status == NodeStatus.COMPLETE

    # b is BLOCKED until its input is supplied (re-evaluate after a completes).
    await runner.tick()
    b = await store.get_node(find_node(children, "Confirm the key decision").id)
    assert b.status == NodeStatus.BLOCKED
    await runner.provide_input(b.id, "decision_x", "single hero section")
    stepped = await runner.step(root.id)
    assert stepped == [b.id]
    if runner._running:
        await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    b = await store.get_node(b.id)
    assert b.status == NodeStatus.COMPLETE

    # c joins after b; step it to finish.
    c = find_node(children, "Produce the deliverable")
    stepped = await runner.step(root.id)
    assert stepped == [c.id]
    if runner._running:
        await asyncio.gather(*list(runner._running.values()), return_exceptions=True)
    c = await store.get_node(c.id)
    assert c.status == NodeStatus.COMPLETE

    # a final tick derives container completion for the root
    await runner.tick()
    root = await store.get_node(root.id)
    assert root.status == NodeStatus.COMPLETE
    await store.dispose()
    print("MANUAL MODE TEST PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(test_manual_mode())
