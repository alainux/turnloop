# Full-run acceptance contract

`turn/tests/test_full_run_acceptance.py` drives three projects through the real
browser authoring flow using the offline heuristic planner and deterministic
echo harness:

1. a modular command-line software project;
2. a branching story-driven game;
3. a book with independently authored chapters.

Each run must visibly reach a human clarification, accept the answer through
the inspector, resume without replacing the project or agent context, and end
with every graph node complete. The test then opens the root run history from
the node context menu.

After the browser closes, the acceptance test reads SQLite directly and proves
for each project:

- one concise root name and four unique compact child objectives;
- the full authoritative project prompt retained on the root and in every
  child execution prompt;
- four containment edges and three dependency edges;
- one satisfied clarification with a preserved `user_input` artifact;
- five completed run records with non-empty summaries and logs;
- four result artifacts whose first line exactly identifies the compact child
  objective that produced it;
- no server traceback or HTTP 500 response.

This is a deterministic product-path acceptance check, not a claim that the
echo harness measures creative or coding quality from a production model.
Model-backed quality evaluation remains a separate evaluation concern so CI
does not require network access or spend tokens.

## Model-backed human-tinkering demonstration (2026-08-13)

Two additional disposable projects were exercised through the real UI against
installed Codex, OpenCode, and Pi harnesses. Their database and repositories
live under `/private/tmp/turnloop-human-demo.db` and
`/private/tmp/turnloop-human-projects/`; they are evidence from this manual run,
not checked-in fixtures.

The first run created a small browser CYOA in manual OpenCode mode, discarded
its first decomposition, regenerated nested planners with Codex
`gpt-5.6-luna`/high, forked a deep story branch, edited a deep implementer, and
changed three leaves to Pi `freeinference/deepseek-v4-flash`/high. Sequential
launches occurred at `12:31:48`, `12:33:48`, and `12:35:48` UTC, proving the
configured 120-second spacing. A page-shell revision was rejected with CSP
feedback, resumed in the same Pi session, inspected, accepted, then the graph
was drained with automatic acceptance. All 11 active nodes finished complete;
all 10 non-root nodes are merge-accepted, and every active node has
graph-inspection audit evidence. Independent review caught that the original
integrator had accepted an early progress-shaped result and left story/runtime
content inconsistent. The same integrator session was rejected with that
feedback and performed a glue-only correction (33 insertions/41 deletions
across the existing README, engine, and shell). Final browser replay proved
both authoritative endings: signal 3 reaches “The Light Comes Home” and signal
0 reaches “The Unanswered Tide.”

The second run created a deterministic constellation generator, cancelled its
planner in flight, revised and regenerated the graph, edited an implementer,
cancelled an incorrectly chosen documentation job, and exercised parser,
retry, model-choke, and harness-switch recovery. Browser QA caught a URL-seed
regression after automatic acceptance; a corrective fork produced a smoke test
and fixed the defect. Two fresh `?seed=aurora` loads now render the same name
and legend, and `node smoke-test.js` prints `PASS`. All six active nodes finish
complete and have graph-inspection evidence.

Issues found by this deliberately adversarial run became regression contracts:
provider-aware planners, inherited/cascaded agent configuration, absolute graph
tool runtime, Pi session/usage decoding, common worktree artifacts, strict
structured-result handling, artifact shorthand normalization, declared-file
existence checks, harness-switch session reset, running-state projection, and
named `turn-result`/`turn-plan` parsing. Independent review added bare
schema-plan recognition, final-result selection, material-change enforcement,
domain-direction edge parsing, core-level cross-harness session reset, and a
complete-node automatic-acceptance drain.

The final lifecycle closure also re-ships accepted corrections made after a
root first becomes complete and verifies redundant worktree/branch cleanup
before persisting acceptance. In the saved adventure, the corrected work
commit is now an ancestor of the checked-out `main` branch, only `main` and the
project's reusable root working branch remain, and `.turn/worktrees` is empty.

## Non-trivial WebGL game demonstration (2026-08-13)

`Aether Run` was authored and executed as a real model-backed workgraph in
`/private/tmp/turnloop-3d-projects/proj-1591d412`, using Codex
`gpt-5.6-luna`/high. The delivered application is a dependency-free WebGL2
hovercraft time trial with a deterministic seeded simulation, ten ordered
energy gates, collision/boost/pause/restart flows, accessible UI and settings,
renderer fallbacks, a replay/debug smoke API, and persisted best time.

