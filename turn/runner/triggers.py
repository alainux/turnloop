"""Workspace-wide event dispatch and trigger activation.

Triggers are persisted with their project, while events are deliberately
workspace-scoped. The dispatcher is the only component that knows how an
event fans out across projects; the Store remains responsible for durable
graph mutation.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid
from typing import Any

from turn.domain.schemas import EventSource, Trigger, TriggerContext, TriggerKind
from turn.logging import EventLog
from turn.runner.events import EventBus


class EventInbox:
    """Small append-only cross-process inbox used by the CLI event helper."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir).expanduser().resolve() / "trigger-events.jsonl"
        self.cursor_path = self.path.with_suffix(".cursor")

    def append(self, *, name: str, data: dict[str, Any], project_id: str | None = None, node_id: str | None = None) -> dict[str, Any]:
        record = {
            "event_id": str(uuid.uuid4()),
            "event_name": name,
            "data": data,
            "source": EventSource.CLI.value,
            "project_id": project_id,
            "node_id": node_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        return record

    def start_offset(self) -> int:
        try:
            return max(0, int(self.cursor_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # A first daemon start must consume events queued by a human or
            # agent before the runner came online. Once a cursor exists, it
            # remains the durable handoff point across daemon restarts.
            return 0

    def read_from(self, offset: int) -> tuple[list[dict[str, Any]], int]:
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                stream.seek(offset)
                records: list[dict[str, Any]] = []
                for line in stream:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
                return records, stream.tell()
        except OSError:
            return [], offset

    def save_offset(self, offset: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(offset), encoding="utf-8")


def _same_minute(left: datetime | None, right: datetime) -> bool:
    return bool(left and left.astimezone(timezone.utc).replace(second=0, microsecond=0) == right.replace(second=0, microsecond=0))


def _field_matches(value: int, expression: str, minimum: int, maximum: int) -> bool:
    for part in expression.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                return False
            return step > 0 and (value - minimum) % step == 0
        if "-" in part:
            try:
                start, end = (int(piece) for piece in part.split("-", 1))
            except ValueError:
                return False
            if start <= value <= end:
                return True
            continue
        try:
            candidate = int(part)
            if candidate == value or (
                minimum == 0 and maximum == 7 and candidate == 7 and value == 0
            ):
                return True
        except ValueError:
            return False
    return False


def schedule_is_due(expression: str, now: datetime, last_fired_at: datetime | None) -> bool:
    """Evaluate a classic five-field UTC cron expression."""
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("schedule must be a five-field cron expression")
    if last_fired_at is not None and _same_minute(last_fired_at, now):
        return False
    return (
        _field_matches(now.minute, fields[0], 0, 59)
        and _field_matches(now.hour, fields[1], 0, 23)
        and _field_matches(now.day, fields[2], 1, 31)
        and _field_matches(now.month, fields[3], 1, 12)
        and _field_matches((now.weekday() + 1) % 7, fields[4], 0, 7)
    )


class TriggerDispatcher:
    """Match exact event names and activate every matching target globally."""

    def __init__(self, store, events: EventBus, logs: EventLog, data_dir: str | Path):
        self.store = store
        self.events = events
        self.logs = logs
        self.inbox = EventInbox(data_dir)
        self._wake = None
        self._task: asyncio.Task | None = None
        self._offset = 0
        self._stop = False

    def set_wake(self, wake) -> None:
        self._wake = wake

    async def start(self) -> None:
        self._stop = False
        self._offset = self.inbox.start_offset()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while not self._stop:
            await self.poll_inbox()
            await self.tick_schedules()
            await asyncio.sleep(0.1)

    async def poll_inbox(self) -> None:
        records, offset = self.inbox.read_from(self._offset)
        for record in records:
            try:
                source = EventSource(str(record.get("source", EventSource.CLI.value)))
                occurred_at = datetime.fromisoformat(str(record["occurred_at"]))
                await self.emit(
                    str(record["event_name"]),
                    data=dict(record.get("data") or {}),
                    source=source,
                    project_id=uuid.UUID(str(record["project_id"])) if record.get("project_id") else None,
                    node_id=uuid.UUID(str(record["node_id"])) if record.get("node_id") else None,
                    event_id=uuid.UUID(str(record["event_id"])),
                    occurred_at=occurred_at,
                )
            except (KeyError, TypeError, ValueError):
                await self.logs.emit(None, kind="trigger.activity", action="event.rejected", status="error", source="trigger", message="invalid CLI event envelope", data=record)
        self._offset = offset
        self.inbox.save_offset(offset)

    async def tick_schedules(self, now: datetime | None = None) -> None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        for trigger in await self.store.list_triggers():
            if not trigger.enabled or trigger.kind is not TriggerKind.SCHEDULE or not trigger.schedule:
                continue
            try:
                due = schedule_is_due(trigger.schedule, current, trigger.last_fired_at)
            except ValueError as error:
                await self.logs.emit(trigger.project_id, kind="trigger.activity", action="schedule.rejected", status="error", source="schedule", message=str(error), data={"trigger_id": str(trigger.id), "schedule": trigger.schedule})
                continue
            if not due:
                continue
            await self.store.mark_trigger_fired(trigger.id, current)
            await self.emit(
                self._schedule_event_name(trigger),
                data={"scheduled_at": current.isoformat(), "trigger_id": str(trigger.id)},
                source=EventSource.SCHEDULE,
                project_id=trigger.project_id,
            )

    @staticmethod
    def _schedule_event_name(trigger: Trigger) -> str:
        return trigger.event_name or f"schedule.{trigger.id}"

    @staticmethod
    def _matches(trigger: Trigger, event_name: str) -> bool:
        if not trigger.enabled:
            return False
        if trigger.kind is TriggerKind.SCHEDULE:
            return event_name == TriggerDispatcher._schedule_event_name(trigger)
        return trigger.event_name == event_name

    async def emit(
        self,
        event_name: str,
        *,
        data: dict[str, Any] | None = None,
        source: EventSource = EventSource.CLI,
        project_id: uuid.UUID | None = None,
        node_id: uuid.UUID | None = None,
        event_id: uuid.UUID | None = None,
        occurred_at: datetime | None = None,
    ) -> dict[str, Any]:
        if isinstance(source, str):
            source = EventSource(source)
        if not event_name or not event_name.strip():
            raise ValueError("event name cannot be empty")
        event_id = event_id or uuid.uuid4()
        occurred_at = (occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        payload = dict(data or {})
        envelope = {
            "event_id": str(event_id),
            "event_name": event_name,
            "data": payload,
            "source": source.value,
            "project_id": str(project_id) if project_id else None,
            "node_id": str(node_id) if node_id else None,
            "occurred_at": occurred_at.isoformat(),
        }
        await self.logs.emit(project_id, kind="trigger.event", action="event.emitted", source=source.value, message=f"event {event_name} emitted", data=envelope)
        matched = 0
        for trigger in await self.store.list_triggers():
            if not self._matches(trigger, event_name):
                continue
            matched += 1
            activation_data = {**trigger.data, **payload}
            context = TriggerContext(
                trigger_id=trigger.id,
                event_id=event_id,
                event_name=event_name,
                data=activation_data,
                source=source,
                source_project_id=project_id,
                source_node_id=node_id,
                occurred_at=occurred_at,
            )
            activated = await self.store.activate_trigger(trigger, context)
            await self.logs.emit(
                trigger.project_id,
                kind="trigger.activity",
                action="trigger.matched",
                source="trigger",
                message=f"trigger matched event {event_name}",
                data={
                    "trigger_id": str(trigger.id),
                    "event_id": str(event_id),
                    "event_name": event_name,
                    "event_source": source.value,
                    "event_data": payload,
                    "trigger_data": trigger.data,
                    "activation_data": activation_data,
                    "target_node_id": str(trigger.target_node_id),
                    "activated_node_ids": [str(node.id) for node in activated],
                },
            )
            await self.events.publish({"type": "trigger.activated", "project_id": str(trigger.project_id), "data": {"trigger_id": str(trigger.id), "event_id": str(event_id), "target_node_id": str(trigger.target_node_id), "event_name": event_name}})
        await self.events.publish({"type": "trigger.emitted", "project_id": str(project_id) if project_id else None, "data": {**envelope, "matched": matched}})
        if matched and self._wake is not None:
            self._wake()
        return {**envelope, "matched": matched}
