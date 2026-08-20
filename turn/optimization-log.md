# Turn optimization log

This local log records measured optimization iterations. A qualifying project
counts toward the three-project streak only when its objective and validation
pass, required telemetry is complete, no critical Turn/UI/harness issue remains,
and applicable qualitative dimensions score at least 4/5.

## 2026-08-20 — Cancellation-state integrity baseline

- **Scope:** Turn's process-backed workflow laboratory and the local production
  build. This is a framework-quality iteration, not a qualifying user project.
- **Hypothesis:** A user Stop can race terminal shutdown; a late provider error
  or result may overwrite `CANCELLED`, leaving the UI and recorded run outcome
  inconsistent.
- **Baseline evidence:** the full suite had 331 passing tests, 4 skips, and one
  failure in `test_mock_process_workflow_lab_covers_core_state_machine_paths`.
  The stopped process did not settle as `CANCELLED` within ten seconds. The
  existing Luna demo also had an invalid-graph cancellation with zero useful
  agent-action telemetry, so it could not be scored as a real-project run.
- **Focused change:** Stop now persists the cancellation decision before
  terminal shutdown. Planner and worker returns/errors recognize that terminal
  state, keep the run `CANCELLED`, and discard late results rather than applying
  graph or artifact mutations. Failed-state handling also uses a guarded
  transition so it cannot overwrite cancellation.
- **Verification:** Added a deterministic regression test where a provider
  returns success while Stop is in progress. The cancellation tests and mock
  process workflow passed (3 passed). Full suite: **333 passed, 4 skipped** in
  140.88 seconds. Production UI build: passed.
- **Resulting assessment:** cancellation semantics 4/5; recovery 4/5;
  verification 4/5; architecture adherence 4/5. Live-agent telemetry,
  planner quality, UI/UX coherence, and final software quality remain
  unscored because no complete representative project ran in this iteration.
- **Qualifying streak:** 0/3.

### Follow-up: telemetry v11/v12 and reusable browser QA contract

- **Telemetry correction:** post-change verification had been falsely zero
  because generic word matching treated test-related text inside CLI payloads
  as test commands and left an old post-test edit active. Version 11 now
  recognizes actual shell test/lint/build/typecheck commands, carries command
  exit status, and lets the latest successful test certify the latest edit.
  The live ShiftRelay panel moved from the false 0% to 75%, exposing a second
  denominator issue: setup/planner documentation and graph changes were being
  graded as product edits. Version 12 limits the rate to executor/integrator
  delivery changes, while still counting an implementer's missing or failed
  test as a failure.
- **Live UI evidence:** after a real server restart and UI reload, ShiftRelay
  shows 18 skills loaded / 0 opaque native observations, 132 tool calls, 100%
  verification after delivery change, 0 harness failures, 1 approve / 2
  evidence-backed rejects, 308 actions, and 458,258 tokens (440,832 cached).
  The 75% value was not hidden; its evidence led directly to the scoped v12
  correction.
- **Future-workflow correction:** planning and verification capabilities now
  require a verifier to inspect its actual browser controls, prefer the
  in-app Browser when controllable, and use Chrome control when it is the
  available local surface. A missing individual binding alone cannot reject
  the delivered product. This removes the manual verifier-prompt intervention
  that ShiftRelay required.
- **Acceptance reliability:** the browser three-project acceptance test had
  treated an unresolved async JavaScript predicate as truthy, which could
  inspect the one-node pre-plan graph. It now polls the returned graph value;
  the real three-project browser flow passed.
- **Verification:** focused metrics/API and browser acceptance coverage passed;
  full suite: **344 passed** (one pre-existing third-party deprecation
  warning); production UI build passed.
- **Assessment update:** telemetry truthfulness 4/5 and browser-verifier
  resilience 4/5. ShiftRelay remains uncounted because its 2/5 efficiency
  score is unchanged; its learned contracts should improve the next run.
- **Qualifying streak:** 0/3.
- **Remaining weakness:** no new complete Luna-managed software workflow has
  yet demonstrated all role metrics, capability use, harness logs, verification,
  and human UI paths together.
