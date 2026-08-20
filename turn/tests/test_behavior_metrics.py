"""Deterministic coverage for normalized behavioral evidence and role views."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from turn.metrics import (
    BehaviorExpectations,
    BehaviorMetricsStore,
    HarnessEvent,
    HarnessEventKind,
    QualitativeAssessment,
    evaluate_expectations,
    normalize_codex_event,
    normalize_opencode_event,
    normalize_pi_event,
)


def _record(root, project_id, kind, *, node_id=None, action=None, data=None, seconds=0):
    record = {
        "project_id": project_id,
        "kind": kind,
        "timestamp": (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat(),
        "data": {**(data or {}), **({"node_id": node_id} if node_id else {})},
    }
    if action:
        record["action"] = action
    BehaviorMetricsStore.record(root, record)


def _launch(root, project_id, node_id, role, second=0):
    _record(root, project_id, "harness.launch", node_id=node_id, seconds=second, data={
        "role": role, "harness": "codex", "model": "model-a",
    })


def _event(root, project_id, node_id, kind, *, second, name=None, data=None, status=None):
    event = HarnessEvent(
        kind=kind, occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=second),
        node_id=node_id, name=name, status=status, data=data or {},
    )
    _record(root, project_id, "harness.event", node_id=node_id, seconds=second, data=event.model_dump(mode="json"))


def _return(root, project_id, node_id, second=20, outcome="COMPLETE"):
    _record(root, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=second, data={
        "outcome": outcome,
        "usage": {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 5, "cost_usd": 0.25},
    })


def test_reducer_records_setup_planner_executor_integrator_and_verifier_evidence(tmp_path):
    project_id = "project-1"
    # Setup: document/research before work, valid graph, invalid submission and a graph replacement.
    _launch(tmp_path, project_id, "setup", "setup")
    _event(tmp_path, project_id, "setup", HarnessEventKind.FILE_READ, second=1, name="DESIGN.md", data={"path": "DESIGN.md"})
    _event(tmp_path, project_id, "setup", HarnessEventKind.WEB_SEARCH, second=2, name="web")
    _record(tmp_path, project_id, "graph.transition", node_id="setup", action="plan.applied", seconds=3, data={
        "created_node_ids": ["planner", "executor"], "created_roles": ["planner", "executor"], "edge_count": 2,
    })
    _record(tmp_path, project_id, "application.error", node_id="setup", seconds=4, data={"phase": "planner"})
    _record(tmp_path, project_id, "graph.transition", node_id="setup", action="graph.replaced", seconds=5, data={"removed_node_ids": ["old"]})
    _return(tmp_path, project_id, "setup")

    # Planner: valid submission and a later replan.
    _launch(tmp_path, project_id, "planner", "planner")
    _record(tmp_path, project_id, "graph.transition", node_id="planner", action="plan.applied", seconds=2, data={"created_node_ids": ["child"], "edge_count": 1})
    _record(tmp_path, project_id, "graph.transition", node_id="planner", action="plan.applied", seconds=3, data={"created_node_ids": ["child-2"], "edge_count": 1})
    _return(tmp_path, project_id, "planner")

    # Executor: final edit then a verification command; a repeated failed command and recovery.
    _launch(tmp_path, project_id, "executor", "executor")
    _event(tmp_path, project_id, "executor", HarnessEventKind.FILE_WRITE, second=1, name="src/app.py", data={"path": "src/app.py"})
    _event(tmp_path, project_id, "executor", HarnessEventKind.COMMAND_END, second=2, name="pytest", data={"command": "pytest", "exit_code": 1})
    _event(tmp_path, project_id, "executor", HarnessEventKind.COMMAND_END, second=3, name="pytest", data={"command": "pytest", "exit_code": 1})
    _event(tmp_path, project_id, "executor", HarnessEventKind.COMMAND_END, second=4, name="pytest", data={"command": "pytest", "exit_code": 0})
    _record(tmp_path, project_id, "graph.transition", node_id="executor", action="node.status", seconds=5, data={"from": "FAILED", "to": "RUNNABLE"})
    _record(tmp_path, project_id, "verification.outcome", node_id="executor", seconds=6, data={"decision": "REJECT"})
    _return(tmp_path, project_id, "executor")

    # Integrator: integration check and an encountered error.
    _launch(tmp_path, project_id, "integrator", "integrator")
    _event(tmp_path, project_id, "integrator", HarnessEventKind.COMMAND_END, second=1, name="npm test", data={"command": "npm test", "exit_code": 0})
    _event(tmp_path, project_id, "integrator", HarnessEventKind.ERROR, second=2, name="merge_conflict")
    _return(tmp_path, project_id, "integrator")

    # Verifier: both decisions remain observable for later false-decision evidence.
    _launch(tmp_path, project_id, "verifier", "verifier")
    _event(tmp_path, project_id, "verifier", HarnessEventKind.FILE_READ, second=1, name="src/app.py", data={"path": "src/app.py"})
    _event(tmp_path, project_id, "verifier", HarnessEventKind.COMMAND_END, second=2, name="typecheck", data={"command": "npm run typecheck", "exit_code": 0})
    _record(tmp_path, project_id, "verification.completed", node_id="verifier", seconds=3, data={"decision": "REJECT"})
    _record(tmp_path, project_id, "verification.completed", node_id="verifier", seconds=4, data={"decision": "APPROVE"})
    _return(tmp_path, project_id, "verifier")

    metrics = BehaviorMetricsStore.read(tmp_path, project_id)
    setup = metrics.by_node["setup"]
    assert setup.docs_before_action_successes == 1
    assert setup.web_searches == 1
    assert setup.role_metrics["valid_graph_submissions"] == 1
    assert setup.role_metrics["invalid_graph_submissions"] == 1
    assert setup.role_metrics["top_level_planners_created"] == 1
    assert setup.role_metrics["graph_replacements"] == 1
    planner = metrics.by_node["planner"]
    assert planner.role_metrics["valid_graph_submissions"] == 2
    assert planner.role_metrics["replans"] == 1
    executor = metrics.by_node["executor"]
    assert executor.verification_after_change_successes == 1
    assert executor.failed_commands == 2
    assert executor.repeated_failed_actions == 1
    assert executor.recovery_actions == 1
    assert executor.role_metrics["verifier_rejected"] == 1
    integrator = metrics.by_node["integrator"]
    assert integrator.verification_commands == 1
    assert integrator.errors == 1
    verifier = metrics.by_node["verifier"]
    assert verifier.role_metrics["accepts"] == 1
    assert verifier.role_metrics["rejections"] == 1
    assert verifier.role_metrics["rejection_cycles"] == 1
    assert verifier.input_tokens == 11 and verifier.cached_input_tokens == 3 and verifier.output_tokens == 5
    assert verifier.cost_usd == 0.25
    assert evaluate_expectations(setup, BehaviorExpectations(read_docs=True, use_skills=True))["read_docs"] is True


def test_harness_adapters_normalize_structured_events_without_provider_names_in_metrics():
    codex = normalize_codex_event({"type": "item.completed", "item": {"details": {
        "type": "command_execution", "command": "pytest", "exit_code": 1, "status": "failed",
    }}})
    assert {event.kind for event in codex} == {HarnessEventKind.COMMAND_END}
    assert normalize_codex_event({"type": "item.completed", "item": {"details": {"type": "mcp_tool_call", "tool": "search", "server": "anything"}}})[0].kind is HarnessEventKind.MCP_CALL
    assert normalize_codex_event({"type": "item.completed", "item": {"details": {"type": "web_search", "query": "docs"}}})[0].kind is HarnessEventKind.WEB_SEARCH
    pi = normalize_pi_event({"type": "tool_execution_end", "toolName": "write", "args": {"path": "a.py"}, "isError": False})
    assert HarnessEventKind.FILE_WRITE in {event.kind for event in pi}
    opencode = normalize_opencode_event({"type": "message.part.updated", "properties": {"part": {
        "type": "tool", "tool": "read", "state": {"status": "completed"}, "input": {"path": "README.md"},
    }}})
    assert HarnessEventKind.FILE_READ in {event.kind for event in opencode}


def test_run_projection_keeps_attempts_separate_and_accepts_future_assessments(tmp_path):
    project_id = "project-runs"
    node_id = "executor"
    first = "run-first"
    second = "run-second"

    _record(tmp_path, project_id, "harness.launch", node_id=node_id, seconds=0, data={
        "run_id": first, "attempt": 1, "role": "executor", "harness": "codex", "model": "luna",
    })
    first_event = HarnessEvent(
        kind=HarnessEventKind.FILE_WRITE,
        node_id=node_id,
        run_id=first,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=1),
        name="src/app.py",
        data={"path": "src/app.py"},
    )
    _record(tmp_path, project_id, "harness.event", node_id=node_id, seconds=1, data=first_event.model_dump(mode="json"))
    _record(tmp_path, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=4, data={
        "run_id": first, "status": "FAILED", "outcome": "FAIL", "error": "test failed",
        "usage": {"input_tokens": 10, "cached_input_tokens": 2, "output_tokens": 4},
    })

    _record(tmp_path, project_id, "harness.launch", node_id=node_id, seconds=10, data={
        "run_id": second, "attempt": 2, "role": "executor", "harness": "codex", "model": "luna",
    })
    second_event = HarnessEvent(
        kind=HarnessEventKind.COMMAND_END,
        node_id=node_id,
        run_id=second,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=12),
        name="pytest",
        data={"command": "pytest", "exit_code": 0},
    )
    _record(tmp_path, project_id, "harness.event", node_id=node_id, seconds=12, data=second_event.model_dump(mode="json"))
    _record(tmp_path, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=14, data={
        "run_id": second, "status": "COMPLETE", "outcome": "COMPLETE",
        "usage": {"input_tokens": 20, "cached_input_tokens": 6, "output_tokens": 8},
    })

    BehaviorMetricsStore.append_assessment(
        tmp_path,
        project_id,
        second,
        QualitativeAssessment(name="execution discipline", score=0.8, judge="future-judge", rubric_version="v1"),
    )
    metrics = BehaviorMetricsStore.read(tmp_path, project_id)
    assert set(metrics.by_run) == {first, second}
    assert metrics.by_run[first].attempt == 1
    assert metrics.by_run[first].status == "FAILED"
    assert metrics.by_run[first].files_written == 1
    assert metrics.by_run[first].input_tokens == 10
    assert metrics.by_run[second].attempt == 2
    assert metrics.by_run[second].status == "COMPLETE"
    assert metrics.by_run[second].verification_commands == 1
    assert metrics.by_run[second].input_tokens == 20
    assert metrics.by_run[second].qualitative_assessments[0].name == "execution discipline"


def test_final_run_usage_replaces_provisional_structured_usage(tmp_path):
    project_id = "project-usage"
    node_id = "executor"
    run_id = "run-usage"
    _record(tmp_path, project_id, "harness.launch", node_id=node_id, seconds=0, data={
        "run_id": run_id, "attempt": 1, "role": "executor", "harness": "codex",
    })
    event = HarnessEvent(
        kind=HarnessEventKind.USAGE,
        node_id=node_id,
        run_id=run_id,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=1),
        data={"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 40},
    )
    _record(tmp_path, project_id, "harness.event", node_id=node_id, seconds=1, data=event.model_dump(mode="json"))
    _record(tmp_path, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=3, data={
        "run_id": run_id, "status": "COMPLETE", "outcome": "COMPLETE",
        "usage": {"input_tokens": 12, "cached_input_tokens": 8, "output_tokens": 3},
    })
    # A native sidecar can be flushed after Turn has already persisted the
    # authoritative completion usage. It remains behavioral evidence, but
    # must not inflate the final run token total.
    late_event = HarnessEvent(
        kind=HarnessEventKind.USAGE,
        node_id=node_id,
        run_id=run_id,
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=4),
        data={"input_tokens": 1000, "cached_input_tokens": 900, "output_tokens": 40},
    )
    _record(tmp_path, project_id, "harness.event", node_id=node_id, seconds=4, data=late_event.model_dump(mode="json"))

    run = BehaviorMetricsStore.read(tmp_path, project_id).by_run[run_id]
    assert (run.input_tokens, run.cached_input_tokens, run.output_tokens) == (12, 8, 3)


def test_cancelled_attempt_is_not_counted_as_a_harness_failure(tmp_path):
    project_id = "project-cancelled"
    node_id = "executor"
    _record(tmp_path, project_id, "harness.launch", node_id=node_id, seconds=0, data={
        "run_id": "cancelled", "role": "executor", "harness": "codex",
    })
    _record(tmp_path, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=1, data={
        "run_id": "cancelled", "status": "CANCELLED", "outcome": "FAIL", "error": "run cancelled by user", "usage": {},
    })
    _record(tmp_path, project_id, "harness.launch", node_id=node_id, seconds=2, data={
        "run_id": "failed", "role": "executor", "harness": "codex",
    })
    _record(tmp_path, project_id, "harness.return", node_id=node_id, action="run.updated", seconds=3, data={
        "run_id": "failed", "status": "FAILED", "outcome": "FAIL", "usage": {},
    })

    metrics = BehaviorMetricsStore.read(tmp_path, project_id)
    assert metrics.by_run["cancelled"].harness_failures == 0
    assert metrics.by_run["failed"].harness_failures == 1
    assert metrics.project.harness_failures == 1
