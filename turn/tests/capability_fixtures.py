from __future__ import annotations

from pathlib import Path

from turn.capabilities.catalog import CapabilityCatalog


BUILTIN_ROOT = Path(__file__).resolve().parents[1] / "capabilities" / "builtin"


def load_builtin_capabilities(project_root: str | Path, ids: list[str] | None = None) -> None:
    catalog = CapabilityCatalog(Path(project_root) / ".capability-test-catalog", builtin_root=BUILTIN_ROOT)
    selected = ids or [entry.id for entry in catalog.list()]
    for capability_id in selected:
        catalog.load_into_project(capability_id, project_root)
