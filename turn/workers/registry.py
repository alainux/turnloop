"""Worker registry — maps executor names to Worker instances and holds the planner."""
from __future__ import annotations

from turn.config import Settings
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


def build_registry(settings: Settings, default_executor: str | None = None) -> WorkerRegistry:
    """Construct the default registry.

    `default_executor` lets callers (e.g. tests) swap Codex for a deterministic
    worker without touching the data model.
    """
    reg = WorkerRegistry()
    reg.register(EchoWorker())
    reg.register(ShellWorker())
    reg.register(CodexWorker(settings))
    reg.register(CLIHarnessWorker(HarnessKind.CLAUDE))
    reg.register(CLIHarnessWorker(HarnessKind.OPENCODE))
    reg.register(CLIHarnessWorker(HarnessKind.PI))

    executor = default_executor or settings.default_executor
    heuristic = HeuristicPlanner(default_executor=executor)
    if settings.planner == "heuristic":
        planner: Planner = heuristic
    else:
        planner = AgentPlanner(fallback=heuristic, settings=settings)
    reg.register_planner(planner)
    return reg
