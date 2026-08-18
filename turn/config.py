"""Runtime configuration for Turn.

Turn keeps durable preferences in ``./turn/config.json`` by default and keeps
each project's graph in that project's own ``.turn/state.json`` file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


REAL_HARNESSES = frozenset({"codex", "claude", "opencode", "pi"})
TEST_ONLY_PLANNERS = frozenset({"heuristic", "echo", "fake"})
TEST_ONLY_EXECUTORS = frozenset({"echo", "fake"})


def test_modes_enabled() -> bool:
    return os.getenv("TURN_TEST_MODE", "").lower() in {"1", "true", "yes"}


def _default_agent_defaults() -> dict[str, dict[str, str]]:
    """Return explicit defaults for every built-in agent specialization."""
    shared = {
        "harness": os.getenv("TURN_DEFAULT_EXECUTOR", "codex"),
        "model": os.getenv("TURN_CODEX_MODEL", ""),
        "reasoning": os.getenv("TURN_REASONING", "default"),
    }
    return {
        role: dict(shared)
        for role in ("planner", "executor", "integrator", "verifier")
    }


def validate_server_settings(config: "Settings") -> None:
    """Reject deterministic/test-only modes at the served-app boundary."""
    if config.planner in TEST_ONLY_PLANNERS:
        planner_name = f"{config.planner} planning"
        raise RuntimeError(
            f"{planner_name} is test-only; the served app requires TURN_PLANNER=codex"
        )
    if config.planner != "codex":
        raise RuntimeError(
            f"unsupported planner '{config.planner}'; the served app requires TURN_PLANNER=codex"
        )
    if config.default_executor in TEST_ONLY_EXECUTORS:
        raise RuntimeError(
            "deterministic Echo execution is test-only; the served app requires a real harness"
        )
    if config.default_executor not in REAL_HARNESSES:
        raise RuntimeError(
            f"unsupported default executor '{config.default_executor}'; choose a real harness"
        )


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
    data_dir: str = field(
        default_factory=lambda: os.getenv("TURN_DATA_DIR", str(Path.cwd() / "turn"))
    )

    # --- execution -------------------------------------------------------
    # "direct" runs workers in-process (with timeout/cancel handling).
    # "prefect" wraps each node Run in a Prefect flow (optional dependency).
    execution_backend: str = field(
        default_factory=lambda: os.getenv("TURN_EXECUTION_BACKEND", "direct")
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
    stall_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("TURN_STALL_TIMEOUT", "90"))
    )
    # A quiet terminal is only considered for cleanup while detached from the
    # browser. The warning is informational; the reaper is the actual grace
    # period and never runs while a terminal client is attached.
    terminal_idle_warning_seconds: float = field(
        default_factory=lambda: float(os.getenv("TURN_TERMINAL_IDLE_WARNING", "300"))
    )
    terminal_idle_reap_seconds: float = field(
        default_factory=lambda: float(os.getenv("TURN_TERMINAL_IDLE_REAP", "1800"))
    )
    delay_between_jobs_ms: int = field(
        default_factory=lambda: int(os.getenv("TURN_JOB_DELAY_MS", "0"))
    )
    retry_backoff_ms: int = field(
        default_factory=lambda: int(os.getenv("TURN_RETRY_BACKOFF_MS", "750"))
    )
    retry_choked_models: bool = field(
        default_factory=lambda: os.getenv("TURN_RETRY_CHOKED", "1").lower() in ("1", "true", "yes")
    )

    # --- workers ---------------------------------------------------------
    codex_binary: str = field(
        default_factory=lambda: os.getenv("TURN_CODEX_BIN", "codex")
    )
    codex_model: str | None = field(
        default_factory=lambda: os.getenv("TURN_CODEX_MODEL")
    )
    default_reasoning: str = field(
        default_factory=lambda: os.getenv("TURN_REASONING", "default")
    )
    default_executor: str = field(
        default_factory=lambda: os.getenv("TURN_DEFAULT_EXECUTOR", "codex")
    )
    # Role-specific defaults are persisted and exposed as one explicit
    # contract. The older process-level fields remain inputs for startup and
    # CLI execution, while new agents resolve through this map.
    agent_defaults: dict[str, dict[str, str]] = field(
        default_factory=_default_agent_defaults
    )
    # "codex" is the real planner. "heuristic" is opt-in for offline tests;
    # production never silently substitutes it.
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
    # Where NEW projects are created by default. Each project becomes its own
    # assigned directory under this directory (e.g. ./projects/<id>/). The
    # default is deliberately repo-local so a local Turn server never creates
    # project files in a temporary system directory. Version control is
    # managed by the end user outside Turn.
    projects_dir: str = field(
        default_factory=lambda: os.getenv("TURN_PROJECTS_DIR", str(Path.cwd() / "projects"))
    )

# A single process-wide settings instance.
_load_env_file()
settings = Settings()
