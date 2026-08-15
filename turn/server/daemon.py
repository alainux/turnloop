"""Single-machine daemon boundary for the Turn server."""
from __future__ import annotations

import fcntl
from pathlib import Path
from types import TracebackType
from typing import Type

import uvicorn

from turn.config import Settings


class ServerAlreadyRunning(RuntimeError):
    """Raised when another Turn daemon owns the machine-wide lease."""


class ServerLease:
    """Process lease preventing two Turn servers sharing one data directory."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir).expanduser().resolve() / "server.lock"
        self._handle = None

    def __enter__(self) -> "ServerLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self._handle.close()
            self._handle = None
            raise ServerAlreadyRunning(
                f"Turn server already owns {self.path.parent}"
            ) from error
        return self

    def __exit__(
        self,
        exc_type: Type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


class TurnDaemon:
    """Start the global per-machine server with an explicit lease."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, *, host: str, port: int) -> None:
        with ServerLease(self.settings.data_dir):
            uvicorn.run(
                "turn.server.app:app",
                host=host,
                port=port,
                log_level="info",
            )
