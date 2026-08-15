"""Worker registry — maps executor names to Worker instances and holds the planner."""
from __future__ import annotations

from turn.config import REAL_HARNESSES, Settings
from turn.workers.base import Planner, Worker
from turn.workers.codex_worker import CodexWorker
from turn.workers.echo_worker import EchoWorker
from turn.workers.planner import AgentPlanner, HeuristicPlanner
from turn.workers.shell_worker import ShellWorker
from turn.workers.harnesses import CLIHarnessWorker
from turn.domain.schemas import HarnessKind


class WorkerRegistry:
    def __init__(self):
        self.workers: dict[str, Worker] = {}
        self.planner: Planner | None = None

    def register(self, worker: Worker) -> None:
        self.workers[worker.name] = worker

    def get(self, name: str | None) -> Worker | None:
        if name is None:
            return None
        return self.workers.get(name)

    def register_planner(self, planner: Planner) -> None:
        self.planner = planner


def build_registry(
    settings: Settings,
    default_executor: str | None = None,
    *,
    test_mode: bool = False,
) -> WorkerRegistry:
    """Construct the default registry.

    ``test_mode`` is the only path that registers deterministic workers or the
    heuristic planner. The served application never enables it.
    """
    reg = WorkerRegistry()
    reg.register(ShellWorker())
    reg.register(CodexWorker(settings))
    reg.register(CLIHarnessWorker(HarnessKind.CLAUDE, settings))
    reg.register(CLIHarnessWorker(HarnessKind.OPENCODE, settings))
    reg.register(CLIHarnessWorker(HarnessKind.PI, settings))

    executor = default_executor or settings.default_executor
    if test_mode:
        reg.register(EchoWorker())
    elif settings.planner != "codex":
        raise RuntimeError(
            "non-Codex planners are test-only; construct the registry with test_mode=True"
        )
    elif executor not in REAL_HARNESSES:
        raise RuntimeError(
            f"non-real executor '{executor}' is test-only; choose a real harness"
        )

    if test_mode and settings.planner == "heuristic":
        planner: Planner = HeuristicPlanner(default_executor=executor)
    else:
        planner = AgentPlanner(settings=settings)
    reg.register_planner(planner)
    return reg
