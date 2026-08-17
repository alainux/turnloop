from __future__ import annotations

from pathlib import Path
import json

import pytest

from turn.db.store import Store
from turn.domain.schemas import (
    AgentConfig,
    AgentType,
    NodeSpec,
    SETUP_SKILL_ID,
    PlanResult,
)
from turn.skills.library import (
    SKILLS,
    install_builtin_skill,
    resolve_skill_paths,
    validate_plan_skill_files,
    validate_skill_reference,
)
from turn.workers.planner import AgentPlanner


def test_find_skills_is_a_planner_skill():
    planner = AgentConfig(type_id=AgentType.PLANNER)
    assert planner.skill_ids == [
        "turn-planning", "imagegen", "find-skills", "find-mcps",
    ]
    assert any(path.endswith("planner/find-skills.md") for path in planner.skills)
    assert any(path.endswith("planner/find-mcps.md") for path in planner.skills)
    assert any(path.endswith("planner/turn-planning.md") for path in planner.skills)
    assert SETUP_SKILL_ID not in planner.skill_ids
    assert not any(path.endswith("planner/turn-setup.md") for path in planner.skills)
    assert any(path.endswith("planner/imagegen.md") for path in planner.skills)
    assert not any(path.endswith("turn-architecture-research.md") for path in planner.skills)


