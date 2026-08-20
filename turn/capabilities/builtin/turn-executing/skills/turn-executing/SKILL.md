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
4. Check whether the assignment is still **leaf-fit** before editing. A leaf is
   one cohesive contract with one primary craft or implementation boundary,
   bounded ownership, and one concrete acceptance path. If the assignment
   actually contains several independently ownable contracts, multiple crafts,
   a substantial internal backlog, or its own composition/evaluation problem, do not
   silently turn one executor into an entire team. Escalate it to a nested
   planning boundary as described below.

## Implementation contract

- Implement a real, usable slice in the existing project structure. Do not
  create a disconnected showcase, placeholder-only surface, or private
  duplicate entry point.
- Re-read the original user-facing outcome before implementation. Verify
  external facts, maintained APIs, and repository conventions when they affect
  the result; do not quietly replace an unknown with a plausible invention.
- Define and preserve typed inputs, outputs, invariants, and integration seams.
  Keep the canonical state and ownership clear so another worker can compose
  your result without guessing.
- Honor every explicit acceptance criterion. Return criterion-level evidence
  with inspectable repository-relative references; do not mark a criterion
  satisfied from a summary alone.
- The completion envelope must include one evidence item for every declared
  criterion. Use the exact criterion id, `status: "PASS"` only after running
  the check, a concise observed result, and at least one repository-relative
  `refs` entry. Generic `artifacts` or a prose summary do not substitute for
  criterion evidence. Use `FAIL` or `UNVERIFIED` when the work is not proven.
- Work only in the execution workspace assigned to this node. Do not merge
  unrelated branches or modify another node's workspace; the integrator owns
  cross-branch assembly.
- Stay inside the assigned boundary. Do not rewrite sibling ownership or absorb
  another worker's responsibility. Executors do not author arbitrary graph
  topology. The one exception is **scope escalation**: when the current node is
  demonstrably not leaf-fit, return `EXPAND` with exactly one child planner so
  that a planning boundary—not the executor—owns the actual decomposition. If
  the boundary is merely underspecified or conflicts with a prerequisite
  contract, report the exact gap instead of compensating with an unrelated
  implementation.
- For visual or interactive work, connect controls, overlays, assets, and
  feedback to the actual runtime state. A UI element that is not reachable from
  the requested journey is not a completed feature.
- Add focused tests or deterministic checks for the contract you own, and leave
  concise launch/use notes where downstream workers can find them.
- Treat trigger activation as an input to the node, not as work the node must
  recreate. If `TURN_CONTEXT` contains a trigger context, do not emit that
  context's event name from the node; emitting it would create an accidental
  self-trigger loop. Use a synthetic trigger context, a direct local entrypoint,
  or fixture-based tests while developing, and reserve the activating event for
  a deliberate end-to-end demonstration.
- Make the work verifiable: identify the command, fixture, observable output,
  or manual journey that proves the exported contract. “Hard to test” is a
  signal to add an adapter, fixture, evidence file, or an explicit BLOCK/FAIL,
  never a reason to claim completion.

## Adaptive scope escalation

Turn is allowed to discover organizational structure during execution. If a
node looked leaf-sized to its parent planner but, after reading the repository
and prerequisite contracts, it clearly requires a team, **do not compress the
team into one heroic executor**. Return `EXPAND` and delegate the whole current
contract to exactly one nested planner child.

That child is a nested organization boundary. Give it the inherited mission,
constraints, exported contract, relevant document/resource references, and the
evidence that revealed the larger scope. Do not pre-author its descendants;
the child planner will use `$turn-planning` to choose the organization. Declare
it unambiguously with `agent_type: "planner"` and `plan: true`.

Example handoff:

```sh
turn agent submit --kind result --stdin <<'TURN_PAYLOAD'
{"outcome":"EXPAND","summary":"The assigned boundary contains multiple independently verifiable contracts and needs nested decomposition.","missing_inputs":[],"artifacts":[],"children":{"nodes":[{"key":"nested-boundary","objective":"Plan and deliver the inherited boundary","agent_type":"planner","plan":true,"generated_prompt":"Own the inherited mission. Decompose it into leaf-fit work, compose descendants when required, and independently evaluate the exported contract before returning it to the parent. Preserve the existing project and prerequisite contracts."}]}}
TURN_PAYLOAD
```

Use this only for real scope discovery. A cohesive leaf should execute rather
than bouncing through another planner. `EXPAND` is not a way to avoid difficult
work; it is the escape valve that prevents accidental under-decomposition.

## Pre-handoff quality gate

Before reporting `COMPLETE`, exercise the real boundary you own: run the
focused tests and the documented build or launch command from the project
structure. For visual or interactive work, use the available browser-control
or UI inspection skill and inspect the rendered result, controls, state transitions, and console
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

For a node with acceptance criteria, the result handoff has this shape (use the
exact ids from the live graph):

```sh
turn agent submit --kind result --stdin <<'TURN_PAYLOAD'
{"outcome":"COMPLETE","summary":"Implemented and checked the boundary.","missing_inputs":[],"artifacts":[{"kind":"text","name":"implementation-notes","ref":"IMPLEMENTATION.md"}],"evidence":[{"criterion_id":"typed-core","status":"PASS","summary":"Typed records and inventory calculation passed the focused tests.","refs":["src/inventory/models.py","tests/test_inventory_core.py"]}]}
TURN_PAYLOAD
```

Repeat the `evidence` object for every criterion. The server validates these
records before advancing downstream work, so omitting them will correctly
reject an otherwise plausible completion summary.
