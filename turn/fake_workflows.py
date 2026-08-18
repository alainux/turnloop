"""Process-level fake-harness workflows used by test-mode E2E runs.

These projects are ordinary Turn graphs. Their plans are stored in each
project directory and their leaf prompts contain small fixture markers that
the repository-owned fake process understands. The graph, runner, terminal,
session, artifact, and rejection paths therefore run through the same
boundaries as a native harness.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    HarnessKind,
    NodeSpec,
    PlanResult,
    RunPolicy,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.workers.filesystem import init_project_directory


def fake_workflows_enabled() -> bool:
    return os.getenv("TURN_FAKE_WORKFLOWS", "").lower() in {"1", "true", "yes"}


def _leaf(
    key: str,
    objective: str,
    marker: str,
    *,
    follows: list[str] | None = None,
    agent_type: AgentType | None = None,
) -> NodeSpec:
    return NodeSpec(
        key=key,
        objective=objective,
        executor="fake",
        generated_prompt=marker,
        follows=follows or [],
        agent_type=agent_type,
    )


@dataclass(frozen=True)
class FakeWorkflowDefinition:
    key: str
    title: str
    prompt: str
    plan: PlanResult


def fake_workflow_definitions() -> tuple[FakeWorkflowDefinition, ...]:
    return (
        FakeWorkflowDefinition(
            key="reject-return",
            title="Fake · reject and return",
            prompt="Exercise a verifier rejection that returns work to an arbitrary target.",
            plan=PlanResult(
                nodes=[
                    _leaf("work", "Build the reviewable change", "FAKE_COMPLETE_REVIEWABLE"),
                    _leaf(
                        "review",
                        "Reject the change and return it to work",
                        "FAKE_VERIFY_REJECT",
                        follows=["work"],
                        agent_type=AgentType.VERIFIER,
                    ),
                    _leaf(
                        "release",
                        "Publish the accepted change",
                        "FAKE_COMPLETE_RELEASE",
                        follows=["review"],
                    ),
                ],
            ),
        ),
        FakeWorkflowDefinition(
            key="expand-graph",
            title="Fake · graph expansion",
            prompt="Exercise a process harness that expands itself into a dependent subgraph.",
            plan=PlanResult(
                nodes=[
                    _leaf("expand", "Expand this work into two ordered tasks", "FAKE_EXPAND"),
                ],
            ),
        ),
        FakeWorkflowDefinition(
            key="rerun-clean",
            title="Fake · rerun replaces outputs",
            prompt="Exercise Run again with a fresh graph and no accumulated artifacts.",
            plan=PlanResult(
                nodes=[
                    _leaf("reusable", "Produce one replaceable result", "FAKE_RERUN"),
                ],
            ),
        ),
        FakeWorkflowDefinition(
            key="failure-retry",
            title="Fake · failure and retry",
            prompt="Exercise a visible process failure followed by a successful retry.",
            plan=PlanResult(
                nodes=[
                    _leaf("retryable", "Run a task that fails once then recovers", "FAKE_FAIL_ONCE"),
                ],
            ),
        ),
        FakeWorkflowDefinition(
            key="block-input",
            title="Fake · block and provide input",
            prompt="Exercise a blocked process node that becomes runnable after input is supplied.",
            plan=PlanResult(
                nodes=[
                    _leaf("decision", "Wait for a user decision, then continue", "FAKE_BLOCK_ONCE"),
                ],
            ),
        ),
        FakeWorkflowDefinition(
            key="cancel-skip",
            title="Fake · stop and skip",
            prompt="Exercise stopping an active process and leaving its dependent work skipped.",
            plan=PlanResult(
                nodes=[
                    _leaf("long-task", "Run a cancellable long task", "FAKE_DELAYED"),
                    _leaf(
                        "skipped-dependent",
                        "Only run after the long task completes",
                        "FAKE_COMPLETE_DEPENDENT",
                        follows=["long-task"],
                    ),
                ],
            ),
        ),
    )


async def seed_fake_workflows(store: Store) -> list[str]:
    """Create the process-harness lab projects once."""
    existing = {project.project_name or project.objective for project in await store.list_projects()}
    created: list[str] = []
    for definition in fake_workflow_definitions():
        if definition.title in existing:
            continue
        root_id = uuid.uuid4()
        repo_path = init_project_directory(root_id, projects_dir=str(store.projects_dir))
        root = await store.create_project(
            definition.prompt,
            name=definition.title,
            repo_path=repo_path,
            id=root_id,
            agent=AgentConfig(
                harness=HarnessKind.FAKE,
                model="deterministic",
                type_id=AgentType.PLANNER,
            ),
            run_policy=RunPolicy(auto_run=False),
        )
        catalog = CapabilityCatalog(store.data_dir / "capabilities")
        for entry in catalog.list():
            catalog.load_into_project(entry.id, repo_path)
        plan_path = Path(repo_path) / ".turn" / "fake-plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(definition.plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
        root = await store.set_resource_refs(root.id, [str(plan_path.resolve())]) or root
        await store.apply_plan(root, definition.plan)
        created.append(str(root_id))
        existing.add(definition.title)
    return created