The live run deliberately exercised adverse human behavior rather than a clean
happy path: project policy changes, repeated cancel/retry cycles, a damaged
provider session reset from the inspector, a rejected branch, same-node review
feedback, planner regeneration, redundant-branch cancellation, manual review
override after direct evidence contradicted a stale model judgment, and final
base-branch shipping. The parent verifier found a real integration defect:
control state emitted `move`/`start` commands that the simulation contract
rejected. It returned `BLOCK` with focused feedback; the same child node kept
its worktree and conversation until a deliberate human session reset was used
after the provider became choked. A later parent-verifier run accepted the
corrected implementation after inspecting the merged worktree and focused
test. Separate verifier session IDs preserve reviewer context without
overwriting the parent's planning conversation.

The adversarial run exposed and closed additional lifecycle regressions:
nested work must merge into the parent worktree rather than switching the
repository root; plan children inherit agent configuration but never a parent
session ID; read-only verification cannot reset or merge its target worktree;
old completion callbacks cannot remove a newer single-flight verifier;
container acceptance awaits descendant tasks; accepted state dominates late
worker results and restart projections; cancelled descendants are not revived
as accepted work; and non-interactive harnesses close stdin explicitly. Final
independent review also generalized read-only verification to Claude,
OpenCode, and Pi; terminalizes orphaned RUNNING run records after process
restart; canonicalizes every accepted graph projection to COMPLETE; and keeps
footer status derived from the graph across unrelated settings commands.

Final evidence:

- the generated game suite passes 15/15, including deterministic replay,
  progression, collisions, pause/restart, outcomes, renderer math, ten gates,
  smoke divergence, and the repaired input-command contract;
- browser play-testing starts the WebGL course, renders the 3D world and HUD,
  clears a gate, pauses, and resumes without console errors;
- every successful active agent has a graph-inspection audit record (14/14 at
  the evidence snapshot; failed/no-output attempts are excluded);
- the integrator changed only the HTML/package entry points, one two-line
  course adapter correction, and `src/game-shell.js`, whose imports and code
  compose the renderer, simulation, controls, audio, UI and debug modules;
- the corrected `turn-1591d412` work branch is an ancestor of checked-out
  `master`, with zero persisted RUNNING runs, zero accepted/cancelled
  contradictions, zero review gates and zero active non-terminal nodes;
- the Turn suite passes 76 tests plus 13 JavaScript tests; the explicit browser
  E2E/full-run pair passes 2/2; normal sandbox execution skips only those two
  listener-bound browser cases;
- the product authoring UI contains no `<img>` element, logo asset, hero image,
  or decorative animation. The titlebar uses a typographic wordmark and all
  functional icons are vendored Lucide assets with transparent backgrounds.

## Final offline tinkering run (Runbook Forge Final)

The final isolated run used the UI directly against the heuristic planner and
Echo workers. It began in step mode with sequential execution, decomposed the
project, edited an implementer objective/instructions in place, created a
root-level alternative planner tree, supplied human clarification to both
trees, switched to auto-run, rejected the fork with focused regression
feedback, re-planned it, supplied the new clarification, and accepted the
revised result. Reloading and reopening the project showed a durable complete
graph rather than a transient projection.

Persisted evidence in `/private/tmp/turnloop-final-demo2.db`:

- 14 nodes: 10 COMPLETE and 4 intentionally CANCELLED superseded revisions;
- 15 COMPLETE runs, every one with non-empty logs, and zero RUNNING runs;
- one visible fork in the same project, three persisted `user_input` artifacts,
  and planner revision 2 containing the reviewer feedback;
- zero unresolved reviews and final UI state `14/14 resolved` / `Workgraph
  complete`;
- the live terminal endpoint completed a real WebSocket upgrade and rendered
  the Echo transcript in xterm.

The demo uncovered a root-fork reachability bug and misleading manual-review
copy. Root forks now remain top-level alternative branches inside their source
project, and review labels derive from the explicit owner. Both contracts have
regressions. Final automated evidence is 86 passing Python tests (2 expected
listener-bound sandbox skips), 14 passing JavaScript tests, and an explicit
Chromium/full-acceptance pair passing 2/2.
