"""Harness-neutral behavioral evidence and a small, fail-open metrics reducer.

The JSONL project log remains the evidence stream.  This module only
normalizes structured harness events and maintains a compact materialized
projection beside the project state; it never reads terminal transcripts.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HarnessEventKind(str, Enum):
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    COMMAND_START = "command_start"
    COMMAND_END = "command_end"
    WEB_SEARCH = "web_search"
    MCP_CALL = "mcp_call"
    SKILL_ACCESS = "skill_access"
    CONTEXT_ACCESS = "context_access"
    USAGE = "usage"
    ERROR = "error"
    STATUS = "status"


class HarnessEvent(BaseModel):
    """A common fact emitted by a harness adapter, independent of provider APIs."""

    model_config = ConfigDict(extra="forbid")

    kind: HarnessEventKind
    occurred_at: datetime = Field(default_factory=_utcnow)
    harness: str | None = None
    node_id: str | None = None
    run_id: str | None = None
    role: str | None = None
    model: str | None = None
    name: str | None = None
    status: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class BehaviorExpectations(BaseModel):
    """Small optional expectations. They remain separate from observed facts."""

    model_config = ConfigDict(extra="forbid")

    read_docs: bool | None = None
    use_skills: bool | None = None
    verify_after_changes: bool | None = None


class QualitativeAssessment(BaseModel):
    """Reserved append-only shape for a future independent qualitative judge."""

    model_config = ConfigDict(extra="forbid")

    name: str
    score: float | None = None
    assessment: str | None = None
    judge: str | None = None
    model: str | None = None
    rubric_version: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)


class BehaviorMetrics(BaseModel):
    """Small, generic materialized behavior record for a project or node."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    node_id: str | None = None
    role: str | None = None
    harness: str | None = None
    model: str | None = None
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    active_run_started_at: datetime | None = None
    duration_seconds: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    actions: int = 0
    errors: int = 0
    retries: int = 0
    failed_commands: int = 0
    repeated_failed_actions: int = 0
    files_read: int = 0
    files_written: int = 0
    docs_accessed: int = 0
    skills_accessed: int = 0
    mcp_calls: int = 0
    web_searches: int = 0
    verification_commands: int = 0
    graph_changes: int = 0
    harness_failures: int = 0
    recovery_actions: int = 0
    docs_before_action_runs: int = 0
    docs_before_action_successes: int = 0
    verification_after_change_runs: int = 0
    verification_after_change_successes: int = 0
    dynamic_usage: dict[str, int] = Field(default_factory=dict)
    role_metrics: dict[str, int] = Field(default_factory=dict)
    qualitative_assessments: list[QualitativeAssessment] = Field(default_factory=list)
    # Reducer state is retained so a daemon restart does not change ordering
    # or repeated-action observations in the middle of an active run.
    reducer_state: dict[str, Any] = Field(default_factory=dict)


class BehaviorRunMetrics(BehaviorMetrics):
    """Behavior evidence for one concrete harness attempt.

    A run is deliberately a projection of the same facts as its project and
    node, not a second event stream.  This gives optimization work a stable
    before/after comparison boundary and leaves a precise attachment point
    for a future independent qualitative judge.
    """

    run_id: str
    attempt: int | None = None
    status: str | None = None
    outcome: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


class ProjectBehaviorMetrics(BaseModel):
    """The persisted metrics projection for one project."""

    model_config = ConfigDict(extra="forbid")

    version: int = 6
    project: BehaviorMetrics
    by_node: dict[str, BehaviorMetrics] = Field(default_factory=dict)
    by_run: dict[str, BehaviorRunMetrics] = Field(default_factory=dict)


def _counter(metrics: BehaviorMetrics, key: str, amount: int = 1) -> None:
    metrics.dynamic_usage[key] = metrics.dynamic_usage.get(key, 0) + amount


def _role_counter(metrics: BehaviorMetrics, key: str, amount: int = 1) -> None:
    metrics.role_metrics[key] = metrics.role_metrics.get(key, 0) + amount


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _event_time(record: dict[str, Any]) -> datetime:
    raw = record.get("timestamp")
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _utcnow()


