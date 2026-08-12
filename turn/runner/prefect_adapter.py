"""Prefect 3 thin execution adapter (optional).

Turn owns the workgraph and node state. Prefect does not. This adapter maps one
node Run to one Prefect-managed execution so Prefect can provide retries,
timeouts, scheduling, and worker infrastructure without leaking Prefect concepts
into Turn's data model.

Prefect is an OPTIONAL dependency. The adapter is only selected when
``TURN_EXECUTION_BACKEND=prefect`` and Prefect is importable. Otherwise the
runner uses `DirectExecutionAdapter`.
"""
from __future__ import annotations

from typing import Optional

from turn.config import Settings
from turn.workers.base import NodeExecutionContext, Worker
from turn.domain.schemas import WorkerResult


class PrefectExecutionAdapter:
    """Runs a worker inside a Prefect flow (ephemeral, in-process by default)."""

    def __init__(self, settings: Settings):
        self.s = settings

    async def run(self, worker: Worker, ctx: NodeExecutionContext, timeout: float) -> WorkerResult:
        from prefect import flow, task

        w = worker
        c = ctx

        @task(
            retries=max(self.s.max_retries, 0),
            retry_delay_seconds=2,
            timeout_seconds=timeout,
        )
        async def _run_task() -> WorkerResult:
            return await w.execute(c)

        @flow(name=f"turn-node-{c.node.id}")
        async def _run_flow() -> WorkerResult:
            return await _run_task()

        # Ephemeral orchestration: no Prefect server required.
        return await _run_flow()


def get_execution_adapter(settings: Settings):
    """Return the configured execution adapter.

    Falls back to the direct adapter if Prefect is requested but unavailable, so
    the system always has a working backend.
    """
    if settings.execution_backend == "prefect":
        try:
            import prefect  # noqa: F401

            return PrefectExecutionAdapter(settings)
        except ImportError:
            print("[turn] Prefect backend requested but not installed; using direct backend.")
    from turn.runner.runner import DirectExecutionAdapter

    return DirectExecutionAdapter(settings)
