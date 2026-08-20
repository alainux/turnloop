from __future__ import annotations

import json

from turn.contracts.dag import parse_result, parse_verification, validate_agent_submission
from turn.contracts.organization_codecs import (
    parse_manager_result,
    parse_plan_audit,
    parse_structured_artifact,
)
from turn.contracts.text import sanitize_control_text
from turn.domain.organization import EvidenceStatus
from turn.domain.schemas import ArtifactKind


def test_result_artifact_aliases_are_canonical_across_submission_paths():
    payload = {
        "outcome": "COMPLETE",
        "summary": "tested",
        "artifacts": [{"kind": "test-report", "name": "report", "content": "ok"}],
    }
    cli = validate_agent_submission("result", payload)
    shared = parse_result(payload)
    assert cli.model_dump(mode="json") == shared.model_dump(mode="json")
    assert shared.artifacts[0].kind is ArtifactKind.EVIDENCE

    envelope = {
        "outcome": "COMPLETE",
        "artifacts": [{
            "kind": "json",
            "name": "manager-result",
            "schema_name": "turn.manager-result",
            "schema_version": "v1",
            "content": json.dumps({"decision": "ACCEPT", "summary": "done"}),
        }],
    }
    assert parse_structured_artifact(
        envelope,
        schema_name="turn.manager-result",
        artifact_name="manager-result",
    )["decision"] == "ACCEPT"


def test_verification_evidence_refs_object_form_matches_canonical_evidence():
    evidence = {
        "criterion_id": "smoke",
        "status": "PASS",
        "summary": "The smoke test passed.",
        "refs": ["reports/smoke.txt"],
    }
    canonical = parse_verification({"decision": "APPROVE", "summary": "ok", "evidence": [evidence]})
    alternate = validate_agent_submission(
        "verification",
        {"decision": "APPROVE", "summary": "ok", "evidence_refs": [json.dumps(evidence)]},
    )
    assert alternate.evidence == canonical.evidence
    assert alternate.evidence[0].status is EvidenceStatus.PASS


def test_manager_alias_and_plan_audit_finding_forms_have_one_typed_result():
    manager = parse_manager_result({
        "status": "CONTINUE",
        "plan": {"status": "CONTINUE", "exported_handoff": "review"},
        "work_items": [{
            "key": "follow-up",
            "objective": "Address the finding",
            "dependencies": [],
            "acceptance_criteria": ["The finding is addressed."],
        }],
    })
    assert manager.decision.value == "CONTINUE"
    assert manager.plan is None
    assert manager.work_items[0].title == "Address the finding"
    assert manager.work_items[0].instructions == "Address the finding"
    assert manager.work_items[0].acceptance_criteria[0].description == "The finding is addressed."

    audit = parse_plan_audit({
        "decision": "approve",
        "summary": "Looks good",
        "findings": [{"area": "ownership", "severity": "advisory", "message": "Clarify wording."}],
    })
    assert audit.decision.value == "APPROVE"
    assert audit.findings == ["ownership: advisory: Clarify wording."]


def test_control_text_sanitization_removes_terminal_sequences_and_bounds_feedback():
    raw = "\x1b[2J\x1b[1;1H\x1b[31mBad\x1b[0m\x1b]0;title\x07\r\n\x00next"
    clean = sanitize_control_text(raw)
    assert "\x1b" not in clean
    assert "Bad" in clean and "next" in clean
    assert len(sanitize_control_text("x" * 5000)) <= 4000