def _is_doc(value: str | None) -> bool:
    return bool(value and value.lower().rsplit("?", 1)[0].endswith((".md", ".mdx", ".txt", ".rst")))


def _is_verification_command(value: str | None) -> bool:
    if not value:
        return False
    lower = value.lower()
    return any(token in lower for token in ("test", "pytest", "vitest", "jest", "typecheck", "tsc", "lint", "build", "verify", "check"))


def _usage_values(value: dict[str, Any]) -> dict[str, float]:
    return {
        "input_tokens": _number(value.get("input_tokens")),
        "cached_input_tokens": _number(value.get("cached_input_tokens")),
        "output_tokens": _number(value.get("output_tokens")),
        "cost_usd": _number(value.get("cost_usd")),
    }


def _has_usage(value: dict[str, Any]) -> bool:
    return any(amount != 0 for amount in _usage_values(value).values())


def _add_usage(metrics: BehaviorMetrics, value: dict[str, Any], *, sign: int = 1) -> None:
    usage = _usage_values(value)
    metrics.input_tokens = max(0, metrics.input_tokens + sign * int(usage["input_tokens"]))
    metrics.cached_input_tokens = max(0, metrics.cached_input_tokens + sign * int(usage["cached_input_tokens"]))
    metrics.output_tokens = max(0, metrics.output_tokens + sign * int(usage["output_tokens"]))
    metrics.cost_usd = max(0.0, metrics.cost_usd + sign * usage["cost_usd"])


def _apply_harness_event(metrics: BehaviorMetrics, event: HarnessEvent) -> None:
    when = event.occurred_at
    metrics.first_observed_at = metrics.first_observed_at or when
    metrics.last_observed_at = when
    metrics.role = metrics.role or event.role
    metrics.harness = metrics.harness or event.harness
    metrics.model = metrics.model or event.model
    kind = event.kind
    name = event.name or "unknown"
    failed = (event.status or "").lower() in {"error", "failed", "failure", "cancelled", "declined"}
    state = metrics.reducer_state
    document_access = (
        (kind is HarnessEventKind.FILE_READ and _is_doc(str(event.data.get("path") or name)))
        or (kind is HarnessEventKind.CONTEXT_ACCESS and _is_doc(str(event.data.get("path") or name)))
    )
    if kind in {HarnessEventKind.TOOL_CALL, HarnessEventKind.FILE_READ, HarnessEventKind.FILE_WRITE,
                HarnessEventKind.COMMAND_START, HarnessEventKind.WEB_SEARCH, HarnessEventKind.MCP_CALL,
                HarnessEventKind.SKILL_ACCESS, HarnessEventKind.CONTEXT_ACCESS}:
        metrics.actions += 1
        if not state.get("run_saw_action") and not state.get("run_saw_docs") and not document_access:
            state["run_actions_before_docs"] = True
        state["run_saw_action"] = True
    if kind is HarnessEventKind.TOOL_CALL:
        _counter(metrics, f"tool:{name}")
    elif kind is HarnessEventKind.FILE_READ:
        metrics.files_read += 1
        _counter(metrics, "resource:file_read")
        path = str(event.data.get("path") or name)
        if _is_doc(path):
            metrics.docs_accessed += 1
            state["run_saw_docs"] = True
    elif kind is HarnessEventKind.FILE_WRITE:
        metrics.files_written += 1
        if state.get("run_saw_verification"):
            state["run_changed_after_verification"] = True
        state["run_saw_change"] = True
        _counter(metrics, "resource:file_write")
    elif kind is HarnessEventKind.COMMAND_START:
        _counter(metrics, f"command:{name}")
        if _is_verification_command(str(event.data.get("command") or name)):
            metrics.verification_commands += 1
            state["run_saw_verification"] = True
            if state.get("run_saw_change"):
                state["run_verification_after_change"] = True
    elif kind is HarnessEventKind.COMMAND_END:
        exit_code = event.data.get("exit_code")
        failed = failed or (exit_code is not None and _number(exit_code) != 0)
        if failed:
            metrics.failed_commands += 1
            signature = str(event.data.get("command") or name)
            failures = state.setdefault("failed_action_signatures", {})
            before = int(failures.get(signature, 0))
            failures[signature] = before + 1
            if before:
                metrics.repeated_failed_actions += 1
        if _is_verification_command(str(event.data.get("command") or name)):
            metrics.verification_commands += 1
            state["run_saw_verification"] = True
            if state.get("run_saw_change"):
                state["run_verification_after_change"] = True
    elif kind is HarnessEventKind.WEB_SEARCH:
        metrics.web_searches += 1
        _counter(metrics, "resource:web_search")
    elif kind is HarnessEventKind.MCP_CALL:
        metrics.mcp_calls += 1
        _counter(metrics, f"mcp:{name}")
    elif kind is HarnessEventKind.SKILL_ACCESS:
        metrics.skills_accessed += 1
        _counter(metrics, f"skill:{name}")
    elif kind is HarnessEventKind.CONTEXT_ACCESS:
        _counter(metrics, f"context:{name}")
        if _is_doc(str(event.data.get("path") or name)):
            metrics.docs_accessed += 1
            state["run_saw_docs"] = True
    elif kind is HarnessEventKind.USAGE:
        # A provider's structured usage is useful while the run is active,
        # but some providers report a cumulative session snapshot. The
        # durable Run usage is authoritative once Turn has it. Native sidecars
        # may arrive just after the handoff, so ignore provider usage received
        # after that durable result instead of adding it a second time.
        if isinstance(state.get("final_usage"), dict):
            return
        _add_usage(metrics, event.data)
        provisional = state.setdefault("provisional_usage", {
            "input_tokens": 0.0,
            "cached_input_tokens": 0.0,
            "output_tokens": 0.0,
            "cost_usd": 0.0,
        })
        for key, amount in _usage_values(event.data).items():
            provisional[key] = _number(provisional.get(key)) + amount
    elif kind is HarnessEventKind.ERROR:
        metrics.errors += 1
        _counter(metrics, f"error:{name}")
    elif kind is HarnessEventKind.STATUS:
        _counter(metrics, f"status:{event.status or name}")
    if failed and kind is not HarnessEventKind.COMMAND_END:
        metrics.errors += 1


