# Current scope and future-readiness ledger

“Current” means an end-to-end implementation and verification exist. “Future-
ready” means only a deliberate contract or extension seam exists.

## Implemented current scope

| Verifiable item | Current implementation | Evidence |
|---|---|---|
| Minimal IDE shell | Prompt-first wordmark-only chrome, collapsed explorer, graph workspace, inspector, status bar, settings/policy panels, light/dark and density tokens | UI-quality and browser visual tests |
| Compact authoring | One objective field; attachment/directory/harness/model/reasoning toolbar; one low-frequency configurator | Browser E2E |
| Project resources | Text/binary files are size-checked, safely named, persisted under the project, attached to the root, and inherited through ancestry | API attachment test and context renderer |
| Harness discovery | Codex, Claude Code, OpenCode and Pi are detected and invoked through adapters; unavailable choices are explicit | Capability/adapter tests |
| Graph/node/agent/type | Strict graph schema, canonical planner type at root/fork/store boundaries, open other agent type IDs, central capability catalog, pure runnability/evaluation | Domain and topology tests |
| UI state machine | Server presentation plus shell reducer cover queued, ready, running, paused, dependency/input waits, manual review, parent verification, accepted, failed, cancelled, overlays, stale responses, and single-flight commands | State/transition/browser tests |
| Review ownership | Manual mode exposes human accept/reject. Parent mode schedules a real parent verifier and hides human decisions until the bounded failsafe transfers ownership; rejection resumes the same child session/worktree | Transition, verifier, lifecycle tests |
| Completion gating | Unaccepted prerequisites cannot release dependents; unresolved input/review reopens containers and prevents final shipping | Graph/state/runner tests |
| Real terminal | Local harnesses share a POSIX PTY; xterm receives raw ANSI bytes over WebSocket and can write/resize; active reconnect works and completed snapshots are memory-bounded | PTY unit and browser E2E |
| Damage control | Whole-run timeout, inter-output silence watchdog, cancellation, retry/backoff, overloaded/context/rate classification, configurable policy | Recovery/PTY/runner tests |
| Stable live rendering | SSE reloads are signature-gated; selected dirty fields/focus survive live events; active xterm is not recreated by unrelated events | Browser identity/dirty-form regressions |
| Local security boundary | Loopback Host plus same-origin HTTP/WebSocket enforcement protects mutations, native picker, and PTY input | HTTP/WebSocket security tests |
| Node/branch editing | In-place scope/config fields, pristine save, cascaded agent config, contextual fork, branch pause/resume/cancel, confirmed regeneration | Browser, API, store tests |
| Artifacts and usage | Compact artifact disclosures; run/node/branch/project token and cost aggregation | Browser/API tests |
| CLI/headless | UI-free `TurnCore` and create/projects/graph/run/doctor/serve commands | CLI/core tests |
| Full demonstrations | Adventure, constellation, and non-trivial WebGL game outputs plus graph/log/artifact/session inspections | `docs/ACCEPTANCE_RUNS.md` |

## Future-ready only

| Intended capability | Seam present now | Still required |
|---|---|---|
| Cloud/direct API agents | `TerminalTransport`, Worker/Planner, registry boundaries | Remote event/input adapter, provider auth, parity tests |
| Explicit portable compaction | Context-pressure classification and policy field | Per-harness compact/resume operations and context ledger |
| Custom types/outputs | Open type IDs, artifact schemas, capability records | Persisted registries, editors, renderers, migrations |
| Skills/tools/MCP management | Typed config lists and resource references | Discovery, trust, installation, permission UI |
| Shared contexts/A2A | Referenceable resources/artifacts/event bus | Scoped channels, membership, provenance, budgets |
| Validation/optimization loops | Append-only graph, attempts, validator-compatible type | Composable loop specs, termination/budget/regression gates |
| Decomposition as specs | Validated plans, revisions, lineage | Immutable named specs, diff, approval, promotion/reuse |
| Native bundles | Local headless API and replaceable platform picker | Benchmarked signed bundles, updates, secure secrets, ConPTY |

## Explicit non-claims

- The silence watchdog detects a live process with no output; it does not yet
  estimate semantic reasoning speed or portably compact every provider.
- Parent verification uses the configured parent harness, not a future
  validator registry.
- Missing harness cost remains unknown rather than estimated.
- POSIX PTY is current on macOS/Linux; Windows ConPTY belongs to native
  packaging work.
