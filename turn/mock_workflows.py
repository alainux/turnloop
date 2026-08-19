"""Process-level mock-harness workflows used by test-mode E2E runs.

These projects are ordinary Turn graphs. Their plans are stored in each
project directory and their leaf prompts contain small fixture markers that
the repository-owned mock process understands. The graph, runner, terminal,
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
    TriggerKind,
    TriggerSpec,
)
from turn.capabilities.catalog import CapabilityCatalog
from turn.workers.filesystem import init_project_directory


def mock_workflows_enabled() -> bool:
    return os.getenv("TURN_MOCK_WORKFLOWS", "").lower() in {"1", "true", "yes"}


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
        executor="mock",
        generated_prompt=marker,
        follows=follows or [],
        agent_type=agent_type,
    )


@dataclass(frozen=True)
class MockWorkflowDefinition:
    key: str
    title: str
    prompt: str
    plan: PlanResult
    auto_run: bool = False


def _mock_text(value: str) -> str:
    """Normalize persisted fixture text after the legacy fixture rename."""
    return (
        value.replace("FAKE_", "MOCK_")
        .replace("fake-plan", "mock-plan")
        .replace("scheduled echo", "scheduled mock")
    )


async def _migrate_persisted_mock_project(store: Store, project) -> None:
    """Repair older owned lab state without changing its event history."""
    nodes, _, _ = await store.get_workgraph(project.id)
    for node in nodes:
        prompt = node.generated_prompt
        objective = node.objective
        normalized_prompt = _mock_text(prompt) if prompt else prompt
        normalized_objective = _mock_text(objective) if objective else objective
        if normalized_prompt != prompt or normalized_objective != objective:
            await store.edit_node(
                node.id,
                objective=normalized_objective,
                generated_prompt=normalized_prompt,
            )

    root = await store.get_node(project.id)
    if root is not None:
        refs = [_mock_text(ref) for ref in root.resource_refs]
        if refs != root.resource_refs:
            await store.set_resource_refs(root.id, refs)

    if not project.repo_path:
        return
    repo = Path(project.repo_path)
    for plan_path in (repo / ".turn" / "fake-plan.json", repo / ".turn" / "mock-plan.json"):
        if not plan_path.exists():
            continue
        original = plan_path.read_text(encoding="utf-8")
        normalized = _mock_text(original)
        if normalized != original:
            plan_path.write_text(normalized, encoding="utf-8")


def mock_workflow_definitions() -> tuple[MockWorkflowDefinition, ...]:
    return (
        MockWorkflowDefinition(
            key="reject-return",
            title="Mock · reject and return",
            prompt="Exercise a verifier rejection that returns work to an arbitrary target.",
            plan=PlanResult(
                nodes=[
                    _leaf("work", "Build the reviewable change", "MOCK_COMPLETE_REVIEWABLE"),
                    _leaf(
                        "review",
                        "Reject the change and return it to work",
                        "MOCK_VERIFY_REJECT",
                        follows=["work"],
                        agent_type=AgentType.VERIFIER,
                    ),
                    _leaf(
                        "release",
                        "Publish the accepted change",
                        "MOCK_COMPLETE_RELEASE",
                        follows=["review"],
                    ),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="expand-graph",
            title="Mock · graph expansion",
            prompt="Exercise a process harness that expands itself into a dependent subgraph.",
            plan=PlanResult(
                nodes=[
                    _leaf("expand", "Expand this work into two ordered tasks", "MOCK_EXPAND"),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="rerun-clean",
            title="Mock · rerun replaces outputs",
            prompt="Exercise Run again with a fresh graph and no accumulated artifacts.",
            plan=PlanResult(
                nodes=[
                    _leaf("reusable", "Produce one replaceable result", "MOCK_RERUN"),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="failure-retry",
            title="Mock · failure and retry",
            prompt="Exercise a visible process failure followed by a successful retry.",
            plan=PlanResult(
                nodes=[
                    _leaf("retryable", "Run a task that fails once then recovers", "MOCK_FAIL_ONCE"),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="block-input",
            title="Mock · block and provide input",
            prompt="Exercise a blocked process node that becomes runnable after input is supplied.",
            plan=PlanResult(
                nodes=[
                    _leaf("decision", "Wait for a user decision, then continue", "MOCK_BLOCK_ONCE"),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="cancel-skip",
            title="Mock · stop and skip",
            prompt="Exercise stopping an active process and leaving its dependent work skipped.",
            plan=PlanResult(
                nodes=[
                    _leaf("long-task", "Run a cancellable long task", "MOCK_DELAYED"),
                    _leaf(
                        "skipped-dependent",
                        "Only run after the long task completes",
                        "MOCK_COMPLETE_DEPENDENT",
                        follows=["long-task"],
                    ),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="schedule-demo",
            title="Mock · schedule demo",
            prompt="Exercise a classic cron trigger with configured event data through the process harness.",
            auto_run=True,
            plan=PlanResult(
                nodes=[
                    _leaf(
                        "scheduled",
                        "Run the scheduled mock job",
                        "MOCK_COMPLETE_SCHEDULED",
                    ),
                ],
                triggers=[
                    TriggerSpec(
                        target_key="scheduled",
                        kind=TriggerKind.SCHEDULE,
                        schedule="* * * * *",
                        data={"demo": "schedule", "channel": "cron"},
                    ),
                ],
            ),
        ),
        MockWorkflowDefinition(
            key="verifier-acceptance-loop",
            title="Mock · verifier acceptance loop",
            prompt="Exercise a process-harness loop that only restarts after verifier acceptance.",
            plan=PlanResult(
                nodes=[
                    _leaf(
                        "work",
                        "Produce the reviewable work",
                        "MOCK_COMPLETE_REVIEWABLE",
                    ),
                    _leaf(
                        "review",
                        "Reject once, then accept the corrected work",
                        "MOCK_VERIFY_REJECT_THEN_APPROVE",
                        follows=["work"],
                        agent_type=AgentType.VERIFIER,
                    ),
                ],
                triggers=[
                    TriggerSpec(
                        target_key="work",
                        event_name="loop.begin",
                        data={"demo": "verifier-loop", "source": "manual"},
                    ),
                    TriggerSpec(
                        target_key="work",
                        event_name="verification.accepted",
                        data={"demo": "verifier-loop", "source": "acceptance"},
                    ),
                ],
            ),
        ),
    )


async def seed_mock_workflows(store: Store) -> list[str]:
    """Create the process-harness lab projects once."""
    projects = await store.list_projects()
    existing = {project.project_name or project.objective for project in projects}
    existing_by_title = {
        project.project_name or project.objective: project for project in projects
    }
    # Remove only the two obsolete legacy fixtures from the owned mock lab.
    # They were replaced by the process-backed schedule and loop scenarios.
    obsolete_titles = {"Mock · schedule echo", "Mock · loop echo"}
    for project in projects:
        title = project.project_name or project.objective
        if title in obsolete_titles:
            await store.delete_project(project.id)
            existing.discard(title)
            existing_by_title.pop(title, None)
        elif project.agent is not None and project.agent.harness is HarnessKind.MOCK:
            await _migrate_persisted_mock_project(store, project)

    # Keep already-seeded test-lab projects aligned with the professional
    # Mock naming without creating duplicate projects on a server restart.
    legacy_titles = {
        title: title.replace("Fake ·", "Mock ·")
        for title in existing
        if title.startswith("Fake ·")
    }
    for legacy, current_title in legacy_titles.items():
        project = existing_by_title[legacy]
        await store.rename_project(project.id, current_title)
        if project.objective.startswith("Fake ·"):
            await store.edit_node(project.id, objective=current_title)
            project.objective = current_title
        existing.discard(legacy)
        existing.add(current_title)
        existing_by_title[current_title] = project
        existing_by_title.pop(legacy, None)
    created: list[str] = []
    for definition in mock_workflow_definitions():
        if definition.title in existing:
            current = existing_by_title[definition.title]
            if current.objective.startswith("Fake ·"):
                await store.edit_node(current.id, objective=definition.title)
            if current.auto_run != definition.auto_run:
                # The lab definitions are authoritative. Older seeded copies
                # could retain a user-selected auto-run flag and then replay a
                # rejection fixture indefinitely instead of waiting for the
                # next explicit UI action.
                await store.set_project_mode(current.id, definition.auto_run)
            continue
        root_id = uuid.uuid4()
        repo_path = init_project_directory(root_id, projects_dir=str(store.projects_dir))
        root = await store.create_project(
            definition.prompt,
            name=definition.title,
            repo_path=repo_path,
            id=root_id,
            agent=AgentConfig(
                harness=HarnessKind.MOCK,
                model="deterministic",
                type_id=AgentType.PLANNER,
            ),
            run_policy=RunPolicy(auto_run=definition.auto_run),
        )
        catalog = CapabilityCatalog(store.data_dir / "capabilities")
        for entry in catalog.list():
            catalog.load_into_project(entry.id, repo_path)
        plan_path = Path(repo_path) / ".turn" / "mock-plan.json"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(definition.plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
        root = await store.set_resource_refs(root.id, [str(plan_path.resolve())]) or root
        await store.apply_plan(root, definition.plan)
        created.append(str(root_id))
        existing.add(definition.title)
    return created
