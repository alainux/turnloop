<p align="center">
  <img src="docs/assets/banner.png" alt="Turn adaptive workgraph banner">
</p>

<h1 align="center">Turn</h1>

<p align="center">
  <strong>Adaptive development, made visible.</strong><br>
  Turn turns an outcome into an inspectable workgraph, then helps you execute it one deliberate step at a time.
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11 or newer"></a>
  <a href="turn/server"><img src="https://img.shields.io/badge/FastAPI-REST%20%2B%20SSE-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI REST and SSE"></a>
  <a href="ui"><img src="https://img.shields.io/badge/React-19-61DAFB?style=flat-square&logo=react&logoColor=111827" alt="React 19"></a>
  <a href="turn/tests"><img src="https://img.shields.io/badge/tested%20with-pytest-0A9EDC?style=flat-square&logo=pytest&logoColor=white" alt="Tested with pytest"></a>
  <a href="https://github.com/alainux/turnloop"><img src="https://img.shields.io/github/stars/alainux/turnloop?style=flat-square&label=GitHub%20stars" alt="GitHub stars"></a>
</p>

<p align="center">
  <a href="#why-turn">Why Turn</a> ·
  <a href="#product-surface">Product surface</a> ·
  <a href="#screenshots">Screenshots</a> ·
  <a href="#run-locally">Run locally</a> ·
  <a href="#verification">Verification</a>
</p>

## Why Turn

Most agent workflows begin with a prompt and quickly become a stream of opaque
tool calls. Turn keeps the plan, execution, and evidence in one visible control
surface:

- **Start with intent.** Describe the outcome instead of manually inventing a task list.
- **See the decomposition.** Turn renders containment, sequencing, fan-out, fan-in, and integration points as a workgraph.
- **Run with control.** Choose step-by-step or auto-run execution, pause between stages, retry failures, and provide human input when needed.
- **Inspect the work.** Open the document view, terminal, logs, artifacts, costs, and durable agent sessions from the same project.
- **Keep ownership local.** Project state lives on disk, while planner and worker adapters keep harness-specific behavior replaceable.

Turn is designed for complex software, games, books, and other outcomes where
the path matters as much as the final artifact.

## Product surface

| Surface | What it gives you |
| --- | --- |
| Project authoring | Prompt-first creation, project explorer, attachments, and working-directory selection |
| Workgraph | A deterministic left-to-right graph with explicit sequencing, fan-out, and fan-in |
| Execution | Auto/step policies, retries, timeouts, cancellation, recovery, and human-input gates |
| Organizations | Durable charters, independent plan audits, recursive manager review, and replan boundaries |
| Units of work | Priority-ordered tickets with acceptance criteria, dependencies, evidence, and typed handoffs |
| Capacity | Project/global parallel limits, run/token/cost budgets, and optional isolated Git worktrees |
| Agent workspace | Durable PTY-backed terminals, reconnectable sessions, and provider-neutral transport |
| Evidence | Document view, logs, artifacts, diffs, token/cost usage, and server-projected UI state |
| Harnesses | Codex, Claude Code, OpenCode, and Pi adapters with local availability detection |

The process-level Mock harness and heuristic planning are test-only fixtures.
For a repeatable local workflow laboratory, set `TURN_TEST_MODE=1`, choose
`TURN_PLANNER=mock` and `TURN_DEFAULT_EXECUTOR=mock`, and add
`TURN_MOCK_WORKFLOWS=1`; the server then seeds the rejection, expansion, rerun,
failure, input, and cancellation projects once in the configured data
directory. The Mock provider is not advertised or accepted by production
runtime configuration. The seeded lab includes the rejection, expansion, rerun,
failure, input, cancellation, and schedule scenarios; the trigger loop
E2E also exercises a manually started loop with configured event data.

### Current scope

Turn currently provides a local-first workgraph for planning, running, and
inspecting agent work. The shipped surface includes explicit step/auto
execution, durable project state, reconnectable terminals, artifact and usage
inspection, verifier decisions, and provider-specific harness adapters for
Codex, Claude Code, OpenCode, and Pi.

### Future-ready seams

The storage, graph, runner, terminal, and harness boundaries are independent so
new providers, scheduling policies, and evidence types can be added without
making the UI own orchestration state. The graph also models derived flow
edges, such as review rejection returns, separately from the durable workflow
topology.

For a broad objective, the root is a persistent organization boundary rather
than a one-shot checklist. Its charter records the desired outcome, deliverables,
acceptance criteria, constraints, quality and decomposition policy, and budget.
The runtime audits each proposed composition before applying it, materializes
work items and handoffs, schedules only within capacity, and reviews settled
frontiers through:

