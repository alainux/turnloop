# Turn design system

Turn is an operating surface for adaptive workgraphs. Its visual mode is
**operate**: users arrive to author, decompose, inspect, steer, review, and
ship—not to admire a dashboard. The product is deliberately sober: no logo,
hero illustration, decorative status lights, or literal “AI” imagery. The
wordmark, professional Lucide vocabulary, orthogonal workgraph, and truthful
runtime motion are enough identity.

## Information architecture

1. A prompt-first authoring surface owns first load.
2. The workgraph is the primary project surface.
3. The project explorer is optional navigation and starts collapsed.
4. The inspector presents and edits one selected node in place.
5. Global/project settings are side panels; rare authoring controls share one
   compact configurator.
6. Relationship legend, graph counts, connection, usage, and zoom live in the
   status bar.

One fact gets one primary home. A project title appears in the selected project
row and workspace title, not in a breadcrumb and two footers. Objective and
instructions have one in-place editing surface. Branch actions live in the
node context menu. There are no self-labels, redundant dots, or “future” badges.

## Geometry and tokens

- UI base: 13px/1.45; labels 9–10px; titles 13–16px; editable values 12px.
- Title bar: 36px. Status bar: 24px. Sidebar: 236px. Inspector: 360px.
- Controls: 24px compact, 28px normal, 32px prominent.
- Graph nodes: 224×58px. Objectives are one ellipsized line; the inspector owns
  long intent and prompts.
- Buttons never wrap. Secondary actions move to menus before forming a second
  toolbar row.
- Inputs share one text token, padding, border, focus ring, and radius.

Semantic tokens own styling: `--surface-0..3`, `--text-2..3`, `--border`,
`--accent`, state colors, `--space-1..5`, `--control-xs..lg`,
`--icon-xs..lg`, `--radius-sm..lg`, and motion durations. Dark and light themes
preserve semantic contrast rather than merely invert colors.

## Components

### Authoring

The full objective is the only large field. Attachments, directory, harness,
model, reasoning, configuration, and submit are one compact toolbar. Project
name, permission, create/open, auto-run, sequential, and delay live in one
configurator. Directory selection crosses a replaceable native-platform
boundary and defaults to the process working directory. Attached files show as
removable chips and become immutable root resource references.

### Graph node

Each node shows one semantic avatar, state text, harness, model, reasoning,
nonzero resource counts, and available token usage. Node avatars may remain
code-native masks. Running/verifying nodes use a restrained halo and their
active edges flow. Completed nodes never animate. Reduced motion collapses all
animation.

### Inspector

Overview order is identity → agent configuration → current gate/review → usage
→ valid execution actions → scope. Agent and scope fields are the presentation,
not duplicate edit forms. Save is disabled while pristine. Regeneration and
cancellation use one app-native confirmation component.

### Terminal

The terminal is an actual xterm canvas connected to a provider-neutral terminal
transport. Local harnesses run in a POSIX PTY with raw ANSI/true-color bytes,
input, resize, cancellation, reconnectable snapshot, and a configured silence
watchdog. `LIVE` means the WebSocket reports an active writable process;
otherwise the label is `TRANSCRIPT`. Future cloud harnesses implement the same
transport contract without changing graph or UI code.

### Artifacts and help

Artifacts are one-line icon/name/type disclosures; path and preview appear only
when expanded. Every ambiguous or icon-only command has an accessible name and
custom tooltip. Shortcuts and the product safety disclaimer have one standard
`?` surface.

## Current scope versus future readiness

Implemented now: prompt-first authoring, attachments, native directory-picker
boundary, detected local harnesses, model-aware reasoning, IDE panels/themes,
dendrogram, compact node cards, in-place configuration/scope editing, graph
menus, human gates, manual review, bounded parent verification with same-session
rejection, PTY+xterm, stream-stall recovery, usage, CLI/headless core, visual
tests, and responsive desktop/compact layouts.

Future-ready only: provider API/cloud terminal transports, portable explicit
context compaction, registered custom agent/output types, complete skill/tool/
MCP management, shared chats/A2A, reusable validation and optimization loops,
spec promotion, and signed native bundles. These remain documented seams and
must not render dead “future” controls.

## Visual acceptance

- No horizontal document overflow at 1440, 980, 780, or 700px.
- Projects are closed on first load and the prompt has visual prevalence.
- No opaque raster imagery, broken image references, ad-hoc inline icons, or
  missing local icon masks.
- No simultaneously visible duplicate objective, prompt, project usage, or
  branch command.
- Similar inputs/buttons share computed height, font size, and radius.
- Tooltips remain inside the viewport and above side panels/dialogs.
- Dark/light, compact, reduced-motion, active-node, xterm, and rapid-event
  stability all have browser assertions.
