"""Seed a temporary server store with an arbitrary-node rejection state."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    HarnessKind,
    NodeSpec,
    NodeStatus,
    PlanResult,
    RunPolicy,
)


async def main() -> None:
    data_dir = Path(os.environ["TURN_DATA_DIR"])
    projects_dir = Path(os.environ["TURN_PROJECTS_DIR"])
    project_dir = projects_dir / "mock-rejection-demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "Live server step-by-step arbitrary rejection demonstration",
        repo_path=str(project_dir),
        agent=AgentConfig(
            harness=HarnessKind.MOCK,
            type_id=AgentType.PLANNER,
        ),
        run_policy=RunPolicy(auto_run=False),
    )
    foundation, polish, review = await store.apply_plan(
        root,
        PlanResult(nodes=[
            NodeSpec(
                key="foundation",
                objective="Start: build the foundation",
                executor="mock",
            ),
            NodeSpec(
                key="polish",
                objective="Middle: polish the integration",
                executor="mock",
                follows=["foundation"],
            ),
            NodeSpec(
                key="review",
                objective="Review: reject back to Start",
                executor="mock",
                agent_type=AgentType.VERIFIER,
                follows=["polish"],
            ),
        ]),
    )
    # Keep the project in manual Step mode. The root is already expanded, so
    # the server must not launch its planner on startup.
    root.auto_run = False
    root.run_policy = RunPolicy(auto_run=False)
    await store._save_node(root)
    await store.set_status(foundation.id, NodeStatus.RUNNABLE)
    review.generated_prompt = "MOCK_VERIFY_REJECT"
    await store._save_node(review)
    print(root.id)
    await store.dispose()


if __name__ == "__main__":
    asyncio.run(main())
