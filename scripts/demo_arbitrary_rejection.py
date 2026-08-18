"""Run a local Echo project through an arbitrary-node rejection loop.

This is intentionally deterministic: it uses the real Store, Runner,
GraphWalker, and EchoWorker, while replacing only the terminal transport with
an in-memory sink so no harness or Herdr service is required.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from turn.config import Settings
from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    HarnessKind,
    NodeSpec,
    NodeStatus,
    PlanResult,
    VerificationDecision,
)
from turn.graph.logic import GraphWalker, derive_flow_edges
from turn.runner.events import EventBus
from turn.runner.runner import Runner
from turn.workers.echo_worker import EchoWorker
from turn.workers.base import NodeExecutionContext
from turn.workers.registry import WorkerRegistry


class DemoTerminal:
    """Small terminal port that records injected rejection feedback."""

    supports_inject = False

    def __init__(self) -> None:
        self.output: dict[object, str] = {}

    def snapshot(self, node_id):
        return {"active": True, "output": self.output.get(node_id, "")}

    async def write(self, node_id, data):
        value = data.decode() if isinstance(data, bytes) else data
        self.output[node_id] = self.output.get(node_id, "") + value
        return True


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="turn-echo-rejection-") as temporary:
        root_path = Path(temporary)
        store = Store(root_path / "state")
        await store.init()
        terminal = DemoTerminal()
        registry = WorkerRegistry()
        echo = EchoWorker()
        registry.register(echo)
        runner = Runner(
            store,
            registry=registry,
            events=EventBus(),
            settings=Settings(),
            terminal_transport=terminal,
        )
        root = await store.create_project(
            "Echo arbitrary rejection demonstration",
            repo_path=str(root_path / "project"),
            agent=AgentConfig(
                harness=HarnessKind.ECHO,
                type_id=AgentType.PLANNER,
            ),
        )
        foundation, polish, review = await store.apply_plan(
            root,
            PlanResult(nodes=[
                NodeSpec(
                    key="foundation",
                    objective="Build the foundation",
                    executor="echo",
                ),
                NodeSpec(
                    key="polish",
                    objective="Polish the integration",
                    executor="echo",
                    follows=["foundation"],
                ),
                NodeSpec(
                    key="review",
                    objective="Review the integration",
                    executor="echo",
                    agent_type=AgentType.EXECUTOR,
                    follows=["polish"],
                ),
            ]),
        )

        async def complete(node) -> None:
            await store.set_status(node.id, NodeStatus.RUNNING)
            run = await store.create_run(node, "echo")
            result = await echo.execute(
                NodeExecutionContext(node=node, repo_path=str(root_path / "project"))
            )
            await runner._handle_outcome(node, run, root.id, result)

        await complete(foundation)
        await complete(polish)

        review.generated_prompt = json.dumps({
            "outcome": "COMPLETE",
            "summary": "The integration review found an earlier defect",
            "verification": {
                "decision": VerificationDecision.REJECT.value,
                "summary": "Repair the foundation before polishing again",
                "findings": ["The integration relies on an invalid foundation"],
                "required_changes": ["Rebuild the foundation"],
                "target_node_id": str(foundation.id),
            },
        })
        await store._save_node(review)
        await store.set_status(review.id, NodeStatus.RUNNING)
        review_run = await store.create_run(review, "echo")
        review_result = await echo.execute(
            NodeExecutionContext(node=review, repo_path=str(root_path / "project"))
        )
        await runner._handle_outcome(review, review_run, root.id, review_result)

        nodes, edges, _ = await store.get_workgraph(root.id)
        walker = GraphWalker(nodes, edges)
        flow = derive_flow_edges(nodes, edges, walker.evaluate().status)
        print(f"project: {root.id}")
        print(f"reviewer: {review.id} ({review.agent.type_id.value})")
        print(f"arbitrary target: {foundation.id}")
        print(f"return arrow: {flow[0].src} -> {flow[0].dst} [{flow[0].type.value}]")
        print(f"target status after rejection: {(await store.get_node(foundation.id)).status.value}")
        print(f"persistent DAG return edges: {sum(edge.type.value == 'RETURN' for edge in edges)}")

        await store.set_status(foundation.id, NodeStatus.COMPLETE)
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
