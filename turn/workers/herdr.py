"""Typed Herdr port and CLI adapter.

Turn depends on this small port for workspace and pane management. The
concrete adapter is the only place that knows Herdr's CLI command shape and
JSON response envelope; the terminal transport remains responsible for Turn's
durable node-to-pane mapping and short-lived control streams.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Protocol


class HerdrAdapterError(RuntimeError):
    """A Herdr command failed or returned an invalid response."""


class HerdrResourceNotFound(HerdrAdapterError):
    """A requested Herdr workspace or pane no longer exists."""

    def __init__(self, resource: str, resource_id: str):
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource}_not_found: {resource_id}")


@dataclass(frozen=True)
class HerdrWorkspace:
    workspace_id: str
    label: str | None = None
    pane_count: int | None = None
    tab_count: int | None = None


@dataclass(frozen=True)
class HerdrWorkspaceCreation:
    workspace_id: str
    root_pane_id: str


@dataclass(frozen=True)
class HerdrPane:
    pane_id: str


class HerdrAdapter(Protocol):
    """The Herdr operations Turn requires for project terminal management."""

    @property
    def available(self) -> bool: ...

    async def list_workspaces(self) -> tuple[HerdrWorkspace, ...]: ...

    async def create_workspace(
        self, *, cwd: str, label: str, focus: bool = False
    ) -> HerdrWorkspaceCreation: ...

    async def get_workspace(self, workspace_id: str) -> HerdrWorkspace: ...

    async def close_workspace(self, workspace_id: str) -> bool: ...

    async def get_pane(self, pane_id: str) -> HerdrPane: ...

    async def create_tab(
        self, *, workspace_id: str, cwd: str, label: str, focus: bool = False
    ) -> HerdrPane: ...

    async def close_pane(self, pane_id: str) -> bool: ...

    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> bool: ...

    async def run_command(self, pane_id: str, command: str) -> bool: ...

    async def wait_for_output(
        self,
        pane_id: str,
        *,
        regex: str = ".",
        source: str = "recent-unwrapped",
        lines: int | None = None,
    ) -> bool: ...

    async def read_pane(
        self,
        pane_id: str,
        *,
        source: str = "recent",
        lines: int = 2000,
    ) -> str: ...

    def terminal_control_command(
        self, pane_id: str, *, cols: int = 80, rows: int = 24
    ) -> list[str]: ...


class HerdrCliAdapter:
    """Call the documented Herdr CLI bridge from an external Turn process."""

    def __init__(self, herdr_binary: str | None = None, session: str | None = None):
        self._herdr = herdr_binary or shutil.which("herdr")
        self._session = session if session is not None else os.getenv("HERDR_SESSION")

    @property
    def available(self) -> bool:
        return bool(self._herdr)

    def command(self, *args: str) -> list[str]:
        if not self._herdr:
            raise HerdrAdapterError("Herdr is required for project terminal management")
        command = [self._herdr]
        if self._session:
            command.extend(("--session", self._session))
        command.extend(args)
        return command

    async def _run(self, *args: str) -> dict[str, object]:
        if not self.available:
            raise HerdrAdapterError("Herdr is required for project terminal management")
        process = await asyncio.create_subprocess_exec(
            *self.command(*args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            self._raise_command_error(detail, args)
        try:
            value = json.loads(stdout.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HerdrAdapterError(
                f"herdr returned invalid JSON for {' '.join(args)}"
            ) from error
        if not isinstance(value, dict):
            raise HerdrAdapterError(
                f"herdr returned an invalid response for {' '.join(args)}"
            )
        return value

    async def _run_without_result(self, *args: str) -> None:
        """Run a successful side-effect command whose CLI response is empty."""
        if not self.available:
            raise HerdrAdapterError("Herdr is required for terminal commands")
        process = await asyncio.create_subprocess_exec(
            *self.command(*args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            self._raise_command_error(detail, args)

    @staticmethod
    def _raise_command_error(detail: str, args: tuple[str, ...]) -> None:
        for resource in ("workspace", "pane"):
            if f"{resource}_not_found" in detail:
                resource_id = next(
                    (argument for argument in args if ":" in argument),
                    args[-1] if args else "unknown",
                )
                raise HerdrResourceNotFound(resource, resource_id)
        raise HerdrAdapterError(detail or f"herdr {' '.join(args)} failed")

    @staticmethod
    def _result(response: dict[str, object]) -> dict[str, object]:
        value = response.get("result", response)
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _required_id(value: object, *names: str) -> str:
        if isinstance(value, dict):
            for name in names:
                candidate = value.get(name)
                if isinstance(candidate, str) and candidate:
                    return candidate
        raise HerdrAdapterError("Herdr response did not contain a required identifier")

    async def list_workspaces(self) -> tuple[HerdrWorkspace, ...]:
        result = self._result(await self._run("workspace", "list"))
        raw_workspaces = result.get("workspaces")
        if not isinstance(raw_workspaces, list):
            raise HerdrAdapterError("Herdr workspace list response was invalid")
        workspaces: list[HerdrWorkspace] = []
        for raw in raw_workspaces:
            if not isinstance(raw, dict):
                raise HerdrAdapterError("Herdr workspace list contained an invalid workspace")
            workspace_id = self._required_id(raw, "workspace_id", "id")
            label = raw.get("label")
            pane_count = raw.get("pane_count")
            tab_count = raw.get("tab_count")
            workspaces.append(
                HerdrWorkspace(
                    workspace_id,
                    label if isinstance(label, str) else None,
                    pane_count if isinstance(pane_count, int) else None,
                    tab_count if isinstance(tab_count, int) else None,
                )
            )
        return tuple(workspaces)

    async def create_workspace(
        self, *, cwd: str, label: str, focus: bool = False
    ) -> HerdrWorkspaceCreation:
        args = ["workspace", "create", "--cwd", cwd, "--label", label]
        if not focus:
            args.append("--no-focus")
        result = self._result(await self._run(*args))
        workspace = result.get("workspace")
        root_pane = result.get("root_pane")
        return HerdrWorkspaceCreation(
            self._required_id(workspace, "workspace_id", "id"),
            self._required_id(root_pane, "pane_id", "id"),
        )

    async def get_workspace(self, workspace_id: str) -> HerdrWorkspace:
        result = self._result(await self._run("workspace", "get", workspace_id))
        raw = result.get("workspace")
        if not isinstance(raw, dict):
            raise HerdrAdapterError("Herdr workspace get response was invalid")
        label = raw.get("label")
        pane_count = raw.get("pane_count")
        tab_count = raw.get("tab_count")
        return HerdrWorkspace(
            self._required_id(raw, "workspace_id", "id"),
            label if isinstance(label, str) else None,
            pane_count if isinstance(pane_count, int) else None,
            tab_count if isinstance(tab_count, int) else None,
        )

    async def close_workspace(self, workspace_id: str) -> bool:
        try:
            await self._run("workspace", "close", workspace_id)
        except HerdrResourceNotFound:
            return False
        return True

    async def get_pane(self, pane_id: str) -> HerdrPane:
        result = self._result(await self._run("pane", "get", pane_id))
        raw = result.get("pane")
        return HerdrPane(self._required_id(raw, "pane_id", "id"))

    async def create_tab(
        self, *, workspace_id: str, cwd: str, label: str, focus: bool = False
    ) -> HerdrPane:
        args = [
            "tab",
            "create",
            "--workspace",
            workspace_id,
            "--cwd",
            cwd,
            "--label",
            label,
        ]
        if not focus:
            args.append("--no-focus")
        result = self._result(await self._run(*args))
        return HerdrPane(self._required_id(result.get("root_pane"), "pane_id", "id"))

    async def close_pane(self, pane_id: str) -> bool:
        try:
            await self._run("pane", "close", pane_id)
        except HerdrResourceNotFound:
            return False
        return True

    async def send_keys(self, pane_id: str, keys: tuple[str, ...]) -> bool:
        """Send logical keys through Herdr's pane surface.

        This is deliberately separate from terminal.input. Scroll gestures are
        pane operations, not bytes appended to the provider's current prompt.
        Keeping them on the Herdr adapter also means the browser never needs a
        second local transcript or a guessed PTY escape sequence.
        """
        if not keys:
            return True
        try:
            await self._run_without_result("pane", "send-keys", pane_id, *keys)
        except HerdrResourceNotFound:
            return False
        return True

    async def run_command(self, pane_id: str, command: str) -> bool:
        """Submit one complete shell command through Herdr's atomic pane API."""
        if not command:
            return True
        try:
            await self._run_without_result("pane", "run", pane_id, command)
        except HerdrResourceNotFound:
            return False
        return True

    async def wait_for_output(
        self,
        pane_id: str,
        *,
        regex: str = ".",
        source: str = "recent-unwrapped",
        lines: int | None = None,
    ) -> bool:
        """Wait for Herdr to observe output from a live pane.

        Herdr owns the pane lifecycle and provides this as an event-driven
        readiness primitive. In particular, a newly-created shell is not safe
        to interrupt or inject into merely because ``tab create`` returned its
        pane id; the first terminal output is the shell's actual ready signal.
        """
        args = [
            "pane",
            "wait-output",
            pane_id,
            "--regex",
            regex,
            "--source",
            source,
        ]
        if lines is not None:
            args.extend(("--lines", str(max(1, lines))))
        await self._run(*args)
        return True

    async def read_pane(
        self,
        pane_id: str,
        *,
        source: str = "recent",
        lines: int = 2000,
    ) -> str:
        if not self.available:
            raise HerdrAdapterError("Herdr is required for pane reads")
        process = await asyncio.create_subprocess_exec(
            *self.command(
                "pane",
                "read",
                pane_id,
                "--source",
                source,
                "--lines",
                str(max(1, lines)),
                "--format",
                "ansi",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            self._raise_command_error(detail, ("pane", "read", pane_id))
        return stdout.decode(errors="replace")

    async def foreground_process_names(self, pane_id: str) -> tuple[str, ...]:
        result = self._result(await self._run("pane", "process-info", "--pane", pane_id))
        info = result.get("process_info")
        processes = info.get("foreground_processes") if isinstance(info, dict) else None
        if not isinstance(processes, list):
            return ()
        names: list[str] = []
        for process in processes:
            if isinstance(process, dict):
                # Node-based CLIs such as Pi report `name: node` while their
                # argv0 remains the provider command. Preserve both so
                # foreground readiness can match the configured harness.
                for field in ("name", "argv0"):
                    name = process.get(field)
                    if isinstance(name, str) and name:
                        names.append(name.rsplit("/", 1)[-1])
        return tuple(dict.fromkeys(names))

    def terminal_control_command(
        self, pane_id: str, *, cols: int = 80, rows: int = 24
    ) -> list[str]:
        return self.command(
            "terminal",
            "session",
            "control",
            pane_id,
            "--takeover",
            "--cols",
            str(max(20, cols)),
            "--rows",
            str(max(4, rows)),
        )
