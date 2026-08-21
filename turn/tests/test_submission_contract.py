"""Regression tests for the audit prompt's handoff wording.

Real runs showed semantic auditors treating "return an envelope" as a chat
reply and ending their turn without publishing the handoff that settles the
audit, leaving the control run open until the timeout. The prompt must demand
an accepted skill-documented handoff without restating the protocol (the
protocol's single home is the turn-basics skill).
"""
from __future__ import annotations

from turn.runner.runner import render_plan_audit_prompt


class _Contract:
    def model_dump(self, mode="json"):
        return {"charter": "c"}


class _Plan:
    def model_dump(self, mode="json"):
        return {"nodes": []}


def test_plan_audit_prompt_demands_a_published_handoff() -> None:
    prompt = render_plan_audit_prompt("CONTEXT", _Contract(), _Plan())
    assert "TURN_INDEPENDENT_PLAN_AUDIT" in prompt
    # Chat replies must not be mistaken for settling the audit.
    assert "A chat reply does not settle the audit" in prompt
    # The protocol itself stays in turn-basics; the prompt only points there.
    assert "turn-basics" in prompt
    assert "turn agent submit" not in prompt


def test_plan_audit_prompt_drops_ambiguous_return_wording() -> None:
    prompt = render_plan_audit_prompt("CONTEXT", _Contract(), _Plan())
    assert "Return exactly one normal Turn WorkerResult envelope" not in prompt


def test_plan_audit_prompt_explains_transitive_sequence_ordering() -> None:
    """Real runs burned all plan corrections on a catch-22: the auditor
    demanded a direct `follows` edge that the mechanical validator rejects as
    a transitive shortcut. The prompt must state the ordering semantics so an
    auditor never asks for a forbidden edge again.
    """
    prompt = render_plan_audit_prompt("CONTEXT", _Contract(), _Plan())
    assert "ordering is transitive" in prompt
    assert "rejected as a shortcut" in prompt
    assert "never demand a direct follows edge" in prompt
