# Mistakes and verifiability gaps

This document is meant to document the mistakes _found_ as a part of doing a run with real provider data, and document the gaps in verifiability from having assumed correctness. Append here only if you found issues in a run with real provider data. Do not use this document to record issues found in a test run with synthetic data or during development.


## 2026-08-21 — real-provider run (wordutils demo, pi harness)

Found during a real end-to-end run (plan → audit → reject → re-plan → audit → execution → completion):

1. **Semantic plan auditors ended their turns without publishing a handoff.** The audit prompt said "Return exactly one normal Turn WorkerResult envelope", which providers read as a chat reply; the control run then sat open until the 600s timeout, retried, and failed again. Fix: the prompt now demands an accepted handoff submission and states that a chat reply does not settle the audit, while deferring to the turn-basics skill for the protocol itself. Regression: `turn/tests/test_submission_contract.py`.
   - Verifiability gap closed: no test covered audit-prompt wording; `render_plan_audit_prompt` was extracted for that purpose.
2. **First-attempt fix violated the architecture and was caught by guardrail tests.** I embedded a CLI submission contract block into every worker/planner prompt; `test_initial_prompt_is_only_node_data_and_activations` (<600 chars) and `test_codex_worker_keeps_handoff_protocol_in_turn_basics_skill` ("turn agent submit" must not appear in prompts) correctly rejected it: handoff protocol's single home is the turn-basics skill. Reverted; only the contradictory sentence in the audit prompt was repaired.
3. **Terminal tab silently swapped to the control terminal.** With an active plan audit, TerminalView replaced the organization agent's terminal with the synthetic control pane (`terminalNodeId || node.id`). Fix: the agent shell stays primary; the control surface is an explicit switcher ("Agent" / "Plan audit" / "Manager review"). Regression: `ui/src/components/TerminalView.test.tsx`.
4. **Operational note (not a code bug):** harness permission prompts are expected to pause agents until a human approves in the visible Herdr pane. Two planner/auditor timeouts were caused by such waits, not by orchestration defects.

## 2026-08-21 — lead/escalation implementation, real-provider bootstrap run (proj-83789cb9, codex harness)

Found while implementing and verifying LEAD_ESCALATION.md (lead-owned root review, nested parent review, escalation ladder, bootstrap automation):

1. **`Store.get_runs` could not see runs owned by non-graph identities.** The project lead's terminal owner id is not a graph node, so `get_runs` returned `[]` for lead review runs even though they were persisted. Root cause: run lookup required node existence. Fix: fall back to scanning all project states when the id resolves to no node. Regression: `test_lead_escalation.py` asserts lead runs are observable.
2. **`GraphWalker` input-shape confusion.** `_parent_planner_for` first passed a dict of nodes where the walker expects a list, then treated ancestor handles as raw ids when they are nodes. Both caught immediately by tests; fixed by passing a list and normalizing with `getattr(a, "id", a)`.
3. **Bootstrap completion is observed on the scheduler pass after acceptance**, not synchronously during launch. The test initially asserted READY one tick too early. The durable trail (`project.bootstrap` events) makes the two-phase transition visible.
4. **Operational note:** codex's directory-trust prompt blocked the auto-launched bootstrap planner exactly as designed; the operator accepted it in the visible Herdr pane. Bootstrap then ran to acceptance fully autonomously: PLAN_REVIEW SETTLED(APPROVE) → plan applied → BOOTSTRAPPING→READY → Step launched exactly one frontier node → 3/3 real pytest pass.
