"""Local capability-plugin catalog and project loading operations."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from turn.capabilities.plugin import CapabilityPlugin, CapabilityPluginError, load_capability_plugin
from turn.domain.capability_contracts import capability_ids_for_agent_type


@dataclass(frozen=True)
class CapabilityCatalogEntry:
    id: str
    description: str
    version: str | None
    path: Path
    skill_count: int
    mcp_count: int
    builtin: bool
    score: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "description": self.description,
            "version": self.version,
            "path": str(self.path),
            "skills": self.skill_count,
            "mcps": self.mcp_count,
            "builtin": self.builtin,
            **({"score": self.score} if self.score is not None else {}),
        }


class CapabilityCatalog:
    """A deterministic catalog backed by a local directory.

    Packaged capabilities are read-only and user-authored packages are copied
    into ``root``. Loading a catalog entry into a project is a separate copy
    into ``<project>/.turn/capabilities``; it never installs a harness file.
    """

    def __init__(self, root: str | Path, *, builtin_root: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        self.builtin_root = Path(builtin_root or Path(__file__).resolve().parent / "builtin").resolve()

    def _packages(self) -> dict[str, CapabilityPlugin]:
        packages: dict[str, CapabilityPlugin] = {}
        for base, builtin in ((self.builtin_root, True), (self.root, False)):
            if not base.is_dir():
                continue
            for candidate in sorted(base.iterdir()):
                if not candidate.is_dir() or not (candidate / "plugin.json").is_file():
                    continue
                try:
                    package = load_capability_plugin(candidate)
                except CapabilityPluginError:
                    continue
                # Local catalog entries intentionally override a packaged id.
                packages[package.id] = package
        return packages

    def list(self) -> list[CapabilityCatalogEntry]:
        packages = self._packages()
        return [self._entry(package) for package in sorted(packages.values(), key=lambda item: item.id)]

    def search(self, query: str = "") -> list[CapabilityCatalogEntry]:
        query = query.strip().lower()
        entries = self.list()
        if not query:
            return entries
        scored: list[CapabilityCatalogEntry] = []
        for entry in entries:
            haystack = f"{entry.id} {entry.description}".lower()
            tokens = haystack.split()
            exact = 1.0 if query in haystack else 0.0
            token_score = max((SequenceMatcher(None, query, token).ratio() for token in tokens), default=0.0)
            score = max(exact, token_score)
            if score >= 0.35:
                scored.append(CapabilityCatalogEntry(**{**entry.__dict__, "score": round(score, 4)}))
        return sorted(scored, key=lambda item: (-float(item.score or 0), item.id))

    def get(self, capability_id: str) -> CapabilityPlugin:
        package = self._packages().get(capability_id)
        if package is None:
            raise CapabilityPluginError(f"capability plugin not found in local catalog: {capability_id}")
        return package

    def delete(self, capability_id: str) -> Path:
        """Delete one user-authored catalog package.

        Built-in packages are source-controlled and are never removable through
        the catalog API. A local package must resolve to exactly one immediate
        child of the catalog root before it can be deleted.
        """
        package = self.get(capability_id)
        if package.path.is_relative_to(self.builtin_root):
            raise CapabilityPluginError(
                f"cannot delete packaged capability from the local catalog: {capability_id}"
            )

        candidate = (self.root / capability_id).resolve()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as error:
            raise CapabilityPluginError(
                f"catalog capability path escapes the catalog root: {candidate}"
            ) from error
        if len(relative.parts) != 1 or relative.name != capability_id or candidate != package.path.resolve():
            raise CapabilityPluginError(f"catalog capability path is not removable: {candidate}")
        if not candidate.is_dir() or not (candidate / "plugin.json").is_file():
            raise CapabilityPluginError(f"catalog capability is not a package directory: {candidate}")

        shutil.rmtree(candidate)
        return candidate

    def import_directory(self, source: str | Path) -> CapabilityPlugin:
        """Copy a planner-authored plugin into the local catalog."""
        package = load_capability_plugin(source)
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.root / package.id
        if destination.exists() and destination.resolve() != package.path:
            shutil.copytree(package.path, destination, dirs_exist_ok=True)
        elif not destination.exists():
            shutil.copytree(package.path, destination)
        return load_capability_plugin(destination)

    def load_into_project(self, capability_id: str, project_root: str | Path) -> Path:
        """Copy a catalog package to the project-scoped capability set."""
        package = self.get(capability_id)
        destination_root = Path(project_root).expanduser().resolve() / ".turn" / "capabilities"
        destination = destination_root / package.id
        destination_root.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = load_capability_plugin(destination)
            if existing.id != package.id:
                raise CapabilityPluginError(f"project capability path is occupied: {destination}")
        else:
            shutil.copytree(package.path, destination)
        load_capability_plugin(destination)
        return destination

    def resolve_project(self, capability_id: str, project_root: str | Path) -> CapabilityPlugin:
        path = Path(project_root).expanduser().resolve() / ".turn" / "capabilities" / capability_id
        if not path.is_dir():
            raise CapabilityPluginError(
                f"capability {capability_id!r} is not loaded in this project; use `turn capabilities load {capability_id}`"
            )
        package = load_capability_plugin(path)
        if package.id != capability_id:
            raise CapabilityPluginError(f"capability directory id mismatch: {path}")
        return package

    def load_plan_role_capabilities(self, payload: dict, project_root: str | Path) -> None:
        """Materialize Turn's role contract before validating a plan.

        Role capabilities are implicit in an agent specialization. They are
        system-owned project packages, unlike the planner's explicit domain
        selections, so they are loaded automatically at the plan boundary.
        """
        for node in payload.get("nodes", []):
            if not isinstance(node, dict):
                continue
            agent = node.get("agent")
            role = (agent or {}).get("type_id") if isinstance(agent, dict) else None
            role = role or node.get("agent_type") or (
                "planner" if node.get("plan") or node.get("executor") == "planner" else "executor"
            )
            for capability_id in capability_ids_for_agent_type(role):
                self.load_into_project(capability_id, project_root)

    def validate_plan(self, payload: dict, project_root: str | Path, planner_capabilities: list[str] | None = None) -> None:
        """Require every capability named by a plan to be loaded in the project."""
        references: list[tuple[str, str]] = [
            ("planner capability", item) for item in (planner_capabilities or [])
        ]
        for index, node in enumerate(payload.get("nodes", [])):
            if not isinstance(node, dict):
                continue
            key = str(node.get("key") or index)
            references.extend((f"node {key}.capabilities", item) for item in node.get("capabilities", []))
            agent = node.get("agent")
            if isinstance(agent, dict):
                references.extend((f"node {key}.agent.capabilities", item) for item in agent.get("capabilities", []))
            role = (agent or {}).get("type_id") if isinstance(agent, dict) else None
            role = role or node.get("agent_type") or ("planner" if node.get("plan") or node.get("executor") == "planner" else "executor")
            references.extend((f"node {key} role capability", item) for item in capability_ids_for_agent_type(role))
        for location, capability_id in references:
            self.resolve_project(str(capability_id), project_root)

    @staticmethod
    def _entry(package: CapabilityPlugin) -> CapabilityCatalogEntry:
        builtin_root = Path(__file__).resolve().parent / "builtin"
        return CapabilityCatalogEntry(
            id=package.id,
            description=package.description,
            version=package.version,
            path=package.path,
            skill_count=package.skill_count,
            mcp_count=package.mcp_count,
            builtin=package.path.is_relative_to(builtin_root),
        )
