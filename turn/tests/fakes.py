"""Deterministic ports used by architecture and API integration tests."""
from __future__ import annotations

import asyncio
import uuid

from turn.domain.schemas import Node
from turn.domain.schemas import WorkerResult
from turn.workers.herdr import (
    HerdrPane,
    HerdrWorkspace,
    HerdrWorkspaceCreation,
    HerdrResourceNotFound,
)
from turn.workers.terminal import TerminalResult


class FakeHerdrAdapter:
    """In-memory Herdr port; no CLI, daemon, or filesystem process is used."""

    def __init__(self):
        self.workspaces: dict[str, HerdrWorkspace] = {}
        self.panes: dict[str, tuple[str, HerdrPane]] = {}
        self.created: list[str] = []
        self.closed: list[str] = []
        self._workspace_number = 0
        self._pane_number = 0
        self.sent_keys: list[tuple[str, tuple[str, ...]]] = []
        self.run_commands: list[tuple[str, str]] = []
        self.wait_outputs: list[tuple[str, str, str, int | None]] = []
        self.read_requests: list[tuple[str, str, int]] = []

    @property
    def available(self) -> bool:
        return True

    async def list_workspaces(self) -> tuple[HerdrWorkspace, ...]:
        return tuple(self.workspaces.values())

    async def create_workspace(self, *, cwd: str, label: str, focus: bool = False):
        self._workspace_number += 1
        self._pane_number += 1
        workspace_id = f"fake-w{self._workspace_number}"
        pane_id = f"fake-p{self._pane_number}"
        self.workspaces[workspace_id] = HerdrWorkspace(workspace_id, label, 1, 1)
        self.panes[pane_id] = (workspace_id, HerdrPane(pane_id))
        self.created.append(workspace_id)
        return HerdrWorkspaceCreation(workspace_id, pane_id)

    async def get_workspace(self, workspace_id: str) -> HerdrWorkspace:
        try:
            return self.workspaces[workspace_id]
        except KeyError as error:
            raise HerdrResourceNotFound("workspace", workspace_id) from error

    async def close_workspace(self, workspace_id: str) -> bool:
        if workspace_id not in self.workspaces:
            return False
        self.workspaces.pop(workspace_id)
        for pane_id, (owner, _) in list(self.panes.items()):
            if owner == workspace_id:
                self.panes.pop(pane_id)
        self.closed.append(workspace_id)
        return True

    async def get_pane(self, pane_id: str) -> HerdrPane:
        try:
            return self.panes[pane_id][1]
        except KeyError as error:
            raise HerdrResourceNotFound("pane", pane_id) from error

    async def create_tab(self, *, workspace_id: str, cwd: str, label: str, focus: bool = False):
        await self.get_workspace(workspace_id)
        self._pane_number += 1
        pane_id = f"fake-p{self._pane_number}"
        pane = HerdrPane(pane_id)
        self.panes[pane_id] = (workspace_id, pane)
        return pane

    async def close_pane(self, pane_id: str) -> bool:
        return self.panes.pop(pane_id, None) is not None

    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> bool:
        await self.get_pane(pane_id)
        self.sent_keys.append((pane_id, keys))
        return True

    async def run_command(self, pane_id: str, command: str) -> bool:
        await self.get_pane(pane_id)
        self.run_commands.append((pane_id, command))
        return True

    async def wait_for_output(
        self,
        pane_id: str,
        *,
        regex: str = ".",
        source: str = "recent-unwrapped",
        lines: int | None = None,
    ) -> bool:
        await self.get_pane(pane_id)
        self.wait_outputs.append((pane_id, regex, source, lines))
        return True

    async def read_pane(self, pane_id: str, *, source: str = "recent", lines: int = 2000) -> str:
        await self.get_pane(pane_id)
        self.read_requests.append((pane_id, source, lines))
        return ""

    def terminal_control_command(self, pane_id: str, *, cols: int = 80, rows: int = 24):
        raise AssertionError("deterministic Herdr integration does not open a control process")


class FakeTerminalTransport:
    """Deterministic terminal port for runner shell lifecycle tests."""

    supports_inject = False
    backend_name = "fake"

    def __init__(self):
        self._state: dict[uuid.UUID, dict[str, object]] = {}
        self._stops: dict[uuid.UUID, asyncio.Event] = {}
        self.closed_nodes: set[uuid.UUID] = set()
        self.close_requests: list[uuid.UUID] = []

    @property
    def available(self) -> bool:
        return True

    def _node(self, node_id: uuid.UUID) -> dict[str, object]:
        return self._state.setdefault(
            node_id,
            {"active": False, "output": "", "persistent": False, "pane": f"fake-pane-{node_id}"},
        )

    async def run(self, node_id: uuid.UUID, command: list[str], **kwargs) -> TerminalResult:
        state = self._node(node_id)
        state["active"] = True
        state["persistent"] = True
        state["output"] = "fake shell\n"
        stop = self._stops.get(node_id)
        if stop is None or stop.is_set():
            stop = asyncio.Event()
            self._stops[node_id] = stop
        await stop.wait()
        state["active"] = False
        return TerminalResult(0, str(state["output"]).encode())

    async def ensure_session(self, node_id: uuid.UUID, **kwargs) -> TerminalResult:
        """Attach to the deterministic persistent pane without launching work."""
        return await self.run(node_id, ["persistent-pane"], **kwargs)

    async def write(self, node_id: uuid.UUID, data: str | bytes) -> bool:
        state = self._node(node_id)
        if not state["active"]:
            return False
        state["output"] = str(state["output"]) + (data.decode() if isinstance(data, bytes) else data)
        return True

    async def scroll(self, node_id: uuid.UUID, direction: str, amount: int = 1) -> bool:
        if direction not in {"up", "down"}:
            return False
        return await self.write(
            node_id,
            (b"\x1b[5~" if direction == "up" else b"\x1b[6~") * max(1, amount),
        )

    async def resize(self, node_id: uuid.UUID, cols: int, rows: int) -> bool:
        return bool(self._node(node_id)["active"])

    async def stop(self, node_id: uuid.UUID) -> bool:
        state = self._node(node_id)
        was_active = bool(state["active"])
        self._stops.setdefault(node_id, asyncio.Event()).set()
        state["active"] = False
        return was_active

    async def detach(self, node_id: uuid.UUID) -> bool:
        return await self.stop(node_id)

    def release(self, node_id: uuid.UUID) -> bool:
        return False

    def snapshot(self, node_id: uuid.UUID) -> dict:
        state = self._node(node_id)
        return {"active": state["active"], "output": state["output"]}

    async def ensure_persistent_shell(self, node_id: uuid.UUID, **kwargs) -> bool:
        self._node(node_id)["persistent"] = True
        return True

    async def has_persistent_session(self, node_id: uuid.UUID) -> bool:
        return bool(self._node(node_id)["persistent"])

    def pane_id(self, node_id: uuid.UUID) -> str:
        return str(self._node(node_id)["pane"])

    async def close_persistent_session(self, node_id: uuid.UUID) -> bool:
        self.close_requests.append(node_id)
        state = self._node(node_id)
        closed = bool(state["persistent"])
        await self.stop(node_id)
        state["persistent"] = False
        if closed:
            self.closed_nodes.add(node_id)
        return closed

    async def close_project_workspace(self, project_key: str) -> bool:
        return True


class DeterministicExecutionAdapter:
    """Execution port that records calls and invokes only the supplied worker."""

    def __init__(self):
        self.workers: list[str] = []

    async def run(self, worker, ctx, *, timeout: float) -> WorkerResult:
        self.workers.append(worker.name)
        return await worker.execute(ctx)