- **Best next direction:** create and run a small local Micro-SaaS through the
  Turn UI using GPT-5.6-Luna, then inspect its graph, terminal/harness logs,
  quality dashboard, artifacts, and local application behavior before scoring it.

## 2026-08-20 — Native Codex telemetry fidelity

- **Scope:** the completed LedgerLoom Micro-SaaS workflow in the Turn UI. Its
  graph and final product are strong, but its Quality panel reported no
  document or skill evidence even though the native Codex sessions inspected
  README/AGENTS and ran role capabilities.
- **Root cause:** native Codex notify rollouts describe an `exec` tool call,
  with its actual shell command nested in structured arguments. The reducer
  only inspected the wrapper name, so it lost command-level document and test
  evidence. Late native sidecar facts could also arrive after the result
  handoff, too late for the prior one-time ordering calculation.
- **Focused change:** decode structured `exec` command input; derive document
  reads from referenced Markdown/text files; preserve test/verification command
  classification; define documentation ordering against material actions
  (writes, plan applications, and verification decisions) rather than harmless
  discovery; and update completed projections when late sidecar facts arrive.
  Real-harness launches now also record the capability skill paths that Turn
  has installed and verified. The UI labels those separately from runtime
  observed skill-access events, so it does not claim an opaque native skill
  invocation was directly observed.
- **Verification:** targeted behavior and runner coverage: **76 passed**.
  Full suite after relocating this log outside the UI's intentionally clean
  product-documents directory: **335 passed, 4 skipped** in 143.74 seconds.
  Production UI build: passed.
- **Resulting assessment:** telemetry correctness 4/5; recovery 5/5;
  verification 4/5; UI truthfulness 4/5. LedgerLoom remains uncounted because
  the historical launch records predate the verified skill-load field; a fresh
  project is required for a complete, comparable metrics record.
- **Qualifying streak:** 0/3.
- **Best next direction:** use the Turn UI to create a meaningfully different
  native/multi-platform project, then inspect the new live Quality panel to
  confirm its skill-load, document, and verification metrics before scoring it.

### Follow-up: retained-event projection migration

- Rebuilding the older LedgerLoom log initially exposed a second fidelity gap:
  the prior normalized records contained the raw `exec` arguments but no
  derived command or file-write event. The projection migration now derives
  Markdown reads, nested `tools.apply_patch` writes, and subsequent test
  commands directly from that retained structured input. The UI now shows
  LedgerLoom document-order evidence (50%) rather than zero. Historical skill
  loads remain correctly unknown because those launch records predate the
  verified load field; they are not backfilled by guesswork.
- **Verification:** focused API/metrics/UI suites: **94 passed**; full suite:
  **336 passed, 4 skipped** in 143.91 seconds; production UI build: passed.
- **Remaining weakness:** Uvicorn waits indefinitely for a persistent terminal
  WebSocket during daemon shutdown, so a maintenance restart needs a targeted
  forced stop after the graceful signal. This does not affect live work, but it
  should be addressed separately as a daemon lifecycle improvement.

## 2026-08-20 — ShiftRelay PWA, verifier browser fallback, and retained follow-ups

- **Scope:** a new, UI-created local-first restaurant shift-handoff PWA at
  `projects/proj-9474bb71`, run entirely with Codex / GPT-5.6-Luna / medium.
  The five-node workflow was planner → core executor → PWA executor →
  integrator → verifier.
- **Product result:** the approved app supports service areas, prioritized
  owned handoffs, resolved/open and area/priority filtering, IndexedDB-backed
  persistence, a local manifest and service worker, accessible keyboard
  behavior, empty/error/offline-ready states, and a no-backend README. The
  final verifier reported all 10 Node tests passing. Independent parent Chrome
  QA also created, filtered, resolved, and reloaded a handoff; at 390px it had
  no horizontal overflow and no console warnings/errors.