def _apply_log_record(metrics: BehaviorMetrics, record: dict[str, Any]) -> None:
    when = _event_time(record)
    metrics.first_observed_at = metrics.first_observed_at or when
    metrics.last_observed_at = when
    kind = str(record.get("kind") or "")
    action = str(record.get("action") or "")
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    if kind == "harness.event":
        try:
            event = HarnessEvent.model_validate(data)
        except Exception:
            return
        _apply_harness_event(metrics, event)
        return
    if kind == "harness.launch":
        metrics.active_run_started_at = when
        metrics.role = metrics.role or _string(data.get("role"))
        metrics.harness = metrics.harness or _string(data.get("harness"))
        metrics.model = metrics.model or _string(data.get("model"))
        metrics.reducer_state = {
            "active_run_id": _string(data.get("run_id")),
            "last_run_id": _string(data.get("run_id")),
        }
        if isinstance(metrics, BehaviorRunMetrics):
            metrics.attempt = _integer(data.get("attempt")) or metrics.attempt
            metrics.status = "RUNNING"
            metrics.started_at = metrics.started_at or when
    elif kind == "harness.return":
        if metrics.active_run_started_at is not None:
            metrics.duration_seconds += max(0.0, (when - metrics.active_run_started_at).total_seconds())
            metrics.active_run_started_at = None
        # Runner publishes a live return event and Store emits the durable
        # run.updated record afterwards. Count final usage/failure once from
        # the latter, while using the first event for duration.
        if action == "run.updated":
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            if _has_usage(usage):
                state = metrics.reducer_state
                prior_final = state.get("final_usage")
                if isinstance(prior_final, dict):
                    _add_usage(metrics, prior_final, sign=-1)
                else:
                    provisional = state.get("provisional_usage")
                    if isinstance(provisional, dict):
                        _add_usage(metrics, provisional, sign=-1)
                _add_usage(metrics, usage)
                state["final_usage"] = _usage_values(usage)
            # A user cancellation is an observable outcome, but it is not a
            # harness failure. Count only an explicitly failed durable run
            # (or an error that prevented a normal result) here.
            run_status = str(data.get("status") or "")
            if run_status != "CANCELLED" and (run_status == "FAILED" or data.get("error")):
                metrics.harness_failures += 1
        if isinstance(metrics, BehaviorRunMetrics):
            metrics.status = _string(data.get("status")) or metrics.status
            metrics.outcome = _string(data.get("outcome")) or metrics.outcome
            metrics.ended_at = metrics.ended_at or when
        state = metrics.reducer_state
        # The runner publishes an immediate return observation, followed by
        # Store's durable ``run.updated`` log carrying final usage. Count the
        # run-level expectations once, while still accepting the later usage.
        if state.get("run_saw_action") and not state.get("run_observations_finalized"):
            metrics.docs_before_action_runs += 1
            if state.get("run_saw_docs") and not state.get("run_actions_before_docs"):
                metrics.docs_before_action_successes += 1
            if state.get("run_saw_change"):
                metrics.verification_after_change_runs += 1
                if state.get("run_verification_after_change") and not state.get("run_changed_after_verification"):
                    metrics.verification_after_change_successes += 1
            state["run_observations_finalized"] = True
    elif kind == "harness.run" and action == "run.updated" and str(data.get("status")) == "FAILED":
        metrics.harness_failures += 1
    elif kind == "application.error":
        metrics.errors += 1
        if metrics.role in {"setup", "planner"}:
            _role_counter(metrics, "invalid_graph_submissions")
        if isinstance(metrics, BehaviorRunMetrics):
            metrics.status = "FAILED"
            metrics.ended_at = metrics.ended_at or when
    elif kind == "configuration.changed":
        _counter(metrics, f"configuration:{action or 'changed'}")
    elif kind == "graph.transition":
        metrics.graph_changes += 1
        if action == "plan.applied":
            _role_counter(metrics, "valid_graph_submissions")
            _role_counter(metrics, "children_created", len(data.get("created_node_ids") or []))
            _role_counter(metrics, "dependencies_created", int(data.get("edge_count") or 0))
            if metrics.role == "planner":
                _role_counter(metrics, "replans", 1 if metrics.role_metrics.get("valid_graph_submissions", 0) > 1 else 0)
            if metrics.role == "setup":
                _role_counter(metrics, "top_level_planners_created", sum(1 for value in data.get("created_roles") or [] if value == "planner"))
        if action == "graph.replaced":
            _role_counter(metrics, "graph_replacements")
            _role_counter(metrics, "superseded_nodes", len(data.get("removed_node_ids") or []))
        if action in {"node.created", "plan.applied", "node.status"}:
            _counter(metrics, f"graph:{action}")
        if action == "node.status" and str(data.get("from")) == "FAILED" and str(data.get("to")) in {"RUNNABLE", "RUNNING"}:
            metrics.recovery_actions += 1
    elif kind == "agent.action" and action == "agent.submit":
        if data.get("kind") == "plan" and str(record.get("status")) == "error":
            _role_counter(metrics, "invalid_graph_submissions")
        if data.get("kind") == "verification":
            _role_counter(metrics, "verification_submissions")
    elif kind == "verification.completed":
        decision = str(data.get("decision") or "")
        if decision == "APPROVE":
            _role_counter(metrics, "accepts")
        elif decision == "REJECT":
            _role_counter(metrics, "rejections")
            _role_counter(metrics, "rejection_cycles")
    elif kind == "verification.outcome":
        decision = str(data.get("decision") or "")
        if decision == "APPROVE":
            _role_counter(metrics, "verifier_accepted")
        elif decision == "REJECT":
            _role_counter(metrics, "verifier_rejected")
    if kind == "state.changed" and action == "artifact.created":
        _role_counter(metrics, "artifacts", len(data.get("artifact_ids") or []))
    if kind == "graph.transition" and action == "node.status" and str(data.get("from")) in {"RUNNING", "FAILED", "CANCELLED"} and str(data.get("to")) == "RUNNABLE":
        # A new runnable state after an attempted run is an objective retry signal.
        if metrics.active_run_started_at is None:
            metrics.retries += 1


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class BehaviorMetricsStore:
    """Fail-open file projection driven exclusively by existing log records."""

    filename = "behavior-metrics.json"

    @classmethod
    def path(cls, project_root: Path) -> Path:
        return project_root / ".turn" / cls.filename

    @classmethod
    def read(cls, project_root: Path, project_id: str) -> ProjectBehaviorMetrics:
        try:
            return ProjectBehaviorMetrics.model_validate_json(cls.path(project_root).read_text(encoding="utf-8"))
        except Exception:
            return ProjectBehaviorMetrics(project=BehaviorMetrics(project_id=project_id))

    @classmethod
    def record(cls, project_root: Path, record: dict[str, Any]) -> None:
        """Update projection. Every failure is deliberately ignored by the caller."""
        project_id = str(record.get("project_id") or "")
        if not project_id:
            return
        current = cls.read(project_root, project_id)
        cls._record(current, record)
        cls._write(cls.path(project_root), current.model_dump(mode="json"))

    @classmethod
    def rebuild(
        cls,
        project_root: Path,
        project_id: str,
        records: list[dict[str, Any]],
    ) -> ProjectBehaviorMetrics:
        """Recreate a projection from the ordinary retained project logs.

        This is a deterministic migration path for projects that existed
        before run-level projections.  It does not invent observations and
        it remains safe to call repeatedly because it starts from an empty
        projection each time.
        """
        current = ProjectBehaviorMetrics(project=BehaviorMetrics(project_id=project_id))
        for record in records:
            if str(record.get("project_id") or "") == project_id:
                cls._record(current, record)
        cls._write(cls.path(project_root), current.model_dump(mode="json"))
        return current

    @staticmethod
    def _record(current: ProjectBehaviorMetrics, record: dict[str, Any]) -> None:
        project_id = str(record.get("project_id") or "")
        if not project_id:
            return
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        node_id = _string(data.get("node_id"))
        run_id = _string(data.get("run_id"))
        _apply_log_record(current.project, record)
        if node_id:
            node = current.by_node.setdefault(node_id, BehaviorMetrics(project_id=project_id, node_id=node_id))
            _apply_log_record(node, record)
            # Store-generated graph/verification records do not all carry a
            # run ID. Attribute such a record to the most recent launch for
            # its node, which is the only active user-visible attempt.
            run_id = run_id or _string(node.reducer_state.get("active_run_id")) or _string(node.reducer_state.get("last_run_id"))
        if run_id:
            run = current.by_run.setdefault(
                run_id,
                BehaviorRunMetrics(project_id=project_id, node_id=node_id, run_id=run_id),
            )
            _apply_log_record(run, record)

    @classmethod
    def append_assessment(
        cls,
        project_root: Path,
        project_id: str,
        run_id: str,
        assessment: QualitativeAssessment,
    ) -> None:
        """Append externally-produced qualitative evidence to one run.

        Turn does not judge behavior in this MVP.  Keeping this small write
        boundary allows a future judge to identify itself and its rubric
        without changing, interpreting, or overwriting the observations.
        """
        current = cls.read(project_root, project_id)
        run = current.by_run.get(run_id)
        if run is None:
            raise KeyError(f"behavior run not found: {run_id}")
        run.qualitative_assessments.append(assessment)
        cls._write(cls.path(project_root), current.model_dump(mode="json"))

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def evaluate_expectations(metrics: BehaviorMetrics, expectations: BehaviorExpectations | None) -> dict[str, bool | None]:
    """Return optional expectation checks without turning observations into scores."""
    if expectations is None:
        return {}
    return {
        "read_docs": None if expectations.read_docs is None else metrics.docs_accessed > 0,
        "use_skills": None if expectations.use_skills is None else metrics.skills_accessed > 0,
        "verify_after_changes": (
            None if expectations.verify_after_changes is None
            else (not metrics.reducer_state.get("run_saw_change") or metrics.verification_after_change_successes > 0 or metrics.verification_commands > 0)
        ),
    }


