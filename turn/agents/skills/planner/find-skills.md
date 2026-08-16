# Find skills

Use this skill for every broad product plan. Most real work needs domain
guidance, so the planner must search before deciding that a node needs no
specialized skill. Use it for executors, integrators, verifiers, and for the
planner's own architecture/research work. Skill selection is part of the plan,
not an optional afterthought.

Start with reputable sources and inspect the candidate before selecting it:

- Skills catalog: https://skills.sh/
- Agency Agents: https://github.com/msitarzewski/agency-agents
- Skill discovery CLI: `npx skills find <specific query>`
- GitHub agent-skill repositories: https://github.com/search?q=topic%3Aagent-skills&type=repositories

For each concrete node, search with a query that names its actual domain and
deliverable (for example `3D web game runtime`, `terminal game design`, or
`TypeScript architecture`). Inspect the candidate's scope and choose the
smallest useful match. Prefer a local library id when Turn already provides
the skill. Otherwise reference the direct HTTP(S) URL of the skill document in
that node's `skills` array. The server installs external references into the
current project's `turn/skills` directory before launching the agent; do not
paste the skill body into the prompt.

Search in two passes: first look for domain and product guidance (for example
game design, accessibility, persistence, or API architecture), then look for
the delivery and QA guidance needed to prove the node's actual output. A
popular skill is not automatically a good skill. Prefer a maintained, specific
source whose instructions match the node's runtime, language, and acceptance
path. Record the direct source URL and the reason it was selected in the
architecture research notes so downstream workers can audit the choice.

If the search finds no suitable skill, author a concise project-scoped
`turn/skills/<slug>/SKILL.md` before submitting the plan. It must have YAML
frontmatter with `name` and `description`, contain only reusable instructions,
and be referenced by `project:<slug>` in the node's `skills` array. This is a
real project deliverable, not a Turn status or result handoff.

Record the sources actually consulted in `architecture_spec.research_sources`
as direct URLs. Do not claim research that was not performed.

Record the selected skill reference in every concrete node's `skills` array.
If the search finds no applicable guidance, author a small project skill rather
than silently omitting the field. A skill is guidance, not a substitute for the
node's objective, contracts, acceptance criteria, or evidence. Choose by fit,
not by popularity or a broad name alone.

After the plan is submitted, each worker reads the materialized skill from
`TURN_AGENT_SKILL_ROOT` or the paths in `TURN_AGENT_SKILLS`; skill bodies are
not appended to the initial prompt. Assign the smallest set that materially
improves the worker's decision quality and do not use a skill as a reason to
create another planner.
