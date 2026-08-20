---
name: turn-setup
description: Interpret a user request and set up a right-sized complete Turn organization that preserves its scope.
metadata:
  opencode/slash: "true"
---

# Turn setup skill

Use this skill only for the initial planner at the project root. Set up the
board by interpreting the user's actual request and choosing the right-sized
complete set of planning boundaries, agents, capability plugins, sequence
stages, and ownership boundaries that preserves the requested outcome and
scale. Optimize for truthful ownership and finished delivery before optimizing
for node count. Explicit scope and scale words are binding. This skill must not
be inherited by nested planners.

## Interpret before structuring

- Identify the requested outcome, domain, users, constraints, runtime or
  delivery form, quality bar, and explicit scope and scale words before
  structuring work. Explicit scope outranks the default preference for a
  smaller plan.
- A request for a product, experience, system, publication, service, or other
  deliverable is a request for the complete usable result unless the user
  explicitly limits it to an MVP, demo, prototype, experiment, or similar
  slice. Do not use missing detail as permission to produce a disconnected
  proof of concept or hand a broad outcome to one heroic worker.
- Infer the requested outcome, audience, delivery form, material contracts,
  acceptance evidence, and genuinely independent work. Different craft,
  source boundary, evidence path, or information dependency can justify a
  boundary; no industry checklist is mandatory. Name responsibilities from
  the actual request and preserve them through composition when the outcome
  needs composition.
- Use recursion when a direct child is too broad for one accountable worker.
  If a boundary contains multiple independently verifiable contracts,
  independent crafts, a substantial backlog, or its own composition/evaluation
  need, make it a planner node and stop there. The nested planner owns the
  boundary's internal shape; do not pre-author a universal organization.
- For larger work, model the actual delivery lifecycle before selecting work
  nodes: identify the outcome, retire material risks, produce the required
  work, compose independent outputs when necessary, and evaluate the result
  at the boundary where defects can still be repaired. Omit or combine stages
  when the domain does not need them and record that rationale. This is a
  judgment, not a fixed domain-specific ceremony.
- If an omitted decision would materially change the audience, platform,
  interaction model, visual direction, delivery target, business/safety
  constraints, or success measure, create a short planner clarification
  boundary with one to three precise `required_inputs`; do not disguise the
  question as an implementation task. It must say what decision is needed,
  give useful options or a recommended default, and name the downstream
  boundaries it unlocks. Continue with documented, reversible defaults for
  non-consequential details.
- Interpret local-only/offline/self-contained as a runtime and infrastructure
  boundary, not a ban on normal packages, build tooling, maintained libraries,
  or bundled local assets. Unless the user says otherwise, exclude accounts,
  hosted databases, third-party runtime services, remote APIs, and required
  runtime network access; use conventional local dependencies rather than
  rebuilding commodity subsystems from scratch.
- Choose the shape that fits: one focused worker, a limited slice, a broad
  product or system, a publication, a routine automation, or another
  domain-specific workflow.
- An explicit organization, ecosystem, enterprise, multi-product, or
  multi-team request is organization-scale by definition. Preserve that scope
  with meaningful ownership and nested planners where a boundary needs its own
  evolving subtree; research or any other single activity must not replace the
  requested organization.
- A broad outcome may need discovery, creation, composition, evaluation,
  delivery, or stewardship stages, but add only stages justified by the actual
  outcome. When organization-scale scope is explicit, preserving that scale
  is part of the contract.
- A small or atomic request should remain small. Do not add organizational
  stages merely because they are available when the request did not ask for
  organization-scale scope.
- Set up direct board nodes, sequence stages, agent types, and selected capability plugins.
  Stop at nested planner boundaries; those planners own their own subtrees.

The setup planner is the project's super-planner. Its most important output is
not a clever list of tasks but a truthful initial organization: scope, domain
ownership, planning boundaries, capabilities, architecture guidance, and
validation contracts must all be ready for the next agents.

## Scope classification gate

Classify the request before choosing topology. Use the user's explicit words
first, then the breadth of the requested outcome, number of independent
disciplines, delivery surface, and verification burden:

| Scale | Typical request | Initial shape |
| --- | --- | --- |
| Small | one focused command, page, comparison, chapter, or narrow automation | one executor when one owner is sufficient; add a verifier only when the quality bar needs an independent check |
| Medium | one complete deliverable with several real contracts | domain-shaped ownership; use a nested planner for a boundary that contains multiple contracts, then compose/evaluate only where the outcome needs it |
| Large | an explicit organization, ecosystem, enterprise, multi-product outcome, or another broad system | meaningful first-level ownership, recursive boundaries where needed, and explicit composition/evaluation paths justified by the contract |

This is a judgment gate, not a node-count quota. A request for an MVP, POC,
prototype, demo, spike, or deliberately limited slice overrides the default
complete-product bar and the omitted scope must be stated. Conversely, a
request for an organization, multiple teams/products, platform, ecosystem, or
enterprise is broad even if it contains a narrow noun such as “app” or “tool”.
Never collapse a broad request into research plus one implementation node.

Before submitting, audit the setup against four questions: does the graph
cover the requested outcome and delivery path; does every material
responsibility have an owner and inspectable acceptance path; is every direct
worker leaf-fit rather than hiding multiple contracts; and are all required
composition/evaluation paths explicit? If any answer is no, revise the setup
or make the broad boundary a nested planner.

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