def _tool_events(
    *, harness: str, name: str | None, status: str | None = None,
    data: dict[str, Any] | None = None,
) -> list[HarnessEvent]:
    """Classify common tool facts without making resource names part of the model."""
    payload = data or {}
    tool = name or "unknown"
    lowered = tool.lower()
    events = [HarnessEvent(
        kind=HarnessEventKind.TOOL_CALL, harness=harness, name=tool, status=status, data=payload,
    )]
    if any(value in lowered for value in ("read", "view", "open_file")):
        events.append(HarnessEvent(kind=HarnessEventKind.FILE_READ, harness=harness, name=tool, status=status, data=payload))
    elif any(value in lowered for value in ("write", "edit", "patch", "apply")):
        events.append(HarnessEvent(kind=HarnessEventKind.FILE_WRITE, harness=harness, name=tool, status=status, data=payload))
    elif any(value in lowered for value in ("bash", "shell", "command", "exec", "terminal")):
        command_kind = (
            HarnessEventKind.COMMAND_START
            if (status or "").lower() in {"started", "running", "pending"}
            else HarnessEventKind.COMMAND_END
        )
        events.append(HarnessEvent(kind=command_kind, harness=harness, name=tool, status=status, data=payload))
    elif "search" in lowered and "web" in lowered:
        events.append(HarnessEvent(kind=HarnessEventKind.WEB_SEARCH, harness=harness, name=tool, status=status, data=payload))
    elif "skill" in lowered:
        events.append(HarnessEvent(kind=HarnessEventKind.SKILL_ACCESS, harness=harness, name=tool, status=status, data=payload))
    return events


