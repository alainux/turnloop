"""Built-in skills and planner-authored project skill references.

Turn owns the built-in skill catalog. Planners use their available tools to
copy selected library skills or author external skills directly in
``.turn/skills`` and submit them as local ``project:<slug>`` references. Turn
never downloads or inspects external skill content.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from turn.domain.skill_contracts import (
    is_project_skill_reference as _is_project_skill_reference,
    is_skill_url as _is_skill_url,
    project_skill_slug,
    skill_ids_for_agent_type,
    validate_skill_reference as _validate_skill_reference,
)


@dataclass(frozen=True)
class SkillDefinition:
    id: str
    title: str
    source_path: Path
    description: str
    source_url: str | None = None


_ROOT = Path(__file__).resolve().parent.parent / "agents" / "skills"
_AGENCY_AGENTS = "https://github.com/msitarzewski/agency-agents"
_SKILLS_SH = "https://skills.sh/"
SKILLS: dict[str, SkillDefinition] = {
    "turn-planning": SkillDefinition(
        "turn-planning", "Turn planning", _ROOT / "planner" / "turn-planning.md",
        "Graph decomposition, project documents, contracts, and orchestration.",
    ),
    "turn-setup": SkillDefinition(
        "turn-setup", "Turn setup", _ROOT / "planner" / "turn-setup.md",
        "Interpret a user request and set up the smallest complete workgraph that preserves its scope.",
    ),
    "turn-architecture-research": SkillDefinition(
        "turn-architecture-research", "Architecture research", _ROOT / "planner" / "turn-architecture-research.md",
        "Evidence-led product architecture, modular decomposition, and executable filesystem structure.",
    ),
    "turn-product-coherence": SkillDefinition(
        "turn-product-coherence", "Product coherence", _ROOT / "common" / "turn-product-coherence.md",
        "Keep independently produced work coherent as one user-facing product.",
    ),
    "turn-executing": SkillDefinition(
        "turn-executing", "Turn execution", _ROOT / "executor" / "turn-executing.md",
        "Concrete implementation work and CLI result handoff.",
    ),
    "turn-research": SkillDefinition(
        "turn-research", "Research", _ROOT / "executor" / "turn-research.md",
        "Evidence-led market and audience research for an assigned initiative.",
    ),
    "turn-product-design": SkillDefinition(
        "turn-product-design", "Product design", _ROOT / "executor" / "turn-product-design.md",
        "Product, UI, UX, and design-system definition from validated research.",
    ),
    "turn-integrating": SkillDefinition(
        "turn-integrating", "Turn integration", _ROOT / "integrator" / "turn-integrating.md",
        "Recompose prerequisite work into the real user-facing product.",
    ),
    "turn-verifying": SkillDefinition(
        "turn-verifying", "Turn verification", _ROOT / "verifier" / "turn-verifying.md",
        "Code and visual inspection with approve/reject decisions.",
    ),
    "turn-plan-distribution": SkillDefinition(
        "turn-plan-distribution", "Distribution planning", _ROOT / "planner" / "turn-plan-distribution.md",
        "Go-to-market and adoption planning for an assigned product or initiative.",
    ),
    "imagegen": SkillDefinition(
        "imagegen", "Concept image generation", _ROOT / "planner" / "imagegen.md",
        "Purposeful conceptual visual references for implementation and QA.",
        source_url=_AGENCY_AGENTS,
    ),
    "find-skills": SkillDefinition(
        "find-skills", "Find skills", _ROOT / "planner" / "find-skills.md",
        "Discover, evaluate, and reference project-specific agent skills.",
        source_url=_SKILLS_SH,
    ),
    "find-mcps": SkillDefinition(
        "find-mcps", "Find MCP servers", _ROOT / "planner" / "find-mcps.md",
        "Discover, evaluate, and assign domain-specific MCP server access.",
        source_url="https://glama.ai/mcp/servers",
    ),
}


def list_skills() -> tuple[SkillDefinition, ...]:
    return tuple(SKILLS[key] for key in sorted(SKILLS))


def get_skill(skill_id: str) -> SkillDefinition:
    try:
        return SKILLS[skill_id]
    except KeyError as error:
        raise ValueError(f"unknown skill: {skill_id}") from error


def is_skill_url(reference: str) -> bool:
    return _is_skill_url(reference)


def is_project_skill_reference(reference: str) -> bool:
    """Return whether ``reference`` names a skill authored in this project."""
    return _is_project_skill_reference(reference)


def validate_skill_reference(reference: str) -> str:
    """Validate the shape of a built-in, project, or external reference."""
    return _validate_skill_reference(reference)


def project_skill_path(reference: str, project_root: str | Path) -> Path:
    return Path(project_root) / ".turn" / "skills" / project_skill_slug(reference) / "SKILL.md"


def install_builtin_skill(skill_id: str, project_root: str | Path) -> Path:
    """Copy one trusted library skill into the current project."""
    skill = get_skill(skill_id)
    target = Path(project_root) / ".turn" / "skills" / skill.id / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(skill.source_path.read_bytes())
    return target


def resolve_skill_paths(
    skill_ids: list[str], project_root: str | Path, *, allow_library: bool = False
) -> dict[str, Path]:
    """Resolve project skills, optionally exposing library sources to planners."""
    resolved: dict[str, Path] = {}
    for reference in dict.fromkeys(skill_ids):
        validate_skill_reference(reference)
        if is_skill_url(reference):
            raise ValueError(
                f"external skill URL {reference!r} was not installed by the planner; "
                "submit it as project:<slug>"
            )
        if reference in SKILLS and allow_library:
            resolved[reference] = SKILLS[reference].source_path
            continue
        path = (
            Path(project_root) / ".turn" / "skills" / reference / "SKILL.md"
            if reference in SKILLS
            else project_skill_path(reference, project_root)
        )
        if not path.is_file():
            raise ValueError(
                f"{reference} is not installed at {path}; install it in the project "
                "before launching the worker"
            )
        resolved[reference] = path
    return resolved


def validate_plan_skill_files(
    payload: dict,
    project_root: str | Path | None,
    *,
    planner_skill_ids: list[str] | None = None,
) -> None:
    """Check only that every planner-selected skill exists in the project.

    This is intentionally a filesystem-presence check. Skill contents are
    authored and managed by the planner and are never parsed by Turn.
    """
    references: list[tuple[str, str]] = [
        ("planner.skill_ids", reference)
        for reference in (planner_skill_ids or [])
    ]
    for index, node in enumerate(payload.get("nodes", [])):
        if not isinstance(node, dict):
            continue
        key = str(node.get("key") or index)
        references.extend(
            (f"node {key}.skills", str(reference))
            for reference in node.get("skills", [])
        )
        agent = node.get("agent")
        if isinstance(agent, dict):
            references.extend(
                (f"node {key}.agent.skill_ids", str(reference))
                for reference in agent.get("skill_ids", [])
            )
        role = node.get("agent_type")
        if isinstance(agent, dict):
            role = agent.get("type_id") or role
        if not role:
            role = "planner" if node.get("plan") or node.get("executor") == "planner" else "executor"
        for reference in skill_ids_for_agent_type(role):
            references.append((f"node {key} role skill", reference))
    if not references:
        return
    if project_root is None:
        raise ValueError("TURN_REPO is required to check planner-installed skills")
    for location, reference in references:
        validate_skill_reference(reference)
        if is_skill_url(reference):
            raise ValueError(
                f"{location} contains external skill URL {reference!r}; the planner must "
                "install it under .turn/skills/<slug>/SKILL.md and submit project:<slug>"
            )
        path = (
            Path(project_root) / ".turn" / "skills" / reference / "SKILL.md"
            if reference in SKILLS
            else project_skill_path(reference, project_root)
        )
        if not path.is_file():
            raise ValueError(
                f"{location} {reference} is not installed at {path}; the planner must "
                "create or install the skill before submitting the plan"
            )
