---
name: turn-setup
description: Interpret a user request and set up the smallest complete Turn workgraph that preserves its scope.
metadata:
  opencode/slash: "true"
---

# Turn setup skill

Use this skill only for the initial planner at the project root. Set up the
board by interpreting the user's actual request and choosing the minimum
  complete set of agents, capability plugins, sequence stages, and ownership boundaries that
preserves the requested outcome and scale. Do not invent a venture, software
product, or organization when the request does not state one. Explicit scope
and scale words are binding. This skill must not be inherited by nested
planners.

## Interpret before structuring

- Identify the requested outcome, domain, users, constraints, runtime or
  delivery form, quality bar, and explicit scope and scale words before
  structuring work. Explicit scope outranks the default preference for a
  smaller plan.
- Choose the shape that fits: one focused worker, a lean MVP or demo, a
  book-writing workflow, a routine automation, a broad product or system, or
  another domain-specific workflow.
- An app factory is organization-scale by definition: it is a repeatable
  organization/system for producing multiple applications, not one app and
  not a research assignment. Treat explicit requests for an organization,
  platform, ecosystem, enterprise, multiple products, or multiple teams as
  broad even when the request also uses a narrow word such as "app" or "tool".
  Do not collapse that scope into a single research, design, or implementation
  worker. Use broad first-level ownership and nested planners for domains that
  need their own evolving subtree; research is only a supporting domain.
- A broad product or system may need research, design, engineering,
  verification, integration, launch or adoption, and operations stages, but
  add only stages the request actually justifies. When organization-scale
  scope is explicit, preserving that scale is part of what the request
  justifies.
- A small or atomic request should remain small. Do not add organizational
  stages merely because they are available when the request did not ask for
  organization-scale scope.
- Set up direct board nodes, sequence stages, agent types, and selected capability plugins.
  Stop at nested planner boundaries; those planners own their own subtrees.

## Topology ownership

Use `$turn-planning` as the authoritative contract for decomposition and graph
topology. This root-only skill supplies scope, capability procurement, and
ownership boundaries; do not duplicate topology rules here or put them in
worker prompts.

## Root setup contract

- Preserve a user-provided project name. If none exists, choose a concise
  navigation name and return it as the top-level `project_name` field.
- Procure only the reusable capability plugins the chosen work needs. Search
  the local catalog first, inspect candidates, and load every selected id into
  the project before submitting the plan.
- When a worker needs an external tool or data source, package it as an MCP
  component of a capability plugin and record its source and user-owned setup
  requirements in the project document. Do not claim credentials or binaries
  are present.
- Do not reserve future filenames, documents, or artifacts. Workers create
  files and submit the files they actually created.
- Stop at nested planner boundaries. The next planner owns its subtree; do not
  edit sibling or later stages.

The root planner interprets the request and sets up the board. It does not do
a descendant's implementation, research, or verification work.

## Procure only the capability plugins the chosen work needs

- Use `turn-authoring-capabilities` and the local catalog to search for narrow
  domain guidance for the chosen agents.
- Load every selected built-in capability from the Turn catalog with
  `turn capabilities load <id>`. For external guidance, author a complete
  Agent Plugins package and load its directory into the catalog first.
- For web or app architecture, procure a stack- and runtime-specific
  architecture skill only when an architecture stage is warranted. Do not
  assume one is needed for every request.
- If discovery finds nothing useful, use no invented placeholder capability.
- The selected worker or planner capability owns its own deliverable instructions;
  do not copy those instructions into the setup plan.

## Keep ownership explicit

- The setup planner creates the initial board and its direct handoffs. It must
  stop at the next planner and must not invent or edit that planner's future
  descendants.
- A nested planner may replace or expand only its own subtree. It must not edit
  sibling stages, ancestor-owned edges, or later stages owned by another
  planner.
- Workers own the files and outputs described by their capability plugins and prompts.
  Verifiers inspect their assigned boundary and report evidence; they do not
  repair another stage or redesign the graph outside that boundary.
- Use real sequence stages for real handoffs. Do not serialize unrelated work
  merely to make a checklist.

## Documents are produced by workers, not reserved by setup

Do not name, reserve, register, or fabricate future document filenames,
document references, or artifacts. Agents create their own outputs when they
work, according to their assigned skills and prompts. A worker submits an
explicit artifact only after it has created the actual output. The setup plan
contains topology, ownership, sequence, and skill assignment—not a catalog
of files that later agents may or may not produce.
