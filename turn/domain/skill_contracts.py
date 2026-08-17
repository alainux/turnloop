"""Stable skill contracts owned by the domain layer.

This module contains only identifiers, role defaults, and reference syntax.
Filesystem discovery and project installation remain in ``turn.skills``.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

SETUP_SKILL_ID = "turn-setup"

ROLE_SKILL_IDS: dict[str, tuple[str, ...]] = {
    "planner": ("turn-planning", "imagegen", "find-skills", "find-mcps"),
    "executor": ("turn-executing",),
    "integrator": ("turn-integrating",),
    "verifier": ("turn-verifying",),
}
BUILTIN_SKILL_IDS = frozenset({
    "turn-planning",
    "turn-setup",
    "turn-architecture-research",
    "turn-product-coherence",
    "turn-executing",
    "turn-research",
    "turn-product-design",
    "turn-integrating",
    "turn-verifying",
    "turn-plan-distribution",
    "imagegen",
    "find-skills",
    "find-mcps",
})

_PROJECT_SKILL_PATTERN = re.compile(
    r"^project:([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)$"
)


def _agent_type_key(agent_type: object) -> str:
    return str(getattr(agent_type, "value", agent_type))


def skill_ids_for_agent_type(agent_type: object) -> list[str]:
    key = _agent_type_key(agent_type)
    return list(ROLE_SKILL_IDS.get(key, ()))


def skill_paths_for_agent_type(agent_type: object) -> list[str]:
    """Return the canonical source paths for built-in role skills."""
    key = _agent_type_key(agent_type)
    root = Path(__file__).resolve().parent.parent / "agents" / "skills"
    paths = {
        "planner": (
            root / "planner" / "turn-planning.md",
            root / "planner" / "imagegen.md",
            root / "planner" / "find-skills.md",
            root / "planner" / "find-mcps.md",
        ),
        "executor": (root / "executor" / "turn-executing.md",),
        "integrator": (root / "integrator" / "turn-integrating.md",),
        "verifier": (root / "verifier" / "turn-verifying.md",),
    }
    return [str(path) for path in paths.get(key, ())]


def is_skill_url(reference: str) -> bool:
    parsed = urlparse(reference)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_project_skill_reference(reference: str) -> bool:
    return _PROJECT_SKILL_PATTERN.fullmatch(reference) is not None


def project_skill_slug(reference: str) -> str:
    match = _PROJECT_SKILL_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError(f"invalid project skill reference: {reference!r}")
    return match.group(1)


def validate_skill_reference(reference: str) -> str:
    """Validate reference syntax without importing the filesystem catalog."""
    if reference in BUILTIN_SKILL_IDS or is_project_skill_reference(reference) or is_skill_url(reference):
        return reference
    raise ValueError(
        f"unknown skill reference {reference!r}; use a built-in id, project:<slug>, or an http(s) URL"
    )
