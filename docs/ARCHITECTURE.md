# Turn architecture

## Current architecture

Turn uses a hexagonal boundary around a graph kernel:

```text
Web UI ──REST/SSE/WebSocket──┐
                   ├── TurnCore / Runner ── Store ── SQLite
CLI ───────────────┘          │
                              ├── Planner protocol
                              ├── Worker protocol
                              └── Execution adapter
                                      ├── direct
                                      └── Prefect (optional)
```

The graph is the source of truth. A node owns intent and an `AgentConfig`; an
agent configuration selects a stable harness adapter and leaves harness-specific
flags outside the domain. In short: `graph → node → agent → type/harness`.

### Stable contracts

- `PlanResult` validates unique keys, references, and acyclicity. Persistence
  does not alter semantic content.
- `WorkerResult` has exactly four outcomes: complete, expand, block, or fail.
- A worker is not successful merely because its subprocess exited zero:
  structured output is required, named result/plan fences take precedence over
  tool JSON, and declared file artifacts must exist inside the worktree.
- `RunPolicy` is project-scoped and contains execution/recovery/review choices.
- `present_node()` is the only execution-facts-to-UI-state projection. The
  browser renders `ui_state` and `allowed_actions`; it does not infer them.
- `TurnCore` is UI-free and is the public headless embedding boundary.
- `WorkerRegistry` and the execution adapter isolate subprocess and scheduler
  technology from the graph.
- `TerminalTransport` isolates byte streaming, input, resize, lifecycle, and
  stall telemetry. `LocalPtyTransport` is the current macOS/Linux adapter;
  completed reconnect snapshots are LRU-bounded while the full transcript is
  durable in run/artifact storage. Active sessions are never evicted;
  remote provider and Windows ConPTY implementations can preserve the same
  worker/UI contract.
- `HARNESS_CATALOG` is the single capability boundary for installed harnesses,
  model-family effort profiles, and future model discovery. Unknown model IDs
  inherit the harness contract; recognized, delimiter-bounded compact/non-
  reasoning family names narrow the choices without false-matching custom IDs
  such as `smalltalk-pro`. This is a conservative MVP fallback, not provider
  discovery. UI, API, core, and worker boundaries share this policy.
- `layoutDendrogram()` is a UI-free geometry function. Rendering and graph
  actions consume its positions without owning hierarchy logic.
- `reduceAppState()` owns shell phase, selection, connection, overlay, and
  single-flight command state. It refuses stacked panels and overlapping
  mutations; the pure shortcut resolver observes the same substates.
- Graph requests and SSE handlers carry the selected project identity. A late
  response from an earlier selection is discarded instead of repainting stale
  data into the active workspace. A failed initial load exits the loading phase
  into an explicit retryable project shell instead of leaving dead chrome.
- The current single-user server is loopback-only at the ASGI boundary. Host,
  same-origin HTTP, and WebSocket checks protect mutations, native dialogs, and
  PTY input even if uvicorn is accidentally bound to a broad interface. Remote
  service deployment requires a future authenticated transport.

### Current runtime sequence

1. Authoring creates a project root, repository, agent assignment, and policy.
2. A planner emits a validated graph.
3. Pure evaluation derives runnability from status, pauses, inputs, ancestors,
   and dependency edges.
4. The runner launches eligible leaves in a PTY, records every attempt, streams
   raw output, applies whole-run and inter-output timeout/retry policy, and
   persists artifacts and usage.
5. Completed software worktrees merge upward. In manual mode, a human accepts
   or rejects. In parent mode, the parent agent reviews the child diff,
   artifacts, logs, and graph evidence; it may reject with feedback. Parent and
   child session IDs plus the child worktree survive correction rounds.
   Acceptance cleans redundant worktrees.

Every agent prompt includes an absolute graph-explorer command bound to its
requester node. Each invocation is audited, which makes “inspect before
duplicating” independently verifiable. Changing harness providers clears the
old provider session ID; edits within one harness preserve it for review
continuity.

