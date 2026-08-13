# Turn interface design system

Every visible element must communicate state, enable an action, establish
hierarchy, or teach something useful. The interface is intentionally quiet:
there is no product logo asset, hero illustration, decorative animation,
breadcrumb, activity rail, or duplicated project/status copy.

## Foundations

- Semantic tokens cover neutral surfaces, borders, text, accent, success,
  attention, and danger in dark and light themes.
- A 4 px rhythm, four control heights, four icon sizes, and three radii keep
  similar controls visually identical.
- Motion is reserved for real running nodes, flowing active edges, terminal
  output, and feedback. Reduced-motion removes nonessential animation.
- Inputs use one normalized 12 px type scale; node objectives clamp rather than
  overflow, while detailed instructions live in the inspector.

## Information architecture

- First load is the prompt-first authoring surface with the explorer closed.
- Project location is a single folder action beside the prompt. Name,
  permissions, and run options share one compact configuration popover.
- The top bar contains the wordmark, explorer toggle, help, new project, and
  workspace settings. Theme belongs only in workspace settings.
- Rename and remove live beside the relevant explorer entry. Project execution
  policy and workspace settings are non-modal side panels.
- Graph legend, counts, zoom, connection state, and useful live status share the
  footer rather than consuming a second toolbar.
- Node selection opens a contained inspector. Scope and agent configuration are
  editable in place; save actions are disabled while pristine.

## Graph and node grammar

- Containment is a deterministic left-to-right dendrogram. Depth maps to
  columns; every parent is centered across its first and last child.
- Solid orthogonal branches represent containment; dashed overlays represent
  dependencies. The graph remains the only intentionally scrollable canvas.
- Compact node cards show objective, semantic state, harness, model, reasoning,
  resource presence, and usage without duplicate prose or empty vertical bands.
- The card selects; right-click or Shift+F10 opens its contextual actions.
  Destructive or branch-replacing actions require app-native confirmation.

## Icons, help, and accessibility

- Functional icons use the vendored Lucide ISC vocabulary in `ui/icons/`.
  Node avatars remain the intentionally custom graph-specific exception.
- Icon-only controls have accessible names and focus/mouse tooltips. Unfamiliar
  settings have adjacent help hints, including recovery and stall detection.
- Tooltips are fixed above panels and dialogs. Focus rings, selected states,
  keyboard shortcuts, reduced motion, and compact layouts are browser-tested.

## Terminal and adapters

The terminal is xterm backed by a real local PTY transport. It displays raw ANSI
color, streams resize events, and accepts input while a local harness is active.
The browser speaks only the terminal WebSocket contract; future cloud-stream
adapters implement the same transport boundary without changing the inspector.

## Verification contract

Offline tests enforce icon completeness, semantic tokens, no product images,
no native title hints, normalized help coverage, deterministic dendrogram
geometry, guarded transitions, PTY input/resize/color/stall behavior, review
ownership, and persistence. Browser tests cover prompt-first onboarding,
attachments, pristine saves, confirmations, stable DOM identity during SSE,
xterm rendering, theme and compact layouts, screenshots, zero document
overflow, and console cleanliness.
