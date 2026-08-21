# P0–P1 Final Closure Pass

Do not add architecture or features. Fix the remaining closure defects, then certify P0–P1 on one unchanged revision.

## 1. Fix PTY drain correctness

`test_local_pty_detects_silent_live_process` currently fails reproducibly: the child prints `started`, then stalls, Turn detects the stall, but `TerminalResult.output` can be empty.

Fix the read/termination ordering so **all bytes emitted before process termination are drained and preserved** before the terminal result settles.

Required:

- explicit EOF remains the only EOF signal;
- EAGAIN remains non-terminal;
- stall/stop/timeout cannot discard already-written bytes;
- decoder final flush occurs after the PTY is drained.

**Pass condition:**  
`test_local_pty_detects_silent_live_process` passes **20 consecutive runs**, and the entire terminal transport suite passes.

## 2. Repair the Pi/OpenCode native test fixtures

These two tests currently fail:

`test_native_executor_persists_provider_session_before_result[pi]`  
`test_native_executor_persists_provider_session_before_result[opencode]`

The runtime now correctly refuses to launch a selected harness when its binary is unavailable. The tests mock `LocalPtyTransport` but fail to mock/satisfy that preflight.

Update the fixtures to represent an installed harness.

**Do not weaken or remove the production binary preflight.**

**Pass condition:** both tests prove:

1. provider session is persisted before result settlement;
2. valid submitted `COMPLETE` returns `COMPLETE`;
3. neither Pi nor OpenCode must actually be installed to run this unit test.

## 3. Separate control terminals visually

Audit/manager processes already have distinct synthetic process owners. Preserve that.

Do not make an organization's normal **Terminal** tab silently switch to the audit/manager terminal.

The organization terminal must remain its own terminal.

When control activity exists, expose its terminal separately, e.g. a small:

`Plan audit · Open terminal`

or

`Manager review · Open terminal`

control/surface.

The exact UI is up to you; do not redesign Inspector.

Also fix the current `"Plan audit running…"` overflow.

**Pass condition:** with an active plan audit:

- planner terminal opens the planner pane;
- control terminal opens the synthetic audit pane;
- both are independently interactable;
- neither is represented as the other;
- control labels remain inside their layout at narrow Inspector widths.

## 4. Close the test-verifiability bookkeeping gap

`AGENTS.md` requires bugs found outside test coverage to be recorded in `MISTAKES.md`, but `MISTAKES.md` is not currently present on GitHub.

Make the repository internally consistent: add/retain the agreed bug-note file and ensure every bug discovered during this pass receives a regression test before being marked resolved.

Do not create a documentation project.

## 5. Certification gate — one unchanged commit

After fixes, stop modifying source and certify that exact commit.

Required:

```bash
pytest -q turn/tests
npm test
npm run typecheck
npm run build
npm run check:contract
```

Tests that depend on optional external harness binaries must skip cleanly when those binaries are unavailable; unit tests must not fail merely because Pi/OpenCode/Claude/Codex is not installed unless binary availability is specifically what that test exercises.

Then repeatedly run the race-sensitive lifecycle/terminal tests at least **10 times**.

Finally perform one real Herdr smoke run that exercises:

`plan → audit → execution → nested manager review → acceptance`

Verify from the UI that:

- every AI process has a visible Herdr terminal;
- audit and manager terminals are distinct from organizational agents;
- no false lifecycle state appears;
- accepted agent submissions remain authoritative;
- no orphan Turn-owned panes remain after completion/cancel;
- no browser reload or manual state repair is needed.

No source changes are allowed during certification. If a real Turn bug is found, add its regression, fix it, and restart certification from the beginning.

## Done

P0–P1 is closed when:

1. the PTY output-loss regression is gone;
2. the native harness tests correctly test the current runtime contract;
3. control AI terminals are distinct and observable;
4. the complete automated gate is green;
5. one unchanged revision passes repeated lifecycle tests and the real Herdr smoke run without manual rescue.

If all five are true, **stop working on P0–P1 and move on.**