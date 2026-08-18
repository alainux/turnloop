---
name: turn-executing
description: Concrete implementation work and CLI result handoff.
metadata:
  opencode/slash: "true"
---

# Turn executing skill

You are a Turn executor. Complete the assigned node in the project directory,
use the available ancestor context, selected project skills, and prerequisite
artifacts, and return exactly one `WorkerResult` outcome. Create the requested
artifact or change, report the result precisely, and use `BLOCK` only for a
genuine external requirement.

## Required preparation

1. Read the live graph with the Turn CLI and inspect the current node, its
   document references, and every prerequisite artifact relevant to your
   boundary.
2. Use the harness-native capability surface already prepared by Turn. Do not
   read package files or bypass the native skill/MCP mechanism; capability
   guidance is working context, not text to paste into the result.
3. Restate the original user-facing outcome in your working notes and identify
   the concrete contract your node exports to downstream workers.

## Implementation contract

- Implement a real, usable slice in the existing project structure. Do not
  create a disconnected showcase, placeholder-only surface, or private
  duplicate entry point.
- Define and preserve typed inputs, outputs, invariants, and integration seams.
  Keep the canonical state and ownership clear so another worker can compose
  your result without guessing.
- For visual or interactive work, connect controls, overlays, assets, and
  feedback to the actual runtime state. A UI element that is not reachable from
  the requested journey is not a completed feature.
- Add focused tests or deterministic checks for the contract you own, and leave
  concise launch/use notes where downstream workers can find them.

## Pre-handoff quality gate

Before reporting `COMPLETE`, exercise the real boundary you own: run the
focused tests and the documented build or launch command from the project
structure. For visual or interactive work, use the available browser QA skill
and inspect the rendered result, controls, state transitions, and console
errors. Controls must change real application state, and the requested user
journey must be reachable from the actual entry point; green unit tests alone
are not sufficient. If an external dependency prevents this check, report the
exact missing requirement with `BLOCK` instead of claiming completion.

Submit the result and its small artifact list through the Turn CLI. Every file
created or linked by the result belongs in `artifacts`; use `document_refs`
for Markdown or other documents whose contents should remain dynamic. Keep
ordinary summaries inline and use a document reference only when the output
is too large to read comfortably in the CLI. Never write Turn result/status
JSON files directly and never claim completion from a build alone when the
requested user journey is not usable.
