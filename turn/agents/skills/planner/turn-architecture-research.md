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

Prefer a project-owned `ARCHITECTURE.md` as the durable architecture source.
Submit its project-relative path as a document reference and as a file
artifact. Additional Markdown documents and nested imports are valid when the
architecture, prompts, or evidence are easier to maintain as separate files.
The framework must not require a fixed set of section names: use a coherent
outline appropriate to the product and keep any structured metadata as a
compact plan. A concise project-relative filesystem tree is
useful when it is a real composition contract: every worker must place
source, tests, assets, and integration entry points where the tree says they
belong. Keep it small enough to follow and include the launch command and the
location of the final user-facing path in the relevant document or acceptance
criteria.

Record the direct research URLs and why each matters. Select narrow skills for
each executor, integrator, and verifier; author a project `SKILL.md` when no
maintained skill fits. Skills are installed into the project and read from
the filesystem, never pasted into the initial prompt. Keep document references
dynamic: graph inspection exposes paths, while workers explicitly open files
when they need their current contents.

## Software testability bar

For software, the architecture must name the real launch command, clean-
checkout path, automated test commands, observable browser or UI acceptance
journey, fixture/seed strategy, and visible failure behavior. Every runtime,
display, input, persistence, and integration claim must map to a deterministic
test; visual claims must also have a browser or screenshot check when the
runtime can render. The integrator must exercise the real entry point and
reject placeholder screens, disconnected overlays, and module-only demos.

## Final coherence check

Before submitting, ask: if all workers follow this graph, do their outputs
compose into the requested product without a disconnected demo, placeholder
overlay, duplicate entry point, or missing user journey? If not, revise the
boundaries, dependencies, contracts, or integration node before handoff.
