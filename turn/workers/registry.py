"""Worker registry — maps executor names to Worker instances and holds the planner."""
from __future__ import annotations

from turn.config import REAL_HARNESSES, Settings
from turn.workers.base import Planner, Worker
from turn.workers.codex_worker import CodexWorker
from turn.workers.deterministic_worker import DeterministicPlanner, DeterministicWorker
from turn.workers.mock_harness import MockHarnessPlanner, MockHarnessWorker
from turn.workers.planner import AgentPlanner, HeuristicPlanner
from turn.workers.shell_worker import ShellWorker
from turn.workers.harnesses import CLIHarnessWorker
from turn.domain.schemas import HarnessKind


class WorkerRegistry:
    def __init__(self):
        self.workers: dict[str, Worker] = {}
        self.planner: Planner | None = None
        self.planners: dict[str, Planner] = {}

    def register(self, worker: Worker) -> None:
        self.workers[worker.name] = worker

    def get(self, name: str | None) -> Worker | None:
        if name is None:
            return None
        return self.workers.get(name)

    def register_planner(self, planner: Planner, *, key: str | None = None) -> None:
        self.planner = planner
        self.planners[planner.name] = planner
        if key is not None:
            self.planners[key] = planner

    def get_planner(self, key: str) -> Planner | None:
        return self.planners.get(key)


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
        reg.register(DeterministicWorker())
        reg.register(MockHarnessWorker(settings))
    elif settings.planner != "codex":
        raise RuntimeError(
            "non-Codex planners are test-only; construct the registry with test_mode=True"
        )
    elif executor not in REAL_HARNESSES:
        raise RuntimeError(
            f"non-real executor '{executor}' is test-only; choose a real harness"
        )

    # Keep the real planner available in test mode as well. A test server may
    # host mock fixtures and a real-data project at the same time; the
    # project's explicit harness must decide which planner is used.
    reg.register_planner(AgentPlanner(settings=settings), key="real")

    if test_mode and settings.planner == "heuristic":
        planner: Planner = HeuristicPlanner(default_executor=executor, settings=settings)
    elif test_mode and settings.planner == "deterministic":
        planner = DeterministicPlanner()
    elif test_mode and settings.planner == "mock":
        planner = MockHarnessPlanner(settings)
    else:
        planner = AgentPlanner(settings=settings)
    reg.register_planner(planner, key=settings.planner)
    return reg
