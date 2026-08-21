"""Small process-supervision boundary shared by execution and cleanup.

The supervisor owns operational inventory only. Semantic Run/Node outcomes
remain in ``Store.accept_run_submission`` and are never inferred here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from turn.db.store import Store
from turn.domain.schemas import ProcessState, RunStatus


@dataclass(frozen=True)
class OwnedProcess:
    project_id: uuid.UUID
    node_id: uuid.UUID
    run_id: uuid.UUID | None
    pane_id: str | None
    provider: str | None
    process_state: ProcessState
    live: bool


class ProcessSupervisor:
    """Inventory and cleanup for Turn-owned provider terminals."""

    def __init__(self, store: Store, terminal) -> None:
        self.store = store
        self.terminal = terminal

    async def inventory(self, project_id: uuid.UUID | None = None) -> tuple[OwnedProcess, ...]:
        node_ids: set[uuid.UUID] = set()
        project_owned = getattr(self.terminal, "owned_node_ids_for_project", None)
        # Never seed a project-scoped query from the global transport
        # inventory. Synthetic control owners have no graph Node, so that
        # union can make another project's pane appear to belong here.
        if project_id is None:
            owned = getattr(self.terminal, "owned_node_ids", None)
            if owned is not None:
                node_ids.update(owned())

        # Persisted graph/process owners are part of the inventory even if a
        # transport has not rebuilt its local pane cache after a daemon
        # restart. Run.process_owner_id is the durable proof for synthetic
        # control owners when the transport has no project query surface.
        project_runs: dict[uuid.UUID, list] = {}
        for project in await self.store.list_projects():
            if project_id is not None and project.id != project_id:
                continue
            runs = await self.store.get_project_runs(project.id)
            project_runs[project.id] = runs
            for node in [project, *await self.store.descendants(project.id)]:
                # Graph ownership is the durable baseline. A transport may
                # not have rebuilt its pane cache yet, so inventory must still
                # represent every in-scope owner and let ``live`` distinguish
                # an active process from an already-exited one.
                node_ids.add(node.id)
            for run in runs:
                if run.process_owner_id is not None:
                    node_ids.add(run.process_owner_id)
            if project_id is not None and project_owned is not None:
                node_ids.update(project_owned(str(project.id)))
        records: list[OwnedProcess] = []
        for node_id in node_ids:
            node = await self.store.get_node(node_id)
            if node is None:
                # Control-plane review panes use a short-lived synthetic owner
                # UUID, so they have no graph Node. Herdr's project-filtered
                # inventory is the authority for including them here.
                if project_id is None:
                    continue
                runs = project_runs.get(project_id, [])
                pane_id = getattr(self.terminal, "pane_id", lambda _id: None)(node_id)
                matching = [
                    run
                    for run in runs
                    if run.process_owner_id == node_id
                    or (pane_id is not None and run.pane_id == pane_id)
                ]
                run = next((item for item in reversed(matching) if item.status is RunStatus.RUNNING), None)
                snapshot = self.terminal.snapshot(node_id)
                records.append(
                    OwnedProcess(
                        project_id=project_id,
                        node_id=node_id,
                        run_id=run.id if run else None,
                        pane_id=pane_id,
                        provider=run.provider if run else "control",
                        process_state=run.process_state if run else ProcessState.RUNNING,
                        live=bool(snapshot.get("active")),
                    )
                )
                continue
            if project_id is not None and node.project_id != project_id:
                continue
            runs = await self.store.get_runs(node_id)
            run = next((item for item in reversed(runs) if item.status is RunStatus.RUNNING), None)
            pane_id = getattr(self.terminal, "pane_id", lambda _id: None)(node_id)
            snapshot = self.terminal.snapshot(node_id)
            live = bool(snapshot.get("active"))
            foreground = getattr(self.terminal, "foreground_process_names", None)
            if foreground is not None:
                try:
                    live = live or bool(await foreground(node_id))
                except Exception:
                    # Unknown is operationally different from dead; preserve
                    # the durable Run and let restart reconciliation retry.
                    live = True if run is not None else live
            records.append(
                OwnedProcess(
                    project_id=node.project_id,
                    node_id=node_id,
                    run_id=run.id if run else None,
                    pane_id=pane_id,
                    provider=(run.provider if run else node.agent.harness.value if node.agent else node.executor),
                    process_state=(run.process_state if run else ProcessState.RUNNING if live else ProcessState.EXITED),
                    live=live,
                )
            )
        return tuple(records)

    async def close_all(self, project_id: uuid.UUID) -> int:
        """Close every Turn-owned pane found by the same inventory path."""
        records = await self.inventory(project_id)
        graph_nodes, _, _ = await self.store.get_workgraph(project_id)
        order = {node.id: index for index, node in enumerate(graph_nodes)}
        records = tuple(
            sorted(records, key=lambda item: (order.get(item.node_id, len(order)), str(item.node_id)))
        )
        closed = 0
        for record in records:
            if await self.terminal.close_persistent_session(record.node_id):
                closed += 1
        return closed
