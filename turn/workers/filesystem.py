"""Project-directory helpers.

Workers intentionally share the project directory assigned to the graph. Turn
does not initialize, inspect, branch, merge, or otherwise manage version
control; that remains an end-user concern.
"""
from __future__ import annotations

from pathlib import Path


def init_project_directory(
    root_id,
    working_dir: str | None = None,
    projects_dir: str | None = None,
) -> str:
    """Create and return the directory assigned to a project."""
    if working_dir:
        project = Path(working_dir).expanduser().resolve()
    else:
        base = Path(projects_dir or (Path.cwd() / "projects")).expanduser().resolve()
        project = base / f"proj-{root_id.hex[:8]}"
    project.mkdir(parents=True, exist_ok=True)
    return str(project)
