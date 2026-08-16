from __future__ import annotations

from pathlib import Path
import json

import pytest

from turn.domain.schemas import AgentConfig, AgentType, NodeSpec, PlanResult
from turn.skills.library import materialize, validate_skill_reference
from turn.workers.planner import AgentPlanner


class FakeSkillFetcher:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.urls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload


class FakeBundleFetcher(FakeSkillFetcher):
    def __init__(self, files: dict[str, bytes]):
        super().__init__(files[next(path for path in files if Path(path).name == "SKILL.md")])
        self.files = files

    def fetch_files(self, url: str) -> dict[str, bytes]:
        self.urls.append(url)
        return self.files


def test_find_skills_is_a_planner_skill():
    planner = AgentConfig(type_id=AgentType.PLANNER)
    assert planner.skill_ids == ["turn-planning", "imagegen", "find-skills"]
    assert any(path.endswith("planner/find-skills.md") for path in planner.skills)
    assert any(path.endswith("planner/turn-planning.md") for path in planner.skills)
    assert any(path.endswith("planner/imagegen.md") for path in planner.skills)
    assert not any(path.endswith("turn-architecture-research.md") for path in planner.skills)


def test_worker_receives_role_base_skills_and_planner_selected_additions():
    worker = AgentConfig(
        type_id=AgentType.EXECUTOR,
        skill_ids=["turn-architecture-research"],
    )
    assert worker.skill_ids == [
        "turn-executing",
        "turn-architecture-research",
    ]


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
    with pytest.raises(ValueError, match=r"local id, project:<slug>, or an http\(s\) URL"):
        validate_skill_reference("not-installed")


def test_project_authored_skill_is_resolved_without_copying_or_network(tmp_path: Path):
    path = tmp_path / ".turn" / "skills" / "game-design" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: game-design\ndescription: Design playable game loops.\n---\n\n"
        "Keep the interaction loop concrete.\n"
    )

    plan = PlanResult(nodes=[NodeSpec(
        key="game",
        objective="Design the game",
        skills=["project:game-design"],
    )])
    assert plan.nodes[0].skills == ["project:game-design"]
    installed = materialize(["project:game-design"], tmp_path)
    assert installed["project:game-design"] == path


def test_project_authored_skill_requires_frontmatter(tmp_path: Path):
    path = tmp_path / ".turn" / "skills" / "bad" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Missing metadata\n")

    with pytest.raises(ValueError, match="YAML frontmatter"):
        materialize(["project:bad"], tmp_path)


def test_external_skill_is_fetched_once_into_hidden_project_turn_directory(tmp_path: Path):
    url = "https://example.test/skills/visual-qa/SKILL.md"
    payload = b"---\nname: visual-qa\ndescription: Inspect rendered output.\n---\n\n# Visual QA\nInspect the real rendered screen.\n"
    fetcher = FakeSkillFetcher(payload)

    installed = materialize(["turn-executing", url], tmp_path, fetcher=fetcher)
    assert installed["turn-executing"].is_file()
    external_path = installed[url]
    assert external_path == tmp_path / ".turn" / "skills" / external_path.parent.name / "SKILL.md"
    assert not (tmp_path / "turn" / "skills").exists()
    assert external_path.read_bytes() == payload
    assert fetcher.urls == [url]

    materialize([url], tmp_path, fetcher=fetcher)
    assert fetcher.urls == [url]


def test_external_skill_installs_a_standard_multifile_tree(tmp_path: Path):
    url = "https://github.com/example/skills/tree/main/visual-qa"
    skill = b"---\nname: visual-qa\ndescription: Inspect rendered output.\n---\n\nUse browser evidence.\n"
    fetcher = FakeBundleFetcher({
        "visual-qa/SKILL.md": skill,
        "visual-qa/references/checklist.md": b"# Checklist\n",
        "visual-qa/scripts/check.py": b"print('ok')\n",
    })

    installed = materialize([url], tmp_path, fetcher=fetcher)
    root = installed[url].parent
    assert (root / "SKILL.md").read_bytes() == skill
    assert (root / "references" / "checklist.md").read_text() == "# Checklist\n"
    assert (root / "scripts" / "check.py").read_text() == "print('ok')\n"
    assert not (tmp_path / "turn" / "skills").exists()


def test_external_html_is_rejected_and_invalid_existing_install_is_replaced(tmp_path: Path):
    url = "https://example.test/catalog/visual-qa"
    target = tmp_path / ".turn" / "skills" / "external-visual-qa-placeholder" / "SKILL.md"
    # The actual key is URL-derived; create the stale file after resolving it.
    from turn.skills.library import _external_install_key
    target = tmp_path / ".turn" / "skills" / _external_install_key(url) / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("<!doctype html><title>catalog</title>")
    valid = b"---\nname: visual-qa\ndescription: Inspect rendered output.\n---\n\nUse browser evidence.\n"
    installed = materialize([url], tmp_path, fetcher=FakeSkillFetcher(valid))
    assert installed[url].read_bytes() == valid

    bad_url = "https://example.test/catalog/bad"
    with pytest.raises(ValueError, match="HTML"):
        materialize([bad_url], tmp_path, fetcher=FakeSkillFetcher(b"<!doctype html><body>catalog</body>"))