Every completed attempt persists a readable log. Harness executor notes remain
authoritative when supplied; otherwise the run summary is retained as the
transcript fallback. This keeps deterministic and non-terminal workers
inspectable without fabricating terminal output.

## Extension points placed for future scope

These are interfaces or schema seams, not claims of shipped functionality:

| Future capability | Existing seam | Required implementation later |
|---|---|---|
| Custom agent types | Open `AgentConfig.type_id`, registry boundary, capability catalog | Persisted type definitions, editor, validation, versioning |
| Skills/tools/MCP | Lists on `AgentConfig`, resources on nodes | Provider-specific resolution, permission mediation, management UI |
| Custom output types | Artifact contract and capability catalog | Registered schemas, renderers, validation/migrations |
| Remote/cloud terminals | Provider-neutral `TerminalTransport`, local PTY, xterm/WebSocket input and resize | Authenticated remote transport, provider event parity, ownership/audit policy |
| Specialized validator agents | Current parent-verification loop and decision artifacts | Versioned validator registry, reusable regression suites, quorum/budget policies |
| Shared contexts/A2A/chat groups | Artifact/resource references and event bus | Scoped channels, membership, provenance, context budgets |
| Validation/regression/hill-climb/Ralph loops | Append-only graph and agent type IDs | Reusable loop-block specs, termination/budget policies, evaluation ledger |
| Decomposition as a spec | Validated `PlanResult`, revisions, lineage | Immutable named spec versions, approval state, diff/promotion workflow |
| Durable distributed execution | Execution-adapter boundary | Queue/lease worker, heartbeats, crash recovery, idempotency keys |
| Native desktop distribution | Dependency-free UI and headless local API | Measured packaging choice, signed installers, upgrades, secure secret store |

`compact_on_context_pressure` is retained as a policy extension point. Failure
classification and retry are implemented; explicit provider-controlled context
compaction is not yet portable across the four harnesses and is therefore not
presented as complete.

## Stack review

The current stack is appropriate for the MVP, with explicit limits:

- **Python + FastAPI** keeps agent/process integration simple and the headless
  core reusable. It is not yet a small native bundle; measure startup/RSS and
  packaging before considering a Rust/Tauri shell.
- **Vanilla modules and CSS** keep the UI runtime tiny. The pure reducer and API
  module provide migration seams. Adopt a typed component system only when UI
  complexity, accessibility, or reuse demonstrably exceeds this structure.
- **SQLite + SQLAlchemy** is a strong local-first default. The ordered migration
  ledger is sufficient for the MVP; use Alembic before multi-user or distributed
  deployments.
- **SSE** remains right for graph events. A separate WebSocket carries the
  bidirectional PTY byte stream, input, resize, and live-session status without
  distorting REST or graph events.
- **Git worktrees** are effective isolation for code. They must remain a worker
  concern: books, games, and structured artifacts need artifact-store adapters
  rather than mandatory Git semantics.
- **Direct asyncio scheduling** is clearer and lighter today. Prefect remains an
  optional execution adapter; a durable queue should be adopted only with a real
  multi-process/recovery requirement.
- **Harness-reported cost** is authoritative when available. Turn deliberately
  reports unknown cost as unknown rather than inventing estimates.

## Anti-drift rules

1. Add capabilities through schemas/protocols/adapters, never keyword checks in
   the store.
2. Keep execution facts persistent and UI projection pure.
3. Preserve planner output or reject it structurally; never silently rewrite it.
4. Every new transition needs a runner test and a presentation-state test.
5. Every harness-specific flag, parser, or session rule stays in its adapter.
6. Future capability fields must be labeled as extension points until an end-to-
   end implementation and verification exist.
7. Provider capability rules live in the harness catalog and must be tested at
   both authoring and execution boundaries; UI-only option filtering is not a
   sufficient guard.
8. Panels, shortcuts, commands, and async project results must transition
   through reducer-owned state; native DOM visibility is an output, not an
   alternate source of truth.
