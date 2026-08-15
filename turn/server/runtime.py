"""Server-owned application runtime and dependency composition root."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from turn.config import Settings, test_modes_enabled, validate_server_settings
from turn.db.store import Store
from turn.runner.events import EventBus
from turn.runner.prefect_adapter import get_execution_adapter
from turn.runner.runner import Runner
from turn.workers.herdr import HerdrAdapter
from turn.workers.registry import WorkerRegistry, build_registry


@dataclass(frozen=True)
class RuntimeComponents:
    """The dependencies exposed to transport adapters after startup."""

    store: Store
    events: EventBus
    runner: Runner
    test_mode: bool


class TurnRuntime:
    """Own the process-wide Turn services.

    FastAPI is only a transport surface. This object restores settings,
    selects the production/test registry, creates the Herdr-backed runner,
    and owns startup/shutdown. Tests can replace each port without launching
    a provider or invoking the Herdr CLI.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        store: Store | None = None,
        events: EventBus | None = None,
        registry: WorkerRegistry | None = None,
        execution_adapter: Any | None = None,
        herdr_adapter: HerdrAdapter | None = None,
        test_mode: bool | None = None,
    ):
        self.settings = settings
        self.store = store or Store(settings.data_dir)
        self.events = events or EventBus()
        self._registry = registry
        self._execution_adapter = execution_adapter
        self._herdr_adapter = herdr_adapter
        self.test_mode = test_modes_enabled() if test_mode is None else test_mode
        self.runner: Runner | None = None
        self._started = False

    async def start(self) -> RuntimeComponents:
        if self._started:
            return self.components
        await self.store.init()
        await self._restore_settings()
        if not self.test_mode:
            validate_server_settings(self.settings)
        registry = self._registry or build_registry(self.settings, test_mode=self.test_mode)
        adapter = self._execution_adapter or get_execution_adapter(self.settings)
        self.runner = Runner(
            self.store,
            registry,
            self.events,
            self.settings,
            adapter,
            herdr_adapter=self._herdr_adapter,
        )
        await self.runner.start()
        self._started = True
        return self.components

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.stop()
        if self._started:
            await self.store.dispose()
        self._started = False

    @property
    def components(self) -> RuntimeComponents:
        if self.runner is None:
            raise RuntimeError("Turn runtime has not been started")
        return RuntimeComponents(self.store, self.events, self.runner, self.test_mode)

    async def _restore_settings(self) -> None:
        """Restore durable preferences before constructing the runner."""
        casts = {
            "max_retries": int,
            "retry_backoff_ms": int,
            "delay_between_jobs_ms": int,
            "timeout_seconds": float,
            "stall_timeout_seconds": float,
            "retry_choked_models": lambda v: str(v).lower() in ("1", "true", "yes"),
        }
        targets = {
            "max_retries": "max_retries",
            "retry_backoff_ms": "retry_backoff_ms",
            "delay_between_jobs_ms": "delay_between_jobs_ms",
            "timeout_seconds": "default_run_timeout_seconds",
            "stall_timeout_seconds": "stall_timeout_seconds",
            "retry_choked_models": "retry_choked_models",
        }
        for key, cast in casts.items():
            raw = await self.store.get_setting(key)
            if raw is not None:
                setattr(self.settings, targets[key], cast(raw))
        stored = {
            "default_harness": "default_executor",
            "default_model": "codex_model",
            "reasoning": "default_reasoning",
            "permission": "default_permission",
        }
        for key, target in stored.items():
            value = await self.store.get_setting(key)
            if value:
                setattr(self.settings, target, value)
