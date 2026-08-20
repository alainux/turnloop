"""Headless application façade.

This is the public CLI/library boundary. UI servers are clients of the same
Store + Runner kernel; no browser or FastAPI concept is required to create,
inspect, or execute a workgraph.
"""
from __future__ import annotations

import asyncio
import uuid

from turn.config import Settings
from turn.domain.schemas import AgentConfig, RunPolicy
from turn.graph.logic import GraphWalker
from turn.workers.filesystem import init_project_directory
from turn.runtime import TurnRuntime


class TurnCore:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        test_mode: bool = False,
        herdr_adapter=None,
        terminal_transport=None,
    ):
        self.settings = settings or Settings()
        self.runtime = TurnRuntime(
            self.settings,
            herdr_adapter=herdr_adapter,
            terminal_transport=terminal_transport,
            test_mode=test_mode,
        )
        self.store = self.runtime.store
        self.events = self.runtime.events

    @property
    def runner(self):
        return self.runtime.components.runner

    async def __aenter__(self):
        await self.runtime.start()
        return self

    async def __aexit__(self, *_):
        await self.runtime.stop()

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
        repo = init_project_directory(
            project_id,
            working_dir=working_dir,
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
        # A worker finishing is not itself a scheduler pass. Keep one bounded
        # probe after the frontier becomes idle so the runner can propagate a
        # newly-unblocked node and finalize a focused root container. Material
        # organizations still return after that probe when their manager needs
        # a decision; this is not an unbounded polling loop.
        settlement_probe_pending = True
        for _ in range(max_rounds):
            await self.runner.schedule_once(project_id)
            await self.runner.wait_for_idle(project_id)
            if self.runner.active_node_ids(project_id):
                settlement_probe_pending = True
                continue
            nodes, edges, _ = await self.store.get_workgraph(project_id)
            evaluation = GraphWalker(nodes, edges).evaluate()
            active = [
                node
                for node in nodes
                if node.status.value in {"PENDING", "RUNNABLE", "RUNNING"}
                or node.id in evaluation.runnable
            ]
            if not active:
                if settlement_probe_pending:
                    settlement_probe_pending = False
                    continue
                return nodes
            settlement_probe_pending = True
            await asyncio.sleep(self.settings.runner_tick_seconds)
        raise TimeoutError(f"workgraph did not settle after {max_rounds} scheduler rounds")
