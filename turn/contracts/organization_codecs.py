"""Canonical codecs for provider-backed organization control operations."""
from __future__ import annotations

import json
from typing import Any

from turn.contracts.text import sanitize_control_text
from turn.contracts.dag import parse_result
from turn.domain.organization import PlanAuditResult
from turn.domain.schemas import ManagerResult


def _decode(value: str | dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, dict):
        raise ValueError("organization control payload must be a JSON object")
    return payload


def parse_structured_artifact(
    value: str | dict[str, Any], *, schema_name: str, artifact_name: str,
    schema_version: str = "v1",
) -> dict[str, Any]:
    """Extract one JSON artifact through the shared WorkerResult codec."""
    result = parse_result(_decode(value))
    for artifact in result.artifacts:
        if artifact.name == artifact_name and artifact.schema_name == schema_name and artifact.schema_version == schema_version:
            content = artifact.content
            if isinstance(content, str):
                content = json.loads(content)
            if not isinstance(content, dict):
                raise ValueError(f"provider artifact '{artifact_name}' must contain a JSON object")
            return content
    raise RuntimeError(f"provider returned no '{artifact_name}' artifact in its WorkerResult")


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    rendered: list[str] = []
    for item in values:
        if isinstance(item, str):
            rendered.append(sanitize_control_text(item))
            continue
        if isinstance(item, dict):
            message = item.get("finding") or item.get("description") or item.get("message") or item.get("text")
            if message is None:
                message = json.dumps(item, sort_keys=True)
            prefix = ": ".join(str(part) for part in (item.get("area"), item.get("severity")) if part)
            rendered.append(sanitize_control_text(f"{prefix}: {message}" if prefix else message))
            continue
        rendered.append(sanitize_control_text(item))
    return rendered


def parse_plan_audit(value: str | dict[str, Any]) -> PlanAuditResult:
    normalized = dict(_decode(value))
    normalized["decision"] = str(normalized.get("decision", "")).upper()
    normalized["summary"] = sanitize_control_text(normalized.get("summary"))
    normalized["findings"] = _text_list(normalized.get("findings"))
    normalized["required_changes"] = _text_list(normalized.get("required_changes"))
    return PlanAuditResult.model_validate(normalized)


def parse_manager_result(value: str | dict[str, Any]) -> ManagerResult:
    normalized = dict(_decode(value))
    report = normalized.get("plan")
    report_status = None
    report_handoff = None
    if isinstance(report, dict) and "nodes" not in report:
        report_status = report.get("status")
        report_handoff = report.get("exported_handoff")
        normalized.pop("plan", None)
    decision = normalized.get("decision") or normalized.get("status") or report_status
    if decision is not None:
        normalized["decision"] = str(decision).upper()
    if not normalized.get("summary"):
        handoff = normalized.get("exported_handoff") or report_handoff
        normalized["summary"] = f"Manager reviewed {handoff}" if handoff else f"Manager decision: {str(decision).upper()}" if decision is not None else "Manager returned a decision"
    normalized["summary"] = sanitize_control_text(normalized["summary"])
    items = normalized.get("work_items")
    if isinstance(items, list):
        normalized_items: list[Any] = []
        for raw in items:
            if not isinstance(raw, dict):
                normalized_items.append(raw)
                continue
            item = dict(raw)
            objective = item.get("objective")
            if "title" not in item and objective:
                item["title"] = objective
            if "instructions" not in item and objective:
                item["instructions"] = objective
            if isinstance(item.get("title"), str):
                item["title"] = sanitize_control_text(item["title"])
            if isinstance(item.get("instructions"), str):
                item["instructions"] = sanitize_control_text(item["instructions"])
            if "depends_on" not in item and "dependencies" in item:
                item["depends_on"] = item["dependencies"]
            if isinstance(item.get("acceptance_criteria"), list):
                item["acceptance_criteria"] = [
                    criterion if isinstance(criterion, dict) else {
                        "id": f"criterion-{index}", "description": sanitize_control_text(criterion),
                    }
                    for index, criterion in enumerate(item["acceptance_criteria"], start=1)
                ]
            item.pop("objective", None)
            item.pop("dependencies", None)
            normalized_items.append(item)
        normalized["work_items"] = normalized_items
    for field in ("status", "completed_nodes", "evidence_refs", "exported_handoff"):
        normalized.pop(field, None)
    return ManagerResult.model_validate(normalized)
