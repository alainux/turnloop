"""Run a local Mock project through a process-backed rejection loop.

This uses the real Store, Runner, GraphWalker, and process-backed Mock
harness. It is the same process/CLI boundary exercised by the Mock E2E suite.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    HarnessKind,
    NodeStatus,
    NodeSpec,
    PlanResult,
)
from turn.graph.logic import GraphWalker, derive_flow_edges
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.mock_harness import MockHarnessWorker
from turn.workers.registry import WorkerRegistry
from turn.workers.terminal import LocalPtyTransport


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="turn-mock-rejection-") as temporary:
        root_path = Path(temporary)
        store = Store(root_path / "state")
        await store.init()
        settings = Settings(
            data_dir=str(root_path / "state"),
            projects_dir=str(root_path / "projects"),
            default_executor="mock",
            planner="mock",
            runner_tick_seconds=0.01,
        )
        registry = WorkerRegistry()
        registry.register(MockHarnessWorker(settings))
        runner = Runner(
            store,
            registry=registry,
            events=EventBus(),
            settings=settings,
            terminal_transport=LocalPtyTransport(),
        )
        root = await store.create_project(
            "Mock arbitrary rejection demonstration",
            repo_path=str(root_path / "project"),
            agent=AgentConfig(
                harness=HarnessKind.MOCK,
                type_id=AgentType.PLANNER,
            ),
        )
        foundation, polish, review = await store.apply_plan(
            root,
            PlanResult(nodes=[
                NodeSpec(
                    key="foundation",
                    objective="Build the foundation",
                    executor="mock",
                ),
                NodeSpec(
                    key="polish",
                    objective="Polish the integration",
                    executor="mock",
                    follows=["foundation"],
                ),
                NodeSpec(
                    key="review",
                    objective="Review the integration",
                    executor="mock",
                    agent_type=AgentType.VERIFIER,
                    follows=["polish"],
                ),
            ]),
        )

        async def complete(node) -> None:
            await runner.run_node(node.id)
            task = runner._running.get(node.id)
            assert task is not None
            await task

        await complete(foundation)
        await complete(polish)

        review.generated_prompt = "MOCK_VERIFY_REJECT"
        await store._save_node(review)
        await store.set_status(review.id, NodeStatus.RUNNABLE)
        await runner.run_node(review.id)
        review_task = runner._running.get(review.id)
        assert review_task is not None
        await review_task

        nodes, edges, _ = await store.get_workgraph(root.id)
        walker = GraphWalker(nodes, edges)
        flow = derive_flow_edges(nodes, edges, walker.evaluate().status)
        print(f"project: {root.id}")
        print(f"reviewer: {review.id} ({review.agent.type_id.value})")
        print(f"rejection target: {polish.id}")
        print(f"return arrow: {flow[0].src} -> {flow[0].dst} [{flow[0].type.value}]")
        print(f"target status after rejection: {(await store.get_node(polish.id)).status.value}")
        print(f"persistent DAG return edges: {sum(edge.type.value == 'RETURN' for edge in edges)}")

        await store.set_status(polish.id, NodeStatus.COMPLETE)
        nodes, edges, _ = await store.get_workgraph(root.id)
        restored = derive_flow_edges(
            nodes,
            edges,
            GraphWalker(nodes, edges).evaluate().status,
        )
        print(f"return arrows after repair: {len(restored)}")
        await store.dispose()


if __name__ == "__main__":
    asyncio.run(main())