- **Verifier learning:** the initial verifier rejected SVG-only manifest icons
  and absent live browser evidence. The integrator added 192/512 PNG assets,
  precache coverage, correct `image/png` serving, and a Chrome journey. A
  second verifier rejection exposed a contract bug: it was hard-wired to the
  unavailable in-app Browser even though Chrome was available. The verifier
  prompt was corrected in the Turn UI to prefer that surface and fall back to
  Chrome. Its final independent Chrome review approved the app, including
  desktop create/assign/filter/resolve/reload persistence, focus outline,
  duplicate-area recovery, mobile 390×844 layout, offline readiness, and zero
  browser errors. Install UI and network-offline emulation were unavailable and
  recorded honestly as limitations.
- **Server learning:** a retained rejection follow-up kept its reconnect
  controller alive after a CLI handoff. That suppressed a later rejection
  prompt and left the UI displaying a ghost `preparing` integration node with
  no new harness run. Result and verification handoffs now cancel only that
  controller, while preserving the durable provider session. A regression test
  proves a subsequent verifier rejection starts a second retained follow-up.
- **Telemetry learning:** Codex installs skills through the project-native
  `.agents/skills` surface, so launch telemetry previously emitted an empty
  path list even though the role capabilities were installed. Launches now
  report actual skill names; version-10 metrics reconstruct the mandatory
  role skill set for pre-fix Codex records. This avoids falsely scoring native
  agents as `0` skills loaded while keeping runtime skill observation separate.
- **Measured evidence before the version-10 Quality-panel rebuild:** 308
  actions, 458,258 tokens (440,832 cached), 20 minutes, no harness failures,
  28 graph changes, and verifier outcomes 1 approve / 2 reject. The retries
  were useful acceptance corrections, but the iteration was costly because the
  browser contract and retained-follow-up issue were discovered live.
- **Verification:** focused regression coverage: **43 passed**; full suite:
  **337 passed, 4 skipped** in 147.36 seconds; production UI build: passed.
- **Assessment:** product quality 5/5; independent verification 5/5;
  browser/mobile accessibility 4/5; workflow resilience 4/5 after the fix;
  telemetry truthfulness 4/5 after version-10 reconstruction; efficiency 2/5
  because the two initial rejections and ghost continuation consumed excess
  time/tokens. It is therefore **not yet counted** toward the qualifying
  three-project streak: reopen the UI to verify the version-10 Quality panel,
  then rerun a comparable project with the browser fallback contract available
  at planning time and without rejection-loop waste.
- **Qualifying streak:** 0/3.

## 2026-08-20 — Circuit Grove release review and organization-scale planning

- **Scope:** Circuit Grove was created and run through the Turn UI with Codex /
  GPT-5.6-Luna / medium. It is a finished local browser circuit-puzzle game,
  independently approved after real desktop, narrow viewport, keyboard,
  persistence, accessibility, and console-health checks.
- **Product evidence:** the final verifier ran 7/7 tests, solved a live level,
  exercised undo/restart/progression and reload persistence, and observed a
  390×844 layout with a 346px board, 42px minimum targets, no horizontal
  overflow, live feedback, motion persistence, and no console warnings or
  errors. A separate parent UI check also rotated a real tile and observed the
  move/status update.
- **What did not qualify:** its five-node plan concentrated the core game in
  one executor and serialized generic hardening before integration. It did not
  form independent design, engine, content, presentation/assets, and sound
  production lanes. It also incurred a real retained-session launch failure
  after the first verifier rejection (`Codex exited 1`, zero actions/tokens),
  two verifier passes, 34.5 minutes, and 1,280,398 recorded input/cache
  tokens. The Quality panel honestly retained a 50% post-change verification
  rate and one harness failure. This is a strong product result but a 1/5
  workflow-efficiency result, so it is **not counted**.
- **Planning correction:** the release-scale game rule now requires a material
  pre-production/design contract, genuine parallel engine/content/presentation
  lanes, explicit source/artifact ownership, integration, and independent QA.
  More importantly, the generic planner and root-setup contracts now make a
  complete usable release the default for ordinary product language. Vague
  wording cannot justify a one-screen POC or one generalist executor. If a
  missing decision materially changes the product, planners must create a
  short planner clarification boundary with one to three precise UI inputs;
  otherwise they document reversible decisions and organize the needed real
  disciplines.
