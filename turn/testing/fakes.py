"""Small deterministic provider fakes for offline runtime composition."""
from __future__ import annotations

from turn.workers.herdr import (
    HerdrPane,
    HerdrResourceNotFound,
    HerdrWorkspace,
    HerdrWorkspaceCreation,
)


class FakeHerdrAdapter:
    """In-memory Herdr port; no CLI, daemon, or external process is used."""

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

    async def wait_for_output(self, pane_id: str, *, regex: str = ".", source: str = "recent-unwrapped", lines: int | None = None) -> bool:
        await self.get_pane(pane_id)
        self.wait_outputs.append((pane_id, regex, source, lines))
        return True

    async def read_pane(self, pane_id: str, *, source: str = "recent", lines: int = 2000) -> str:
        await self.get_pane(pane_id)
        self.read_requests.append((pane_id, source, lines))
        return ""

    def terminal_control_command(self, pane_id: str, *, cols: int = 80, rows: int = 24):
        raise AssertionError("deterministic Herdr integration does not open a control process")
