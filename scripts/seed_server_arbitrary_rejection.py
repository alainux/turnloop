"""Seed a temporary server store with an arbitrary-node rejection state."""
from __future__ import annotations

import asyncio
import json
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
    VerificationDecision,
    VerificationResult,
)


async def main() -> None:
    data_dir = Path(os.environ["TURN_DATA_DIR"])
    projects_dir = Path(os.environ["TURN_PROJECTS_DIR"])
    project_dir = projects_dir / "echo-rejection-demo"
    project_dir.mkdir(parents=True, exist_ok=True)

    store = Store(data_dir, projects_dir=projects_dir)
    await store.init()
    root = await store.create_project(
        "Live server step-by-step arbitrary rejection demonstration",
        repo_path=str(project_dir),
        agent=AgentConfig(
            harness=HarnessKind.ECHO,
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
                executor="echo",
            ),
            NodeSpec(
                key="polish",
                objective="Middle: polish the integration",
                executor="echo",
                depends_on=["foundation"],
            ),
            NodeSpec(
                key="review",
                objective="Review: reject back to Start",
                executor="echo",
                agent_type=AgentType.EXECUTOR,
                depends_on=["polish"],
            ),
        ]),
    )
    # Keep the project in manual Step mode. The root is already expanded, so
    # the server must not launch its planner on startup.
    root.auto_run = False
    root.run_policy = RunPolicy(auto_run=False)
    await store._save_node(root)
    await store.set_status(foundation.id, NodeStatus.RUNNABLE)
    review.generated_prompt = json.dumps({
        "outcome": "COMPLETE",
        "summary": "Review rejected the integration",
        "verification": VerificationResult(
            decision=VerificationDecision.REJECT,
            summary="Repair the Start foundation before polishing again",
            findings=["The integration relies on an invalid foundation"],
            required_changes=["Rebuild the Start foundation"],
            target_node_id=foundation.id,
        ).model_dump(mode="json"),
    })
    await store._save_node(review)
    print(root.id)
    await store.dispose()


if __name__ == "__main__":
    asyncio.run(main())