@pytest.mark.asyncio
async def test_only_the_project_root_gets_the_setup_skill(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()
    root = await store.create_project("Set up a small workflow")

    assert SETUP_SKILL_ID in root.agent.skill_ids
    assert "imagegen" not in root.agent.skill_ids
    assert not any(path.endswith("planner/imagegen.md") for path in root.agent.skills)

    created = await store.apply_plan(
        root,
        PlanResult(nodes=[NodeSpec(
            key="architect",
            objective="Plan application architecture",
            executor="planner",
            plan=True,
            skills=["turn-architecture-research"],
        )]),
    )
    architect = created[0]
    assert architect.agent is not None
    assert architect.agent.type_id is AgentType.PLANNER
    assert SETUP_SKILL_ID not in architect.agent.skill_ids
    assert "imagegen" in architect.agent.skill_ids
    assert "turn-architecture-research" in architect.agent.skill_ids


@pytest.mark.asyncio
async def test_root_setup_name_is_ingested_without_overriding_user_name(tmp_path):
    store = Store(tmp_path / "state")
    await store.init()

    unnamed = await store.create_project("Build a tiny checklist")
    await store.apply_plan(
        unnamed,
        PlanResult(nodes=[], project_name="Daily Checklist"),
    )
    stored_unnamed = await store.get_node(unnamed.id)
    assert stored_unnamed is not None
    assert stored_unnamed.project_name == "Daily Checklist"

    named = await store.create_project("Build another checklist", name="My Checklist")
    await store.apply_plan(
        named,
        PlanResult(nodes=[], project_name="Planner Replacement"),
    )
    stored_named = await store.get_node(named.id)
    assert stored_named is not None
    assert stored_named.project_name == "My Checklist"

    renamed = await store.create_project("Build a renamed checklist")
    renamed.project_name = "User Renamed Checklist"
    await store._save_node(renamed)
    await store.apply_plan(
        renamed,
        PlanResult(nodes=[], project_name="Planner Replacement"),
    )
    stored_renamed = await store.get_node(renamed.id)
    assert stored_renamed is not None
    assert stored_renamed.project_name == "User Renamed Checklist"


def test_worker_receives_role_base_skills_and_planner_selected_additions():
    worker = AgentConfig(
        type_id=AgentType.EXECUTOR,
        skill_ids=["turn-architecture-research"],
    )
    assert worker.skill_ids == [
        "turn-executing",
        "turn-architecture-research",
    ]


def test_research_and_distribution_skills_are_available_to_assignments():
    for skill_id in (
        "turn-research",
        "turn-product-design",
        "turn-plan-distribution",
    ):
        assert skill_id in SKILLS
        assert SKILLS[skill_id].source_path.is_file()
    assert "turn-organization-validation" not in SKILLS


@pytest.mark.parametrize(
    ("agent_type", "base_skill"),
    [
        (AgentType.EXECUTOR, "turn-executing"),
        (AgentType.INTEGRATOR, "turn-integrating"),
        (AgentType.VERIFIER, "turn-verifying"),
    ],
)
def test_workers_receive_only_their_role_base_skill(agent_type: AgentType, base_skill: str):
    agent = AgentConfig(type_id=agent_type)
    assert agent.skill_ids == [base_skill]
    assert "turn-product-coherence" not in agent.skill_ids


def test_broad_plan_allows_role_base_skills_when_research_finds_no_addition():
    base = {
        "nodes": [
            {"key": "build", "objective": "Build the product", "agent_type": "executor"},
            {
                "key": "integrate",
                "objective": "Integrate the product",
                "agent_type": "integrator",
                "depends_on": ["build"],
            },
        ],
        "document_refs": ["ARCHITECTURE.md"],
    }
    plan = AgentPlanner._parse_plan(json.dumps(base))
    assert plan.nodes[0].skills == []
    assert plan.nodes[1].skills == []

    base["nodes"][0]["skills"] = ["project:product-design"]
    base["nodes"][1]["skills"] = ["turn-integrating"]
    plan = AgentPlanner._parse_plan(json.dumps(base))
    assert plan is not None
    assert plan.nodes[0].skills == ["project:product-design"]


def test_broad_plan_allows_sparse_research_metadata():
    payload = {
        "nodes": [
            {
                "key": "build",
                "objective": "Build the product",
                "agent_type": "executor",
                "skills": ["turn-executing"],
            },
            {
                "key": "verify",
                "objective": "Verify the product",
                "agent_type": "verifier",
                "depends_on": ["build"],
                "skills": ["turn-verifying"],
            },
        ],
        "document_refs": ["ARCHITECTURE.md"],
    }
    plan = AgentPlanner._parse_plan(json.dumps(payload))
    assert plan.nodes[0].skills == ["turn-executing"]


def test_plan_accepts_local_ids_and_external_skill_urls():
    external = "https://example.test/skills/visual-qa/SKILL.md"
    plan = PlanResult(nodes=[
        NodeSpec(
            key="verify",
            objective="Inspect the rendered result",
            agent_type=AgentType.VERIFIER,
            depends_on=["work"],
            skills=[external],
        ),
        NodeSpec(key="work", objective="Build the result", skills=["turn-executing"]),
    ])
    assert plan.nodes[0].skills == [external]
    assert plan.nodes[0].depends_on == ["work"]


def test_unknown_skill_reference_is_rejected():
    with pytest.raises(ValueError, match=r"built-in id, project:<slug>, or an http\(s\) URL"):
        validate_skill_reference("not-installed")


def test_project_authored_skill_is_resolved_without_content_validation(tmp_path: Path):
    path = tmp_path / ".turn" / "skills" / "game-design" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("planner-authored guidance without required frontmatter\n")

    plan = PlanResult(nodes=[NodeSpec(
        key="game",
        objective="Design the game",
        skills=["project:game-design"],
    )])
    assert plan.nodes[0].skills == ["project:game-design"]
    assert resolve_skill_paths(["project:game-design"], tmp_path)["project:game-design"] == path


def test_planner_can_install_a_builtin_skill_from_the_turn_library(tmp_path: Path):
    path = install_builtin_skill("imagegen", tmp_path)

    assert path == tmp_path / ".turn" / "skills" / "imagegen" / "SKILL.md"
    assert path.read_bytes() == SKILLS["imagegen"].source_path.read_bytes()
    assert resolve_skill_paths(["imagegen"], tmp_path)["imagegen"] == path


def test_plan_skill_files_require_current_project_installation(tmp_path: Path):
    payload = {"nodes": [{"key": "work", "skills": ["imagegen", "project:visual-qa"]}]}

    with pytest.raises(ValueError, match="imagegen.*not installed"):
        validate_plan_skill_files(payload, tmp_path)

    install_builtin_skill("imagegen", tmp_path)
    with pytest.raises(ValueError, match="project:visual-qa.*not installed"):
        validate_plan_skill_files(payload, tmp_path)

    skill_path = tmp_path / ".turn" / "skills" / "visual-qa" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("anything the planner authored")
    install_builtin_skill("turn-executing", tmp_path)
    validate_plan_skill_files(payload, tmp_path)


def test_plan_skill_files_reject_external_urls(tmp_path: Path):
    payload = {
        "nodes": [{"key": "work", "skills": ["https://example.test/SKILL.md"]}]
    }

    with pytest.raises(ValueError, match="must install.*project:<slug>"):
        validate_plan_skill_files(payload, tmp_path)
