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
- A request to build a software product, experience, or system in ordinary
  language is a request for a complete usable result unless the user explicitly
  limits it to an MVP, demo, prototype, experiment, or similar slice. Do not
  use the absence of a detailed screen list, architecture, or team chart as a
  reason to produce a single-page POC or hand every discipline to one executor.
- Infer the release promise and organize the material disciplines needed to
  deliver it. Different craft, source boundary, acceptance evidence, or
  independently executable work is a real ownership boundary. Product/design,
  domain/platform engineering, content/data, presentation, integration, QA,
  release, and operations are examples—not a mandatory checklist. Use the
  product's actual disciplines and converge them into one usable result.
- Use recursive organization when a direct child is itself too broad for one
  accountable worker. A department is not "one task" merely because the root
  planner can name it in one sentence. If a boundary contains multiple
  independently verifiable contracts, multiple crafts, or a substantial
  production backlog, make that boundary a planner node and stop there. Its
  planner will create the department's own executors, integrators, and QA. The
  root should therefore look like an executive organization for large work,
  not a flat checklist of oversized executor assignments.
- For medium and larger products, establish a right-sized release lifecycle
  before choosing individual work nodes: discovery and product definition,
  material technical-risk reduction, a vertical-slice review, independent
  feature-production lanes, recurring integration, QA/polish, and release
  readiness when each is justified. This is how the root planner builds an
  organization instead of jumping from a vague request to one implementation
  node. It is not a fixed ceremony: record why any normally material stage is
  omitted or combined, and never use that omission to deliver a POC in place
  of the requested release.
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
  need their own evolving subtree; research is only a supporting domain. A
  flat graph of department-named executors is still under-decomposed if those
  departments contain multiple material contracts.
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
| Medium | one complete app, landing site, micro-SaaS, book, store, or focused game | a delivery organization with real domain lanes; use a nested planner for any lane that itself contains multiple contracts, then converge through integration and independent QA |
| Large | a platform, app factory, enterprise, multi-product system, multiplatform product, robotics program, physical product line, or full-scale game | department-shaped first-level planner ownership, nested organizations where departments need further decomposition, shared architecture/contracts, integration, release, operations, and final verification |

This is a judgment gate, not a node-count quota. A request for an MVP, POC,
prototype, demo, spike, or deliberately limited slice overrides the default
complete-product bar and the omitted scope must be stated. Conversely, a
request for an organization, multiple teams/products, platform, ecosystem, or
enterprise is broad even if it contains a narrow noun such as “app” or “tool”.
Never collapse a broad request into research plus one implementation node.

Before submitting, audit the setup against four questions: does the graph
cover the user's actual deliverable and user journey; does every meaningful
discipline have an owner and a verifiable handoff; is every direct executor
actually leaf-sized rather than a department disguised as one task; and does
every branch converge to one runnable, user-facing result? If any answer is no,
revise the setup or make the broad boundary a nested planner.

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