- **Live proof of the correction:** the next UI-created project, *The Glass
  Archive*, produced an eight-node organization: a creative/game-design
  contract, then parallel engine/state, three-act narrative/world/puzzles,
  visual UX/assets, and adaptive-audio lanes, followed by release integration
  and independently browser-driven verification. The graph was accepted in
  the visible Turn terminal with every agent using GPT-5.6-Luna.
- **Verification:** planner-capability contract tests passed (22 passed), the
  full-suite failure cache was clean after its run, and `npm run build` passed.
- **Qualifying streak:** 0/3. The Glass Archive remains in progress and must
  be scored only after its real UI workflow and final release checks complete.

## 2026-08-20 — Glass Archive rejection loop and release-lifecycle correction

- **UI evidence:** Auto correctly promoted the verifier after the first
  repaired integration (`BLOCKED → RUNNABLE → RUNNING`), and its independent
  browser pass found a real Understack completion defect rather than approving
  the prior release evidence. The integrator then repaired the missing seal,
  witnesses lens, evidence effect, and deterministic full-route coverage.
- **Reliability defect:** after that corrected submission, Auto again promoted
  the verifier but its retained Codex resume exited 1 before any token usage.
  Session discovery had overwritten the verifier's already-known resumed
  session with another active worker's rollout in the shared project cwd. The
  interactive runner now accepts a known session identity and skips discovery
  for resumes; all native harness call sites pass it. A regression test creates
  a competing rollout and proves the resumed worker never adopts it. Focused
  interactive, verification, and transition coverage: **112 passed**.
- **Planning correction:** product-scale planner and root-setup guidance now
  require a right-sized real release lifecycle before nodes are chosen:
  discovery/product definition, material technical-risk reduction, a
  vertical-slice review, independent production lanes, repeated integration,
  QA/polish, and release readiness where justified. The planning document must
  name owners, gates, parallel lanes, and convergence/review points; it may
  omit or combine a stage only with a product-specific rationale. Capability
  guidance tests: **22 passed**.
- **Auto-flow proof:** after the session-identity repair was deployed, the
  eighth integrator run completed and Auto promoted verifier attempt five
  without a UI action. It retained verifier session
  `01a01ef0-e9bd-7313-99c9-530e7e1f5f1a`, rather than adopting the integrator
  session. The verifier independently approved the full route, two earned
  endings, replay, persistence, responsive/keyboard/settings behavior, clean
  console, and the local-only runtime. A separate parent browser spot-check
  opened the published local entry point, collected the starting inventory,
  and entered Bellwether through the visible UI.
- **Live Quality evidence:** 745 actions, 6,136,460 tokens (5,905,280 cached),
  45.3 minutes, 19 recorded runs, 1 approval / 3 corrective rejections,
  70% documentation-before-material-action, 40% verify-after-change, 58 graph
  changes, and 4 harness failures. Role telemetry records 40 installed skills;
  opaque native-harness skill-use observation remains correctly zero rather
  than being fabricated.
- **Assessment:** organization fit 4/5; product and visual/narrative quality
  4/5; architecture and local-runtime adherence 4/5; independent verification
  5/5; recovery after the final repair 4/5; planning/parallelization 3/5
  because this graph predated the stronger lifecycle policy; efficiency 1/5.
  The rejected routes caught genuine product defects, and the session takeover
  was fixed, but the historical harness failures and low change-verification
  rate mean this project is **not counted** toward the streak.
- **Qualifying streak:** 0/3.
- **Best next direction:** create the next meaningfully different real
  software project from the UI only after the lifecycle policy is active, and
  require its planner to express discovery, product definition, technical risk
  reduction, vertical-slice gate, independent production lanes, integration,
  beta/QA, and release readiness in a right-sized graph. It must reach the
  approval with no harness failures and materially higher verification
  coverage.
