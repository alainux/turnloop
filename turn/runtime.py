"""Server-owned application runtime and dependency composition root."""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import json
import uuid
from typing import Any

from turn.config import Settings, test_modes_enabled, validate_server_settings
from turn.db.store import Store
from turn.runner.events import EventBus
from turn.logging import EventLog
from turn.runner.prefect_adapter import get_execution_adapter
from turn.runner.runner import Runner
from turn.runner.triggers import TriggerDispatcher
from turn.workers.herdr import HerdrAdapter
from turn.workers.registry import WorkerRegistry, build_registry
from turn.workers.harnesses import harness_capabilities
from turn.mock_workflows import mock_workflows_enabled, seed_mock_workflows


@dataclass(frozen=True)
class RuntimeComponents:
    """The dependencies exposed to transport adapters after startup."""

    store: Store
    events: EventBus
    logs: EventLog
    runner: Runner
    test_mode: bool
    capabilities: list[dict[str, Any]]


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
        terminal_transport: Any | None = None,
        test_mode: bool | None = None,
    ):
        self.settings = settings
        self.logs = EventLog(settings.data_dir, settings.log_max_records)
        self.store = store or Store(settings.data_dir, projects_dir=settings.projects_dir, logs=self.logs)
        if store is not None and getattr(self.store, "logs", None) is None:
            self.store.logs = self.logs
        self.events = events or EventBus(self.logs)
        if events is not None and getattr(self.events, "logs", None) is None:
            self.events.logs = self.logs
        self._registry = registry
        self._execution_adapter = execution_adapter
        self._herdr_adapter = herdr_adapter
        self._terminal_transport = terminal_transport
        self.test_mode = test_modes_enabled() if test_mode is None else test_mode
        self.runner: Runner | None = None
        self.capabilities: list[dict[str, Any]] = []
        self._started = False
        self.triggers = TriggerDispatcher(
            self.store,
            self.events,
            self.logs,
            settings.data_dir,
        )
        self.store.set_event_sink(self.triggers.emit)

    async def start(self) -> RuntimeComponents:
        if self._started:
            return self.components
        try:
            await self.store.init()
            await self._restore_settings()
            self.logs.set_max_records(self.settings.log_max_records)
            if not self.test_mode:
                validate_server_settings(self.settings)
            # Discover provider capabilities once during daemon startup. The UI
            # reads this server-owned snapshot instead of repeatedly launching
            # provider discovery subprocesses on every page load.
            self.capabilities = await asyncio.to_thread(
                harness_capabilities,
                {"codex": self.settings.codex_model or ""},
                {"codex": self.settings.codex_binary},
            )
            if self.test_mode:
                from turn.workers.mock_harness import mock_harness_script

                self.capabilities.append({
                    "id": "mock", "label": "Mock harness", "binary": mock_harness_script(),
                    "reasoning": ["default"],
                    "models": [{"id": "deterministic", "label": "Deterministic", "reasoning": ["default"], "source": "test"}],
                    "supports_sessions": True, "supports_tools": False,
                    "accepts_custom_models": False, "reasoning_profiles": [],
                    "available": True,
                })
            registry = self._registry or build_registry(self.settings, test_mode=self.test_mode)
            adapter = self._execution_adapter or get_execution_adapter(self.settings)
            self.runner = Runner(
                self.store,
                registry,
                self.events,
                self.settings,
                adapter,
                herdr_adapter=self._herdr_adapter,
                terminal_transport=self._terminal_transport,
                trigger_dispatcher=self.triggers,
            )
            if not self.test_mode:
                # Production organization boundaries receive a fresh semantic
                # plan audit and a retained-session manager review. Test mode
                # stays provider-neutral unless a test injects callbacks into
                # Runner directly.
                self.runner.provider_reviews_enabled = True
                self.runner.manager_reviewer = self.runner._provider_manager_review
            self.triggers.set_wake(self.runner.wake)
            await self.runner.start()
            await self.triggers.start()
            if self.test_mode and mock_workflows_enabled():
                created = await seed_mock_workflows(self.store)
                for project_id in created:
                    await self.runner.ensure_node_terminal(uuid.UUID(project_id))
                self.runner.wake()
            self._started = True
            return self.components
        except BaseException:
            await self._cleanup_failed_start()
            raise

    async def _cleanup_failed_start(self) -> None:
        """Release partially started services when lifespan startup fails."""
        await self.triggers.stop()
        if self.runner is not None:
            await self.runner.stop(close_workspaces=self.test_mode)
        await self.store.dispose()
        self.runner = None
        self._started = False

    async def stop(self) -> None:
        await self.triggers.stop()
        if self.runner is not None:
            await self.runner.stop(close_workspaces=self.test_mode)
        if self._started:
            await self.store.dispose()
        self._started = False

    @property
    def components(self) -> RuntimeComponents:
        if self.runner is None:
            raise RuntimeError("Turn runtime has not been started")
        return RuntimeComponents(self.store, self.events, self.logs, self.runner, self.test_mode, self.capabilities)

    async def _restore_settings(self) -> None:
        """Restore durable preferences before constructing the runner."""
        casts = {
            "max_retries": int,
            "retry_backoff_ms": int,
            "delay_between_jobs_ms": int,
            "timeout_seconds": float,
            "stall_timeout_seconds": float,
            "retry_choked_models": lambda v: str(v).lower() in ("1", "true", "yes"),
            "log_max_records": int,
        }
        targets = {
            "max_retries": "max_retries",
            "retry_backoff_ms": "retry_backoff_ms",
            "delay_between_jobs_ms": "delay_between_jobs_ms",
            "timeout_seconds": "default_run_timeout_seconds",
            "stall_timeout_seconds": "stall_timeout_seconds",
            "retry_choked_models": "retry_choked_models",
            "log_max_records": "log_max_records",
        }
        for key, cast in casts.items():
            raw = await self.store.get_setting(key)
            if raw is not None:
                setattr(self.settings, targets[key], cast(raw))
        stored = {
            "default_harness": "default_executor",
            "default_model": "codex_model",
            "reasoning": "default_reasoning",
        }
        for key, target in stored.items():
            value = await self.store.get_setting(key)
            if value:
                setattr(self.settings, target, value)
        raw_defaults = await self.store.get_setting("agent_defaults")
        if raw_defaults:
            parsed = json.loads(raw_defaults)
            if not isinstance(parsed, dict):
                raise ValueError("stored agent_defaults must be an object")
            self.settings.agent_defaults = parsed
