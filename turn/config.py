"""Runtime configuration for Turn.

All knobs are read from environment variables. SQLite is the default local
store and requires zero external services; TURN_DATABASE_URL can point the
same SQLAlchemy store at another database if desired.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_env_file() -> None:
    """Best-effort load of a repo-local ``.env`` (optional, no hard dependency).

    Inline process environment variables always take precedence over values
    found in ``.env``.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        from dotenv import load_dotenv  # type: ignore

        if path.exists():
            load_dotenv(str(path))
        return
    except Exception:
        pass
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass
class Settings:
    # --- storage ---------------------------------------------------------
    # Postgres example: postgresql+asyncpg://user:pass@localhost:5432/turn
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "TURN_DATABASE_URL", "sqlite+aiosqlite:///./turnloop.db"
        )
    )

    # --- execution -------------------------------------------------------
    # "direct" runs workers in-process (with timeout/cancel handling).
    # "prefect" wraps each node Run in a Prefect flow (optional dependency).
    execution_backend: str = field(
        default_factory=lambda: os.getenv("TURN_EXECUTION_BACKEND", "direct")
    )
    max_concurrency: int = field(
        default_factory=lambda: int(os.getenv("TURN_MAX_CONCURRENCY", "4"))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.getenv("TURN_MAX_RETRIES", "1"))
    )
    runner_tick_seconds: float = field(
        default_factory=lambda: float(os.getenv("TURN_RUNNER_TICK", "0.5"))
    )
    default_run_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("TURN_RUN_TIMEOUT", "600"))
    )

    # --- workers ---------------------------------------------------------
    codex_binary: str = field(
        default_factory=lambda: os.getenv("TURN_CODEX_BIN", "codex")
    )
    codex_model: str | None = field(
        default_factory=lambda: os.getenv("TURN_CODEX_MODEL")
    )
    codex_args: list[str] = field(
        default_factory=lambda: os.getenv("TURN_CODEX_ARGS", "").split()
    )
    default_executor: str = field(
        default_factory=lambda: os.getenv("TURN_DEFAULT_EXECUTOR", "codex")
    )
    # "codex" (Codex-backed, with heuristic fallback) or "heuristic" (offline)
    planner: str = field(
        default_factory=lambda: os.getenv("TURN_PLANNER", "codex")
    )

    # --- planning / model ------------------------------------------------
    openai_api_key: str | None = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY")
        or os.getenv("CODEX_API_KEY")
    )
    openai_model: str = field(
        default_factory=lambda: os.getenv("TURN_PLANNER_MODEL", "gpt-4o-mini")
    )
    openai_base_url: str | None = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL")
    )

    # --- repo / resources ------------------------------------------------
    # Optional FALLBACK path to a software repository used by the codex worker
    # for software branches. Each project normally carries its OWN repo_path
    # (a per-project git repo under projects_dir) recorded on its root node,
    # so this only applies to projects that were created before per-project
    # repos existed. Leave it unset to run projects in stand-alone temp trees.
    repo_path: str | None = field(default_factory=lambda: os.getenv("TURN_REPO_PATH"))

    # Where NEW projects are created by default. Each project becomes its own
    # git repository under this directory (e.g. ./projects/<id>/), so the
    # user is left with a real, initialized repo they can keep or delete.
    projects_dir: str = field(
        default_factory=lambda: os.getenv("TURN_PROJECTS_DIR", "./projects")
    )

    # Optional directory of project-local skills / instructions. Files here
    # are inherited by descendant nodes as resources.
    skills_dir: str | None = field(default_factory=lambda: os.getenv("TURN_SKILLS_DIR"))

    # When True, a node whose worktree has been merged up into its parent is
    # automatically accepted: its (now-redundant) subtree worktree is deleted
    # without waiting for a manual review. When False (default), the user must
    # Accept/Reject each merged node from the UI. This is a global user option.
    auto_accept_merges: bool = field(
        default_factory=lambda: (os.getenv("TURN_AUTO_ACCEPT_MERGES", "0").lower() in ("1", "true", "yes"))
    )


# A single process-wide settings instance.
_load_env_file()
settings = Settings()
