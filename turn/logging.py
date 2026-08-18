"""Best-effort structured project event history.

The log format is deliberately close to common JSONL event envelopes: every
line is independently parseable, files are append-only, and rotation is based
on records rather than bytes.  The writer is intentionally small and owns the
only stitching policy used by the HTTP and CLI readers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
import uuid
from typing import Any, Iterator


class EventLog:
    """One process-owned writer and stitched reader for project JSONL logs.

    ``data_dir`` is the workspace runtime directory (``.turn`` at the
    development root). Project records are bound to the project's own
    ``.turn/logs`` directory by the server-owned store. Unbound records use
    the workspace directory for genuinely workspace-scoped events.
    """

    def __init__(self, data_dir: str | Path, max_records: int = 1000):
        self.workspace_directory = Path(data_dir).expanduser().resolve() / "logs"
        self._max_records = max(1, int(max_records))
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._project_roots: dict[str, Path] = {}

    @property
    def directory(self) -> Path:
        """Backward-compatible alias for the workspace log directory."""
        return self.workspace_directory

    def bind_project(self, project_id: uuid.UUID | str, project_root: str | Path) -> None:
        """Bind a project id to its project-local ``.turn`` directory."""
        self._project_roots[str(project_id)] = Path(project_root).expanduser().resolve()

    def unbind_project(self, project_id: uuid.UUID | str) -> None:
        self._project_roots.pop(str(project_id), None)

    @property
    def max_records(self) -> int:
        return self._max_records

    def set_max_records(self, value: int) -> None:
        self._max_records = max(1, int(value))

    @staticmethod
    def _project_key(project_id: uuid.UUID | str | None) -> str:
        return str(project_id) if project_id is not None else "workspace"

    def _directory(self, project_id: uuid.UUID | str | None) -> Path:
        if project_id is None:
            return self.workspace_directory
        project_root = self._project_roots.get(str(project_id))
        if project_root is None:
            return self.workspace_directory
        return project_root / ".turn" / "logs"

    def _files(self, project_id: uuid.UUID | str | None) -> list[Path]:
        key = self._project_key(project_id)
        return sorted(self._directory(project_id).glob(f"project-{key}-*.jsonl"))

    def _next_path(self, project_id: uuid.UUID | str | None) -> Path:
        key = self._project_key(project_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self._directory(project_id) / f"project-{key}-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"

    def _append(self, record: dict[str, Any]) -> None:
        """Append one line; all filesystem failures are intentionally ignored."""
        try:
            project_id = record.get("project_id")
            with self._lock:
                directory = self._directory(project_id)
                directory.mkdir(parents=True, exist_ok=True)
                files = self._files(project_id)
                target = files[-1] if files else None
                count = 0
                if target is not None:
                    try:
                        with target.open("r", encoding="utf-8") as stream:
                            count = sum(1 for _ in stream)
                    except OSError:
                        count = 0
                if target is None or count >= self._max_records:
                    target = self._next_path(project_id)
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
                    stream.flush()
        except Exception:
            return

    def emit_sync(
        self,
        project_id: uuid.UUID | str | None,
        *,
        kind: str,
        message: str = "",
        status: str = "info",
        source: str = "server",
        action: str | None = None,
        data: Any = None,
        _notify: bool = True,
    ) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_id": str(uuid.uuid4()),
            "project_id": str(project_id) if project_id is not None else None,
            "source": source,
            "kind": kind,
            "status": status,
            "message": message,
        }
        if action is not None:
            record["action"] = action
        if data is not None:
            record["data"] = data
        self._append(record)
        if _notify:
            self._notify(record)
        return record

    async def emit(self, project_id: uuid.UUID | str | None, **kwargs: Any) -> dict[str, Any]:
        try:
            record = await asyncio.to_thread(self.emit_sync, project_id, _notify=False, **kwargs)
            self._notify(record)
            return record
        except BaseException:
            # Logging must never turn a successful orchestration action into a
            # failed one, including while a caller is being cancelled.
            return {}

    def _notify(self, record: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(record)
            except Exception:
                self._subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def read(
        self,
        project_id: uuid.UUID | str | None,
        *,
        search: str = "",
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        needle = search.casefold().strip()
        for path in self._files(project_id):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            value = json.loads(line)
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if not isinstance(value, dict):
                            continue
                        if needle and needle not in json.dumps(value, ensure_ascii=False, default=str).casefold():
                            continue
                        records.append(value)
            except (OSError, UnicodeDecodeError):
                continue
        return records[-max(1, limit):]

    def follow(
        self,
        project_id: uuid.UUID | str | None,
        *,
        search: str = "",
        poll_seconds: float = 0.25,
    ) -> Iterator[dict[str, Any]]:
        """Yield a stitched history and then newly appended records."""
        seen: set[str] = set()
        while True:
            for record in self.read(project_id, search=search, limit=100_000):
                event_id = str(record.get("event_id") or "")
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                yield record
            time.sleep(poll_seconds)