def normalize_codex_event(raw: dict[str, Any]) -> list[HarnessEvent]:
    """Translate Codex JSONL and notify-exported rollout items into facts."""
    event_type = str(raw.get("type") or "")
    if event_type == "turn.codex.rollout":
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        if payload.get("type") == "turn_rollout":
            records = payload.get("records") if isinstance(payload.get("records"), list) else []
            events: list[HarnessEvent] = []
            for record in records:
                if isinstance(record, dict):
                    events.extend(_normalize_codex_rollout(record))
            return events
        return _normalize_codex_rollout(payload)
    item = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    details = item.get("details") if isinstance(item.get("details"), dict) else item
    detail_type = str(details.get("type") or item.get("type") or "")
    status = _string(details.get("status")) or _string(raw.get("status"))
    events: list[HarnessEvent] = []
    if event_type in {"item.started", "item.completed"}:
        if detail_type in {"command_execution", "command"}:
            payload = {"command": details.get("command"), "exit_code": details.get("exit_code")}
            events.append(HarnessEvent(
                kind=HarnessEventKind.COMMAND_START if event_type == "item.started" else HarnessEventKind.COMMAND_END,
                harness="codex", name=str(details.get("command") or "command"), status=status, data=payload,
            ))
        elif detail_type in {"file_change", "file_change_item"}:
            for change in details.get("changes") or []:
                if isinstance(change, dict):
                    events.append(HarnessEvent(
                        kind=HarnessEventKind.FILE_WRITE, harness="codex", status=status,
                        name=str(change.get("path") or "file"), data={"path": change.get("path"), "change": change.get("kind")},
                    ))
        elif detail_type in {"mcp_tool_call", "mcp"}:
            events.append(HarnessEvent(
                kind=HarnessEventKind.MCP_CALL, harness="codex",
                name=str(details.get("tool") or "mcp"), status=status,
                data={"server": details.get("server"), "arguments": details.get("arguments")},
            ))
        elif detail_type in {"web_search", "web_search_item"}:
            events.append(HarnessEvent(
                kind=HarnessEventKind.WEB_SEARCH, harness="codex", name="web_search", status=status,
                data={"query": details.get("query"), "action": details.get("action")},
            ))
        elif detail_type in {"agent_message", "message"}:
            events.append(HarnessEvent(
                kind=HarnessEventKind.STATUS,
                harness="codex",
                name="agent_message",
                status=status or "completed",
                data={"message": details.get("text") or details.get("message")},
            ))
    elif event_type in {"thread.started", "turn.started", "turn.completed"}:
        events.append(HarnessEvent(
            kind=HarnessEventKind.STATUS,
            harness="codex",
            name=event_type,
            status="completed" if event_type == "turn.completed" else "started",
        ))
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    if usage:
        events.append(HarnessEvent(kind=HarnessEventKind.USAGE, harness="codex", data=_usage_payload(usage)))
    if event_type.endswith(("failed", ".error")) or raw.get("error"):
        events.append(HarnessEvent(kind=HarnessEventKind.ERROR, harness="codex", name=event_type or "harness", data={"error": raw.get("error")}))
    return events


