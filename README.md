# Turn

Turn is a local-first agentic development environment built around an adaptive
workgraph. A prompt becomes a visible top-down decomposition; independent
domain branches run in parallel; ordinary executor nodes recombine their
outputs from left to right; and human inputs, artifacts, costs, and recovery
remain inspectable throughout the project.

The kernel is intentionally small: a versioned graph of `Node`, `Edge`, `Run`,
and `Artifact` records, a scheduler, and replaceable planner/worker adapters. The
IDE-like web UI and the headless CLI are clients of that same core.

## What Turn currently includes

This section describes Turn's currently implemented capabilities. It is not a
reduced delivery bar for projects created through Turn: unless a user asks
for an MVP, POC, prototype, or other limited slice, project plans target the
complete requested product.

- Prompt-first project authoring and opening, a collapsible project explorer,
  graph canvas, inspector, real PTY-backed xterm terminal, light/dark themes,
  compact density, attachments, and responsive panels.
- A normalized semantic design system with a quiet wordmark, professional
  vendored Lucide icons, keyboard-aware tooltips, graph context menus, and
  contextual help. The product deliberately ships without decorative imagery.
- A deterministic left-to-right dendrogram: containment and genuine workflow
  stages share the same grey orthogonal edges, with parallel domain branches
  and explicit integration points visible in the graph.
- Server-owned node/UI states with guarded transitions for run, step, pause,
  resume, cancel, retry, input, branch regeneration, and
  forks.
- Per-project execution policy: auto/step, parallel dispatch, inter-job delay,
  timeout, retry/backoff, and choked-model retry.
- Codex, Claude Code, OpenCode, and Pi harness adapters with automatic local
  availability detection, editable model selectors, and model-dependent
  reasoning options. Deterministic Echo and heuristic planning exist only in
  tests and are never exposed by the served application.
- Persistent agent session IDs so reruns can continue the same agent context
  and project directory.
- Agent-, branch-, and project-level token/cost reporting when a harness emits
  usage telemetry.
- A headless Python facade and `turn` CLI.
- Unit, API, runner-transition, browser end-to-end, generated-screenshot, and
  three-domain full-run persistence/log acceptance tests.

The visual and interaction contract is in [DESIGN.md](DESIGN.md). This README
is the sole product, architecture, scope, operation, and verification guide.

## Architecture and current boundary

The graph is the source of truth. `PlanResult` and `WorkerResult` are strict
domain contracts; the runner owns transitions; the store owns durable local
project files; UI and CLI are clients. A node owns intent and an `AgentConfig`, while
harness-specific flags remain inside replaceable planner/worker adapters:
`graph → node → agent → type/harness`.

For broad requests, a `PlanResult` also carries graph-owned architectural
metadata: an executive summary, approach and strategy, typed sections,
decisions, risks, acceptance criteria, and optional diagrams. The document view
renders that metadata alongside the dependency graph, and every worker receives
the same root/branch metadata through its graph context. It is not a second
document store or a filesystem handoff protocol.

The React client is strict TypeScript and mirrors the Python domain vocabulary.
  It consumes server-projected `ui_state`, `allowed_actions`, and
and `generation_active`; it does not guess workflow state. A provider-neutral
terminal transport separates raw machine events used for schema parsing from
the ANSI presentation stream used by a Shadow DOM xterm. Codex final structured
results are submitted through the Turn CLI instead of leaking JSONL into the
human terminal. Code diffs are durable artifacts rendered in the
inspector.

Current scope includes local POSIX PTYs, local harness discovery, provider
sessions, model-dependent
reasoning controls, attachments, direct filesystem project execution, recovery
policies, usage accounting, CLI/headless execution, and the tested web UI.

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
herdr                                  # start/attach the default Herdr service
./scripts/run.sh                       # real Codex planner + workers
```

Turn requires Herdr for project terminals, but the server can run outside the
Herdr UI. By default Turn uses the default Herdr service, so each project
workspace is visible directly when you type `herdr`. Set `HERDR_SESSION` only
when intentionally using a separately named Herdr service. Each project
becomes one Herdr workspace, and each node gets a durable pane inside that
workspace; browser connections are temporary control streams into those panes.

Open <http://127.0.0.1:8000>. For real coding agents, select an installed
harness in the authoring surface or set `TURN_DEFAULT_EXECUTOR`.

## Verification

```bash
npm run typecheck
npm test
npm run build
pytest -q
```

The acceptance suite covers test-only deterministic software/story/book runs,
browser authoring and inspection, PTY ANSI/input/resize/stall behavior,
state transitions, persisted logs/artifacts/diffs, and server security.
Model-backed demonstrations are intentionally separate from those tests: they
prove installed-harness integration but are not deterministic quality scores.

## Headless CLI

```bash
turn doctor
turn create "Build an adaptive narrative engine" --harness codex --run
turn projects
turn graph PROJECT_UUID --tree
turn run PROJECT_UUID
turn serve --port 8000
```

When run from a project directory, `turn create` uses that current directory
as the project directory. The UI/server default is the repo-local `projects/`
directory; override it explicitly with `TURN_PROJECTS_DIR` when needed.

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
complete software/game/book workflows. They then inspect each project's local
state file, runs, artifacts, and server log. In restricted sandboxes these tests skip when
local listener sockets are prohibited; run them in a normal local shell for the
full check.

## Project layout

```text
turn/core.py                 headless application facade
turn/domain/                 schemas and pure UI-state projection
turn/db/                     local project-file persistence
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
