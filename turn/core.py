"""Headless application façade.

This is the public CLI/library boundary. UI servers are clients of the same
Store + Runner kernel; no browser or FastAPI concept is required to create,
inspect, or execute a workgraph.
"""
from __future__ import annotations

import asyncio
import uuid

from turn.config import Settings, settings as default_settings
from turn.db.store import Store
from turn.domain.schemas import AgentConfig, RunPolicy
from turn.runner.events import EventBus
from turn.runner.prefect_adapter import get_execution_adapter
from turn.runner.runner import Runner
from turn.workers.registry import build_registry
from turn.workers.worktree import init_project_repo


class TurnCore:
    def __init__(self, settings: Settings = default_settings):
        self.settings = settings
        self.store = Store(settings.database_url)
        self.events = EventBus()
        self.runner = Runner(
            self.store,
            build_registry(settings),
            self.events,
            settings,
            get_execution_adapter(settings),
        )

    async def __aenter__(self):
        await self.store.init()
        return self

    async def __aexit__(self, *_):
        await self.runner.stop()
        await self.store.dispose()

    async def create_project(
        self,
        prompt: str,
        *,
        name: str | None = None,
        working_dir: str | None = None,
        open_existing: bool = False,
        agent: AgentConfig | None = None,
        run_policy: RunPolicy | None = None,
    ):
        if agent is not None:
            from turn.workers.harnesses import validate_agent_capabilities
            validate_agent_capabilities(agent)
        project_id = uuid.uuid4()
        repo = init_project_repo(
            project_id,
            working_dir=working_dir,
            open_existing=open_existing,
            projects_dir=self.settings.projects_dir,
        )
        return await self.store.create_project(
            prompt,
            name=name,
            repo_path=repo,
            id=project_id,
            agent=agent,
            run_policy=run_policy,
        )

    async def graph(self, project_id: uuid.UUID):
        return await self.store.get_workgraph(project_id)

    async def run_until_settled(self, project_id: uuid.UUID, max_rounds: int = 10_000):
        """Drive one project until it is complete, failed, or needs a human."""
        # A direct headless run is an explicit execution request, even if the
        # project was authored in step mode.
        await self.runner.set_mode(project_id, True)
        for _ in range(max_rounds):
            await self.runner._schedule_project(project_id)
            if self.runner._running:
                await asyncio.gather(*list(self.runner._running.values()), return_exceptions=True)
                continue
            nodes, _, _ = await self.store.get_workgraph(project_id)
            active = [n for n in nodes if n.status.value in {"PENDING", "RUNNABLE", "RUNNING"}]
            if not active:
                return nodes
            await asyncio.sleep(self.settings.runner_tick_seconds)
        raise TimeoutError(f"workgraph did not settle after {max_rounds} scheduler rounds")
