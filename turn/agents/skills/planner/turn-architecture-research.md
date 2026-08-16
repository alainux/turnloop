---
name: turn-architecture-research
description: Research and shape an implementation-ready architecture for a real user-facing product.
---

# Architecture research

Use this skill before decomposing a broad engineering request. The goal is a
coherent product plan that workers can implement and verify, not a list of
abstract components.

## Investigate before deciding

- Restate the user's actual deliverable, runtime, entry point, interaction
  loop, and clean-checkout acceptance path.
- Inspect the repository, the live Turn graph, existing package boundaries,
  and the project's runtime constraints before proposing new structure.
- Research authoritative documentation and a focused domain source. For games,
  consider runtime/rendering, input, content, world, narrative/state, audio,
  persistence, tooling, and visual QA; for other products, select the
  equivalent domain lenses instead of copying a game template.
- Compare architectural choices against the constraints. Do not prescribe an
  event bus, data-oriented design, a framework, or a service boundary unless
  the product actually benefits from it.

## Shape the graph

- Make the first level a set of real product boundaries, not chronological
  verbs. Separate independently buildable domains so they can run in parallel.
- State the contract, invariants, durable namespace, tests, and acceptance
  evidence for every worker. Put integration after its prerequisites and make
  it own the real application/package entry point.
- Put verification after the implementation it inspects as an ordinary
  dependency. A verifier is a sequence step, not a special graph relation.
- Avoid subplanners unless the scope is genuinely too large or uncertain for
  one architectural boundary. A single request should normally expose its
  full useful specification immediately.

## Make the metadata executable

The architecture spec must include a concise project-relative
`filesystem_structure` tree. It is a shared composition contract: every
worker must place source, tests, assets, and integration entry points where
the tree says they belong. Keep it small enough to follow and include the
launch command and the location of the final user-facing path in the relevant
section or acceptance criteria.

Record the direct research URLs and why each matters. Select narrow skills for
each executor, integrator, and verifier; author a project `SKILL.md` when no
maintained skill fits. Skills are installed into the project and read from
the filesystem, never pasted into the initial prompt.

## Final coherence check

Before submitting, ask: if all workers follow this graph, do their outputs
compose into the requested product without a disconnected demo, placeholder
overlay, duplicate entry point, or missing user journey? If not, revise the
boundaries, dependencies, contracts, or integration node before handoff.
