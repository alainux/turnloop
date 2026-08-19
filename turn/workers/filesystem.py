"""Project-directory helpers.

Workers intentionally share the independent project directory assigned to the
graph. Turn initializes the directory's Git root but does not inspect, branch,
or merge user changes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_AGENTS_CONTENT = "# Turn project\n\nThis is a Turn project.\n"


def _scaffold_project_agents(project: Path) -> None:
    """Add the minimal project marker without replacing user instructions."""
    agents = project / "AGENTS.md"
    if agents.exists() or agents.is_symlink():
        return
    try:
        with agents.open("x", encoding="utf-8") as handle:
            handle.write(PROJECT_AGENTS_CONTENT)
    except FileExistsError:
        # Another creator won the race; preserve the file it created.
        return


def _initialize_git_repository(project: Path) -> None:
    """Make the assigned directory an independent project root."""
    git_path = project / ".git"
    if git_path.exists() or git_path.is_symlink():
        return
    try:
        subprocess.run(
            ["git", "init", "--quiet"],
            cwd=str(project),
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError(f"could not initialize Git repository in {project}") from error


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
    _initialize_git_repository(project)
    _scaffold_project_agents(project)
    return str(project)