def _normalize_codex_rollout(record: dict[str, Any]) -> list[HarnessEvent]:
    """Normalize the structured rollout record delivered by Codex ``notify``.

    Rollouts are the same persisted JSON records Codex uses for its native
    session.  This is deliberately a provider adapter, not terminal parsing.
    """
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
    record_type = str(payload.get("type") or record.get("type") or "")
    item = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    item_type = str(item.get("type") or record_type)
    if item_type in {"function_call", "custom_tool_call"}:
        name = _string(item.get("name")) or _string(item.get("tool_name")) or "tool"
        data = {"arguments": item.get("arguments") or item.get("input"), "call_id": item.get("call_id")}
        events = _tool_events(harness="codex", name=name, status="started", data=data)
        if "mcp" in name.lower():
            events.append(HarnessEvent(kind=HarnessEventKind.MCP_CALL, harness="codex", name=name, status="started", data=data))
        return events
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return [HarnessEvent(
            kind=HarnessEventKind.TOOL_RESULT, harness="codex", name=_string(item.get("name")) or "tool",
            status="error" if item.get("error") else "completed",
            data={"output": item.get("output"), "error": item.get("error"), "call_id": item.get("call_id")},
        )]
    if item_type in {"web_search_call", "web_search_end", "web_search"}:
        return [HarnessEvent(kind=HarnessEventKind.WEB_SEARCH, harness="codex", name="web_search", status="completed", data={"query": item.get("query")})]
    if item_type == "token_count":
        info = item.get("info") if isinstance(item.get("info"), dict) else {}
        usage = info.get("last_token_usage") or info.get("total_token_usage")
        return [HarnessEvent(kind=HarnessEventKind.USAGE, harness="codex", data=_usage_payload(usage))] if isinstance(usage, dict) else []
    if item_type in {"task_complete", "task_started", "agent_message", "user_message"}:
        return [HarnessEvent(kind=HarnessEventKind.STATUS, harness="codex", name=item_type, status="completed" if item_type == "task_complete" else "started")]
    return []