```text
PLAN → EXECUTE FRONTIER → OBSERVE → REVIEW → REPLAN → … → ACCEPT CHARTER
```

The `Store` owns this state on disk; `OrganizationManager` owns the review
decision; `Scheduler` owns reservation and budget enforcement; and the REST/CLI
surfaces expose the same records for humans and agents.

## Screenshots

The screenshots below show the authoring surface, harness selection, graph and
terminal inspection, document view, and workspace preferences.

<table>
  <tr>
    <td><img src="docs/assets/screenshot1.png" alt="Turn project authoring screen"></td>
    <td><img src="docs/assets/screenshot2.png" alt="Turn harness selection menu"></td>
  </tr>
  <tr>
    <td><img src="docs/assets/screenshot3.png" alt="Turn workgraph and terminal inspector"></td>
    <td><img src="docs/assets/screenshot4.png" alt="Turn document view"></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/assets/screenshot5.png" alt="Turn light theme and workspace preferences"></td>
  </tr>
</table>

## Architecture

The graph is the source of truth. `PlanResult` and `WorkerResult` are strict
domain contracts; the runner owns transitions; the store owns durable local
project files; the UI and CLI are clients.

```text
prompt
  │
  ▼
planner ──▶ PlanResult ──▶ workgraph ──▶ runner ──▶ harness adapter
                                      │              ├─ Codex
                                      │              ├─ Claude Code
                                      │              ├─ OpenCode
                                      │              └─ Pi
                                      ▼
                              logs · artifacts · diffs · usage
```

For broad requests, a plan can carry an executive summary, approach, typed
sections, decisions, risks, acceptance criteria, and optional diagrams. The
document view renders that metadata as a live project document: nested work is
collapsible, workflow source graphs remain explorable through links, and
generated Markdown, text, and image artifacts open from the same reader. It
refreshes from the current graph as agents append or replace work, and worker
nodes receive the same graph-owned context.

The main boundaries are deliberately small:

- `turn/domain/` — schemas, state transitions, and UI-state projection
- `turn/db/` — local project-file persistence
- `turn/graph/` — pure graph evaluation
- `turn/runner/` — scheduling, recovery, events, and terminal lifecycle
- `turn/workers/` — planner, worker, and harness adapters
- `turn/server/` — REST, SSE, security, and static UI boundary
- `ui/` — strict TypeScript client and interaction state

## Run locally

### Prerequisites

- Python 3.11+
- Node.js and npm
- A running Herdr daemon for project terminals
- At least one supported coding harness installed locally for real runs

Herdr is an external, user-owned daemon. Turn connects to it through the
`herdr` client CLI and never starts, stops, or restarts the daemon. Verify the
existing service with `herdr status server` before starting Turn; do not run
`herdr server` as part of the Turn launch procedure.

**CAUTION FOR AI OPERATORS: HERDR CANNOT BE LAUNCHED INSIDE SUBPROCESSES OR
FROM HERDR ITSELF. DO NOT TRY TO LAUNCH HERDR. REQUEST/USE THE ALREADY-RUNNING
HERDR DAEMON.** If it is unavailable, Turn fails visibly and stops; it does
not launch, restart, or replace Herdr.

### Install and start

```bash
python -m pip install -e ".[dev]"
npm install
npm run build
./scripts/run.sh         # start Turn at http://127.0.0.1:8000
```

Turn uses the repo-local `projects/` directory by default. Override it when
needed:

```bash
TURN_PROJECTS_DIR=/path/to/projects ./scripts/run.sh
```

Open <http://127.0.0.1:8000>, describe an outcome, choose an installed harness,
and create the workgraph. Step mode is the safe default; execution is always an
explicit product action.

### Headless CLI

```bash
turn doctor
turn create "Build an adaptive narrative engine" --harness codex --run
turn projects
turn graph PROJECT_UUID --tree
turn run PROJECT_UUID
turn organization show PROJECT_UUID
turn work list PROJECT_UUID
turn work claim WORK_ITEM_UUID --node-id NODE_UUID
turn work update WORK_ITEM_UUID --status COMPLETE --evidence-ref tests/report.json
turn logs PROJECT_UUID                 # stitched JSONL event history
turn logs PROJECT_UUID --search error  # free-text search
turn logs PROJECT_UUID --follow         # JSONL live feed; pipe to jq or another reader
turn serve --port 8000
turn trigger emit EVENT_NAME --project-id PROJECT_UUID --data '{"key":"value"}'
```

