---
name: turn-setup
description: Interpret a user request and set up the smallest sufficient Turn workgraph.
---

# Turn setup skill

Use this skill only for the initial planner at the project root. Set up the
board by interpreting the user's actual request and choosing the smallest
sufficient agents, skills, dependencies, and ownership boundaries. Do not
assume the work is a venture, software product, or organization. This skill
must not be inherited by nested planners.

## Interpret before structuring

- Identify the requested outcome, domain, users, constraints, runtime or
  delivery form, quality bar, and explicit scope words before structuring work.
- Choose the shape that fits: one focused worker, a lean MVP or demo, a
  book-writing workflow, a routine automation, a broad product or system, or
  another domain-specific workflow.
- A broad product may need research, design, engineering, verification,
  integration, launch or adoption, and operations stages, but add only stages
  the request actually justifies.
- A small or atomic request should remain small. Do not add organizational
  stages merely because they are available.
- Set up direct board nodes, dependencies, agent types, and selected skills.
  Stop at nested planner boundaries; those planners own their own subtrees.

## Sequence by information, not by habit

Before submitting, inspect the proposed handoffs one by one. A node is
parallel only if it can make its decision or create its output without another
node's research, requirements, design, or other durable decision. When a
downstream engineering or delivery stage needs an upstream product direction,
make the dependency explicit. For a different domain, use its equivalent
information flow. This may produce a staged plan, several independent lanes,
or a small single-node plan; do not force a universal pipeline or parallel
branches.

## Procure only the skills the chosen work needs

- Use `find-skills` to search for narrow domain guidance for the chosen agents.
- For web or app architecture, procure a stack- and runtime-specific
  architecture skill only when an architecture stage is warranted. Do not
  assume one is needed for every request.
- If discovery finds nothing useful, use no invented placeholder skill.
- The selected worker or planner skill owns its own deliverable instructions;
  do not copy those instructions into the setup plan.

## Keep ownership explicit

- The setup planner creates the initial board and its direct handoffs. It must
  stop at the next planner and must not invent or edit that planner's future
  descendants.
- A nested planner may replace or expand only its own subtree. It must not edit
  sibling stages, ancestor-owned edges, or later stages owned by another
  planner.
- Workers own the files and outputs described by their skills and prompts.
  Verifiers inspect their assigned boundary and report evidence; they do not
  repair another stage or redesign the graph outside that boundary.
- Use real dependencies for real handoffs. Do not serialize unrelated work
  merely to make a checklist.

## Documents are produced by workers, not reserved by setup

Do not name, reserve, register, or fabricate future document filenames,
document references, or artifacts. Agents create their own outputs when they
work, according to their assigned skills and prompts. A worker submits an
explicit artifact only after it has created the actual output. The setup plan
contains topology, ownership, dependencies, and skill assignment—not a catalog
of files that later agents may or may not produce.