def normalize_pi_event(raw: dict[str, Any]) -> list[HarnessEvent]:
    """Translate Pi JSON/RPC and extension lifecycle facts into common events."""
    event_type = str(raw.get("type") or "")
    name = _string(raw.get("toolName")) or _string(raw.get("tool_name"))
    payload = raw.get("args") if isinstance(raw.get("args"), dict) else raw.get("input")
    data = dict(payload) if isinstance(payload, dict) else {}
    if event_type == "tool_execution_start":
        events = _tool_events(harness="pi", name=name, status="started", data=data)
        if any(value in (name or "").lower() for value in ("bash", "shell", "command", "exec")):
            events.append(HarnessEvent(kind=HarnessEventKind.COMMAND_START, harness="pi", name=name, status="started", data=data))
        return events
    if event_type == "tool_execution_end":
        result = raw.get("result")
        if isinstance(result, dict):
            data = {**data, **result}
        status = "error" if raw.get("isError") else "completed"
        return _tool_events(harness="pi", name=name, status=status, data=data)
    if event_type in {"message_end", "turn_end"}:
        message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else raw.get("usage")
        return [HarnessEvent(kind=HarnessEventKind.USAGE, harness="pi", data=_usage_payload(usage))] if isinstance(usage, dict) else []
    if event_type in {"extension_error", "error"}:
        return [HarnessEvent(kind=HarnessEventKind.ERROR, harness="pi", name=event_type, data={"error": raw.get("error")})]
    if event_type == "context_access":
        events = [HarnessEvent(kind=HarnessEventKind.CONTEXT_ACCESS, harness="pi", name="context")]
        skills = raw.get("skills")
        if isinstance(skills, list):
            events.extend(HarnessEvent(kind=HarnessEventKind.SKILL_ACCESS, harness="pi", name=str(skill)) for skill in skills)
        return events
    if event_type in {"turn_start", "turn_end"}:
        return [HarnessEvent(kind=HarnessEventKind.STATUS, harness="pi", name=event_type,
                             status="completed" if event_type == "turn_end" else "started")]
    return []


