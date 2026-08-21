# Mistakes and verifiability gaps

This document is meant to document the mistakes _found_ as a part of doing a run with real provider data, and document the gaps in verifiability from having assumed correctness. Append here only if you found issues in a run with real provider data. Do not use this document to record issues found in a test run with synthetic data or during development.


## 2026-08-21 — real-provider run (wordutils demo, pi harness)

Found during a real end-to-end run (plan → audit → reject → re-plan → audit → execution → completion):

1. **Semantic plan auditors ended their turns without publishing a handoff.** The audit prompt said "Return exactly one normal Turn WorkerResult envelope", which providers read as a chat reply; the control run then sat open until the 600s timeout, retried, and failed again. Fix: the prompt now demands an accepted handoff submission and states that a chat reply does not settle the audit, while deferring to the turn-basics skill for the protocol itself. Regression: `turn/tests/test_submission_contract.py`.
   - Verifiability gap closed: no test covered audit-prompt wording; `render_plan_audit_prompt` was extracted for that purpose.
2. **First-attempt fix violated the architecture and was caught by guardrail tests.** I embedded a CLI submission contract block into every worker/planner prompt; `test_initial_prompt_is_only_node_data_and_activations` (<600 chars) and `test_codex_worker_keeps_handoff_protocol_in_turn_basics_skill` ("turn agent submit" must not appear in prompts) correctly rejected it: handoff protocol's single home is the turn-basics skill. Reverted; only the contradictory sentence in the audit prompt was repaired.
3. **Terminal tab silently swapped to the control terminal.** With an active plan audit, TerminalView replaced the organization agent's terminal with the synthetic control pane (`terminalNodeId || node.id`). Fix: the agent shell stays primary; the control surface is an explicit switcher ("Agent" / "Plan audit" / "Manager review"). Regression: `ui/src/components/TerminalView.test.tsx`.
4. **Operational note (not a code bug):** harness permission prompts are expected to pause agents until a human approves in the visible Herdr pane. Two planner/auditor timeouts were caused by such waits, not by orchestration defects.
