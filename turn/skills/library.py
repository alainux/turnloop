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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urlparse
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
        "Graph decomposition, project documents, contracts, and orchestration.",
    ),
    "turn-setup": SkillDefinition(
        "turn-setup", "Turn setup", _ROOT / "planner" / "turn-setup.md",
        "Interpret a user request and set up the smallest sufficient workgraph.",
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


class SkillFetcher(Protocol):
    """Port used to retrieve an external skill document."""

    def fetch(self, url: str) -> bytes:
        ...

    def fetch_files(self, url: str) -> dict[str, bytes]:
        """Fetch a standard skill document and its optional support files."""
        ...


class UrlSkillFetcher:
    """Network adapter for standard single- and multi-file skill sources.

    A web page is never treated as a skill. Known repository/catalog URLs are
    resolved to their Markdown source and support files; direct URLs must
    themselves return a standards-shaped ``SKILL.md``.
    """

    def fetch(self, url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "turn-skill-installer/1.0"})
        with urlopen(request, timeout=20) as response:
            payload = response.read(_MAX_SKILL_BYTES + 1)
        if len(payload) > _MAX_SKILL_BYTES:
            raise ValueError(f"skill at {url} exceeds {_MAX_SKILL_BYTES} bytes")
        if not payload.strip():
            raise ValueError(f"skill at {url} is empty")
        return payload

    def _json(self, url: str) -> object:
        request = Request(
            url,
            headers={
                "User-Agent": "turn-skill-installer/1.0",
                "Accept": "application/vnd.github+json, application/json",
            },
        )
        with urlopen(request, timeout=20) as response:
            payload = response.read(_MAX_SKILL_BYTES + 1)
        if len(payload) > _MAX_SKILL_BYTES:
            raise ValueError(f"skill source at {url} exceeds {_MAX_SKILL_BYTES} bytes")
        return json.loads(payload.decode("utf-8"))

    def fetch_files(self, url: str) -> dict[str, bytes]:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host == "github.com" or host.endswith(".github.com"):
            return self._github_files(parsed)
        if host == "skills.sh" or host.endswith(".skills.sh"):
            return self._skills_sh_files(parsed)
        return {"SKILL.md": self.fetch(url)}

    def _github_files(self, parsed) -> dict[str, bytes]:
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 4 or parts[2] not in {"tree", "blob"}:
            raise ValueError(
                "GitHub skill references must target a /tree/<ref>/<skill-directory> "
                "or /blob/<ref>/SKILL.md source"
            )
        owner, repo, kind, ref = parts[:4]
        path = "/".join(parts[4:])
        if kind == "blob":
            if Path(path).name != "SKILL.md":
                raise ValueError("GitHub skill files must be named SKILL.md")
            raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{quote(ref, safe='')}/{quote(path, safe='/')}"
            return {"SKILL.md": self.fetch(raw)}

        api = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contents"
        if path:
            api += "/" + quote(path, safe="/")
        api += "?ref=" + quote(ref, safe="")
        return self._github_directory(api, "")

    def _github_directory(self, url: str, prefix: str) -> dict[str, bytes]:
        listing = self._json(url)
        if isinstance(listing, dict) and listing.get("type") == "file":
            name = str(listing.get("name") or Path(urlparse(url).path).name)
            if name != "SKILL.md":
                raise ValueError("GitHub skill files must be named SKILL.md")
            download = listing.get("download_url")
            if not isinstance(download, str):
                raise ValueError("GitHub skill file has no download URL")
            return {prefix + name: self.fetch(download)}
        if not isinstance(listing, list):
            raise ValueError("GitHub skill directory response is not a file listing")
        files: dict[str, bytes] = {}
        for entry in listing:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if not name or name in {".git", ".github"}:
                continue
            entry_type = entry.get("type")
            entry_url = entry.get("url")
            relative = prefix + name
            if entry_type == "dir" and isinstance(entry_url, str):
                files.update(self._github_directory(entry_url, relative + "/"))
            elif entry_type == "file":
                download = entry.get("download_url")
                if isinstance(download, str):
                    files[relative] = self.fetch(download)
        return files

    def _skills_sh_files(self, parsed) -> dict[str, bytes]:
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] == "api":
            raise ValueError("skills.sh references must identify a specific skill")
        source = "/".join(parts[:2])
        skill = "/".join(parts[2:])
        endpoint = (
            "https://skills.sh/api/v1/skills/"
            f"{quote(source, safe='/')}/{quote(skill, safe='/')}"
        )
        payload = self._json(endpoint)
        raw_files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(raw_files, list):
            raise ValueError("skills.sh response did not contain a file tree")
        files: dict[str, bytes] = {}
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or item.get("name") or "")
            content = item.get("content", item.get("contents"))
            if path and isinstance(content, str):
                files[path] = content.encode("utf-8")
        return files


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
    if re.match(r"\s*<(?:!doctype\s+html|html|head|body)\b", text, re.I):
        raise ValueError(f"skill {path} resolved to HTML; provide its Markdown source")
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
                try:
                    _validate_project_skill_document(target, target.read_bytes())
                except (UnicodeDecodeError, ValueError):
                    # Replace an older invalid materialization, including the
                    # raw HTML written by the previous installer.
                    pass
                else:
                    resolved[reference] = target
                    continue
            fetch_files = getattr(external_fetcher, "fetch_files", None)
            files = (
                fetch_files(reference)
                if callable(fetch_files)
                else {"SKILL.md": external_fetcher.fetch(reference)}
            )
            if not files:
                raise ValueError(f"skill source {reference} returned no files")
            skill_paths = [path for path in files if Path(path).name == "SKILL.md"]
            if len(skill_paths) != 1:
                raise ValueError(
                    f"skill source {reference} must resolve to exactly one SKILL.md"
                )
            skill_root = Path(skill_paths[0]).parent
            normalized_files: dict[str, bytes] = {}
            for relative, content in files.items():
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise ValueError(f"skill source {reference} contains an unsafe path")
                normalized = relative_path.relative_to(skill_root) if skill_root != Path(".") else relative_path
                normalized_files[str(normalized)] = content
            payload = normalized_files.get("SKILL.md")
            if payload is None:
                raise ValueError(f"skill source {reference} did not contain SKILL.md")
        target = destination / install_key / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        if reference in SKILLS:
            shutil.copyfile(SKILLS[reference].source_path, target)
        else:
            _validate_project_skill_document(target, payload)
            for relative, content in normalized_files.items():
                output = target.parent / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(content)
        resolved[reference] = target
    return resolved
