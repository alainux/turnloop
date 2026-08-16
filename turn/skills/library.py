"""Skill catalog and project-scoped skill installer.

Built-in skills are addressed by stable ids. A planner may also select a
specific HTTPS/HTTP skill URL; the server fetches it once into the current
project's ``.turn/skills`` directory. Fetching is behind a small port so tests
can use deterministic content without network access.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


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
_GITHUB_AGENT_SKILLS = "https://github.com/search?q=topic%3Aagent-skills&type=repositories"
_MAX_SKILL_BYTES = 1024 * 1024
_PROJECT_SKILL_PATTERN = re.compile(r"^project:([a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)$")

SKILLS: dict[str, SkillDefinition] = {
    "turn-planning": SkillDefinition(
        "turn-planning", "Turn planning", _ROOT / "planner" / "turn-planning.md",
        "Graph decomposition, architecture metadata, contracts, and orchestration.",
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
    "turn-integrating": SkillDefinition(
        "turn-integrating", "Turn integration", _ROOT / "integrator" / "turn-integrating.md",
        "Recompose prerequisite work into the real user-facing product.",
    ),
    "turn-verifying": SkillDefinition(
        "turn-verifying", "Turn verification", _ROOT / "verifier" / "turn-verifying.md",
        "Code and visual inspection with approve/reject decisions.",
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
}


class SkillFetcher(Protocol):
    """Port used to retrieve an external skill document."""

    def fetch(self, url: str) -> bytes:
        ...


class UrlSkillFetcher:
    """Network adapter for fetching a bounded skill document."""

    def fetch(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "turn-skill-installer/1.0"})
        with urlopen(request, timeout=20) as response:
            payload = response.read(_MAX_SKILL_BYTES + 1)
        if len(payload) > _MAX_SKILL_BYTES:
            raise ValueError(f"skill at {url} exceeds {_MAX_SKILL_BYTES} bytes")
        if not payload.strip():
            raise ValueError(f"skill at {url} is empty")
        return payload


def list_skills() -> tuple[SkillDefinition, ...]:
    return tuple(SKILLS[key] for key in sorted(SKILLS))


def get_skill(skill_id: str) -> SkillDefinition:
    try:
        return SKILLS[skill_id]
    except KeyError as error:
        raise ValueError(f"unknown skill: {skill_id}") from error


def is_skill_url(reference: str) -> bool:
    parsed = urlparse(reference)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def is_project_skill_reference(reference: str) -> bool:
    """Return whether ``reference`` names a skill authored in this project."""
    return _PROJECT_SKILL_PATTERN.fullmatch(reference) is not None


def validate_skill_reference(reference: str) -> str:
    """Validate a built-in id, project skill id, or external HTTP(S) URL."""
    if reference in SKILLS:
        return reference
    if is_project_skill_reference(reference):
        return reference
    if is_skill_url(reference):
        return reference
    raise ValueError(
        f"unknown skill reference {reference!r}; use a local id, project:<slug>, or an http(s) URL"
    )


def _project_skill_path(reference: str, project_root: str | Path) -> Path:
    match = _PROJECT_SKILL_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError(f"invalid project skill reference: {reference!r}")
    return Path(project_root) / ".turn" / "skills" / match.group(1) / "SKILL.md"


def _external_install_key(url: str) -> str:
    parsed = urlparse(url)
    basename = Path(unquote(parsed.path).rstrip("/")).name or parsed.netloc
    stem = Path(basename).stem or "skill"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")[:40] or "skill"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"external-{slug}-{digest}"


def _validate_project_skill_document(path: Path, payload: bytes) -> None:
    """Require the small frontmatter contract for planner-authored skills."""
    text = payload.decode("utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"project skill {path} must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"project skill {path} has incomplete YAML frontmatter")
    frontmatter = text[4:end]
    fields = {
        line.split(":", 1)[0].strip()
        for line in frontmatter.splitlines()
        if ":" in line and not line.lstrip().startswith("#")
    }
    if not {"name", "description"}.issubset(fields):
        raise ValueError(
            f"project skill {path} frontmatter must define name and description"
        )


def materialize(
    skill_ids: list[str],
    project_root: str | Path,
    *,
    fetcher: SkillFetcher | None = None,
) -> dict[str, Path]:
    """Install selected skills into this project only, returning their paths."""
    destination = Path(project_root) / ".turn" / "skills"
    external_fetcher = fetcher or UrlSkillFetcher()
    resolved: dict[str, Path] = {}
    for reference in dict.fromkeys(skill_ids):
        validate_skill_reference(reference)
        if reference in SKILLS:
            definition = get_skill(reference)
            if not definition.source_path.is_file():
                raise FileNotFoundError(f"skill source does not exist: {definition.source_path}")
            install_key = reference
            payload = definition.source_path.read_bytes()
        elif is_project_skill_reference(reference):
            target = _project_skill_path(reference, project_root)
            if not target.is_file():
                raise FileNotFoundError(
                    f"project skill {reference} has not been authored at {target}"
                )
            _validate_project_skill_document(target, target.read_bytes())
            resolved[reference] = target
            continue
        else:
            install_key = _external_install_key(reference)
            target = destination / install_key / "SKILL.md"
            if target.is_file():
                resolved[reference] = target
                continue
            payload = external_fetcher.fetch(reference)
        target = destination / install_key / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if reference in SKILLS:
            shutil.copyfile(SKILLS[reference].source_path, target)
        else:
            target.write_bytes(payload)
        resolved[reference] = target
    return resolved
