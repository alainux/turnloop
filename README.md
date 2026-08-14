# Turn

Turn is a local-first agentic development environment built around an adaptive
workgraph. A prompt becomes a visible decomposition; independent branches run in
parallel; dependencies, human inputs, reviews, artifacts, costs, and recovery
remain inspectable throughout the project.

The kernel is intentionally small: a versioned graph of `Node`, `Edge`, `Run`,
and `Artifact` records, a scheduler, and replaceable planner/worker adapters. The
IDE-like web UI and the headless CLI are clients of that same core.

## What the MVP includes

- Prompt-first project authoring and opening, a collapsible project explorer,
  graph canvas, inspector, real PTY-backed xterm terminal, light/dark themes,
  compact density, attachments, and responsive panels.
- A normalized semantic design system with a quiet wordmark, professional
  vendored Lucide icons, keyboard-aware tooltips, graph context menus, and
  contextual help. The product deliberately ships without decorative imagery.
- A deterministic horizontal dendrogram with orthogonal containment branches,
  dependency overlays, and tested parent/leaf geometry.
- Server-owned node/UI states with guarded transitions for run, step, pause,
  resume, cancel, retry, review, accept, reject, input, branch regeneration, and
  forks.
- Per-project execution policy: auto/step, sequential execution, inter-job delay,
  timeout, retry/backoff, choked-model retry, and manual/parent-verified review.
- Parent auto-verification reads real child evidence, may reject with feedback,
  and continues both parent and child sessions without discarding context.
- Codex, Claude Code, OpenCode, and Pi harness adapters with automatic local
  availability detection, editable model selectors, and model-dependent
  reasoning options; deterministic Echo and Shell adapters for development.
- Persistent agent session IDs so review feedback continues the same agent
  context and worktree.
- Agent-, branch-, and project-level token/cost reporting when a harness emits
  usage telemetry.
- A headless Python facade and `turn` CLI.
- Unit, API, runner-transition, browser end-to-end, generated-screenshot, and
  three-domain full-run persistence/log acceptance tests.

The visual and interaction contract is in [DESIGN.md](DESIGN.md). This README
is the sole product, architecture, scope, operation, and verification guide.

## Architecture and current boundary

The graph is the source of truth. `PlanResult` and `WorkerResult` are strict
domain contracts; the runner owns transitions; the store owns durable SQLite
state; UI and CLI are clients. A node owns intent and an `AgentConfig`, while
harness-specific flags remain inside replaceable planner/worker adapters:
`graph → node → agent → type/harness`.

The React client is strict TypeScript and mirrors the Python domain vocabulary.
It consumes server-projected `ui_state`, `allowed_actions`, review ownership,
and `generation_active`; it does not guess workflow state. A provider-neutral
terminal transport separates raw machine events used for schema parsing from
the ANSI presentation stream used by a Shadow DOM xterm. Codex final structured
results use the CLI's schema-constrained output file instead of leaking JSONL
into the human terminal. Code diffs are durable artifacts rendered in the
inspector.

Current scope includes local POSIX PTYs, local harness discovery, provider
sessions, parent verification with bounded correction rounds, model-dependent
reasoning controls, attachments, isolated git worktrees, recovery policies,
usage accounting, CLI/headless execution, and the tested web UI.

Future-ready seams—not implemented product claims—include remote/cloud terminal
transports, Windows ConPTY, authenticated remote service mode, custom type and
output registries, shared chats/A2A, composable validation/optimization loops,
promoted decomposition specs, and signed native bundles. The open graph,
artifact, registry, transport, and policy contracts are intentionally placed
for that work.

## Run locally

```bash
python -m pip install -e ".[dev]"
npm install
npm run build
playwright install chromium       # once, for browser tests
./scripts/run.sh                  # offline heuristic planner + Echo workers
```

Open <http://127.0.0.1:8000>. For real coding agents, select an installed
harness in the authoring surface or set `TURN_DEFAULT_EXECUTOR`.

## Verification

```bash
npm run typecheck
npm test
npm run build
pytest -q
```

The acceptance suite covers deterministic offline software/story/book runs,
browser authoring and inspection, PTY ANSI/input/resize/stall behavior,
state/review transitions, persisted logs/artifacts/diffs, and server security.
Model-backed demonstrations are intentionally separate from offline CI: they
prove installed-harness integration but are not deterministic quality scores.

## Headless CLI

```bash
turn doctor
turn create "Build an adaptive narrative engine" --harness codex --run
turn projects
turn graph PROJECT_UUID
turn run PROJECT_UUID
turn serve --port 8000
```

`turn run` is an explicit execution request, so it temporarily drives a project
even when it was authored in step mode. It exits when the graph settles, fails,
or requires human input.

## Tests

```bash
python -m pytest -q
npm test
```

Browser tests start isolated servers and exercise onboarding, graph menus,
inspector, real terminal transport, themes, responsive screenshots, and three
complete software/game/book workflows. They then inspect the SQLite graph,
runs, artifacts, and server log. In restricted sandboxes these tests skip when
local listener sockets are prohibited; run them in a normal local shell for the
full check.

## Project layout

```text
turn/core.py                 headless application facade
turn/domain/                 schemas and pure UI-state projection
turn/db/                     SQLAlchemy persistence and migrations
turn/graph/                  pure graph evaluation
turn/runner/                 scheduling, transitions, recovery, events
turn/workers/                planner and harness adapters
turn/server/                 REST, SSE, and static UI boundary
turn/tests/                  unit, integration, API, and browser tests
ui/                          dependency-free IDE shell and UI state reducer
```

## Design constraint

The store never guesses planner intent. It validates keys, references, and graph
acyclicity, then preserves valid objectives and topology exactly—without hidden
child caps, semantic deduplication, title truncation, or domain-specific nodes.