When run from a project directory, `turn create` uses that current directory as
the project directory. The UI/server uses `TURN_PROJECTS_DIR` for its default
project root.

To activate an event trigger, keep the Turn server running and emit its exact
event name with an optional JSON object. From a project directory,
`--project-id` may be omitted; otherwise provide the target project UUID. The
matching node receives the complete object in its trigger context:

```bash
turn trigger emit goal.plan.requested \
  --project-id PROJECT_UUID \
  --data '{"goal":"Plan a small product launch"}'
```

Use `turn logs PROJECT_UUID --search trigger` to inspect event and activation
records after emission.

Schedule triggers use classic five-field UTC cron only, for example
`*/5 * * * *`; interval forms such as `@every 5m` are not supported. Schedules
do not use a manually configured event name; their configured JSON data is
merged with the schedule event's runtime data.

Workspace configuration is stored in `./.turn/config.json`. Project state and
operational history are stored inside each project at
`./projects/<project_name>/.turn/`; logs are rotated JSONL files in that
project's `.turn/logs/` directory. Each file is project-scoped and named with
the project id and UTC timestamp; the server and CLI stitch those files in
order. Configure rotation with
`TURN_LOG_MAX_RECORDS` or the Workspace settings panel. Records include graph
transitions, state/configuration changes, agent CLI responses, harness launch
and return details, decisions, and errors, so external JSONL tooling can read
the same stream as Turn.

## Verification

```bash
npm run typecheck
npm test
npm run build
python -m pytest -q
```

The process-level Mock workflow laboratory is covered by the mandatory
`turn/tests/test_mock_workflows_e2e.py` end-to-end test, which launches the
repository-owned harness process and drives those scenarios through the API,
terminal transport, retained sessions, persisted graph, runs, and artifacts.

The test suite covers:

- domain schemas, transitions, graph invariants, and storage
- REST, SSE, security, and project lifecycle behavior
- harness capability detection and adapter contracts
- PTY ANSI/input/resize/stall behavior
- browser authoring, graph inspection, terminal transport, themes, and responsive layouts
- deterministic full-run persistence for software, story, and book-shaped workflows
- installed-Herdr integration, durable panes, and cleanup boundaries

Model-backed demonstrations prove installed-harness integration; they are kept
separate from deterministic quality scores.

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
ui/                          dependency-free IDE shell and UI reducer
docs/assets/                 product screenshots and social/GitHub banner
```

## Design constraint

The store never guesses planner intent. It validates keys, references, and graph
acyclicity, then preserves valid objectives and topology exactly—without hidden
child caps, semantic deduplication, title truncation, or domain-specific nodes.

The visual and interaction contract lives in [DESIGN.md](DESIGN.md). This README
is the product, architecture, scope, operation, and verification guide.

## Roadmap

- [x] Basic workflow
- [x] Skills
- [x] Nested planners
- [x] Org agent
- [x] MCP basic
- [x] Arbitrary node reject
- [x] Architecture / Hygiene & Cleanups
- [x] Skills / MCP via Capabilities / Agent Plugins 1.0
- [x] Live Logs / State & Graph Transitions
- [x] Composable graph
- [x] Triggers
- [x] Run Quality Dashboard / Metrics
- [x] Organization contracts / independent plan audit
- [x] Persistent organization review loop
- [x] Tickets / units of work / typed handoffs
- [x] Concurrency and run/token/cost budgets
- [x] Worktree isolation and explicit merge boundary
- [x] Organization-fitness metrics
- [x] Worktrees
- [ ] Multi-graph projects
- [ ] Variables / General data passing between nodes
- [ ] Repeatable organizations - Skipped / Locked nodes that can be re-run with new data
- [ ] Decision-based Routing for nodes 
- [ ] Retries / Recoveries / Timeouts / Exit codes / Better process management for Running Processes
- [ ] Loops / Goals / Hill-climbing with visual feedback and metrics
- [ ] Capability library with Web UI
- [ ] Architecture / Hygiene & Cleanups
- [ ] Native app
- [ ] Terminal UI
- [ ] Better Styling / Document view
- [ ] In-host multiplexer
- [ ] Tmux
- [ ] Ghostty Web
- [ ] Security / Sandboxes / Permission boundaries
- [ ] Website / Demos - Capabilities, MCPs, and Skills
- [ ] Architecture / Hygiene & Cleanups
- [ ] Plugins / Extensions / Hooks
- [ ] Product/domain eval packs
- [ ] Phoenix integration