def normalize_claude_event(raw: dict[str, Any]) -> list[HarnessEvent]:
    """Translate documented Claude lifecycle hook payloads into common facts."""
    payload = raw.get("payload") if raw.get("type") == "turn.claude.hook" and isinstance(raw.get("payload"), dict) else raw
    event_type = str(payload.get("hook_event_name") or payload.get("event") or "")
    name = _string(payload.get("tool_name")) or _string(payload.get("toolName"))
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if event_type == "PreToolUse":
        return _tool_events(harness="claude", name=name, status="started", data=tool_input)
    if event_type in {"PostToolUse", "PostToolUseFailure"}:
        status = "error" if event_type == "PostToolUseFailure" else "completed"
        events = [HarnessEvent(kind=HarnessEventKind.TOOL_RESULT, harness="claude", name=name or "tool", status=status,
                               data={"output": payload.get("tool_response"), "input": tool_input})]
        lowered = (name or "").lower()
        if any(token in lowered for token in ("bash", "shell", "command", "exec", "terminal")):
            command = tool_input.get("command") or tool_input.get("cmd")
            events.append(HarnessEvent(kind=HarnessEventKind.COMMAND_END, harness="claude", name=str(command or name or "command"), status=status,
                                       data={"command": command, "exit_code": payload.get("exit_code")}))
        return events
    if event_type in {"SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest"}:
        return [HarnessEvent(kind=HarnessEventKind.STATUS, harness="claude", name=event_type, status="completed" if event_type == "Stop" else "started")]
    usage = payload.get("usage")
    return [HarnessEvent(kind=HarnessEventKind.USAGE, harness="claude", data=_usage_payload(usage))] if isinstance(usage, dict) else []


def normalize_opencode_event(raw: dict[str, Any]) -> list[HarnessEvent]:
    """Translate OpenCode JSON/SSE message-part events into common facts."""
    event_type = str(raw.get("type") or "")
    properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
    part = properties.get("part") if isinstance(properties.get("part"), dict) else raw.get("part")
    if not isinstance(part, dict):
        return [HarnessEvent(kind=HarnessEventKind.ERROR, harness="opencode", name=event_type, data={"error": raw.get("error")})] if raw.get("error") else []
    part_type = str(part.get("type") or "")
    if part_type in {"tool", "tool_use"}:
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = _string(state.get("status")) or _string(part.get("status"))
        name = _string(part.get("tool")) or _string(part.get("name"))
        data = {"input": part.get("input"), "output": part.get("output"), "error": state.get("error")}
        return _tool_events(harness="opencode", name=name, status=status, data=data)
    if part_type in {"step-finish", "step_finish"}:
        return [HarnessEvent(kind=HarnessEventKind.USAGE, harness="opencode", data=_usage_payload(part.get("tokens") or part.get("usage")))]
    return []


def _usage_payload(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    cache = value.get("cache") if isinstance(value.get("cache"), dict) else {}
    return {
        "input_tokens": value.get("input_tokens") or value.get("inputTokens") or value.get("input") or 0,
        "cached_input_tokens": value.get("cached_input_tokens") or value.get("cache_read_input_tokens") or cache.get("read") or 0,
        "output_tokens": value.get("output_tokens") or value.get("outputTokens") or value.get("output") or 0,
        "cost_usd": value.get("cost_usd") or value.get("cost") or 0,
    }


def normalize_harness_event(harness: str, raw: dict[str, Any]) -> list[HarnessEvent]:
    if harness == "codex":
        return normalize_codex_event(raw)
    if harness == "pi":
        return normalize_pi_event(raw)
    if harness == "opencode":
        return normalize_opencode_event(raw)
    if harness == "claude":
        return normalize_claude_event(raw)
    return []


async def emit_jsonl_telemetry(
    harness: str, output: str, sink: Any,
) -> None:
    """Best-effort adapter output handling; malformed lines are not evidence."""
    if sink is None:
        return
    for line in output.splitlines():
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue
            for event in normalize_harness_event(harness, raw):
                await sink(event)
        except Exception:
            continue
