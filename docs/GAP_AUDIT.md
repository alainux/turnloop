# Consolidated gap audit

This file records closure of the final annotated review. Automated success is
not accepted as proof by itself; every row names an observable contract.

## Current implementation

| Annotated concern | Closed contract |
|---|---|
| Fake transcript terminal / cosmetic LIVE | Real PTY transport, xterm canvas, raw ANSI, WebSocket input/resize, live status from transport |
| Parent complete while descendants await review/input | Evaluation and finalization exclude unresolved review; dependency dispatch requires accepted prerequisite |
| Parent and human both verify | Explicit review owner; parent mode hides human actions, bounded verifier error transfers to manual |
| Full-panel/node flicker | Signature-gated graph reconciliation, no loading placeholder on event refresh, terminal preserved |
| Duplicate scope/prompt editors | One always-visible in-place scope form; one always-visible agent config form |
| Dense artifacts | One-line disclosure with icon/name/type and optional expanded detail |
| Repeated/broken chrome | Wordmark only, no hero/logo/raster assets, no breadcrumb/activity rail/status dot |
| Projects too prominent | Sidebar closed on first load; top-bar toggle and centralized shortcut help |
| Oversized/duplicated authoring | One prompt and compact toolbar; name/permission/policy in one configurator |
| Project path default | Core defaults to current working directory; one native-picker affordance |
| Empty harness | Detecting/no-harness state plus only available selectable adapters |
| Cosmetic “choked” option | Real inter-output watchdog with configured silence threshold and retry decision |
| Missing attachments | Browser chips → size/type-safe API persistence → root resources → inherited execution context |
| Always-enabled saves | Settings, policy, agent, and scope saves compare against exact form snapshots |
| Unconfirmed regenerate/cancel | Shared app-native confirmation; regeneration returns created/superseded IDs and leaves failure retryable |
| Inconsistent button/input typography | Shared semantic control sizes and 12px editable-value token |
| Project-row age and state dot | Removed; row owns concise name, harness, and contextual rename/remove only |
| Node vertical waste | Node height normalized to 58px with one-line objective and dense metadata |

## Future-ready only

Remote cloud streaming, native bundling, custom agent/output registries,
composable loop strategies, shared chats/A2A, and writable long-lived cloud
terminals remain extension points rather than implied MVP features. The current
transport, graph, node-agent-type, artifact, and policy boundaries are the seams
reserved for that work.

Verification gates: full Python and JS suites, explicit Chromium E2E with dark/
light and 700px visual generation, PTY ANSI/input/resize/stall tests, a manual
tinkering run, persisted database/log/artifact inspection, and a separate
read-only agent review.
