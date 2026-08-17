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
the skill, and install it with `turn skills install <id>`. Otherwise use the
available tools to copy the external skill into `.turn/skills/<slug>/` and
reference only `project:<slug>` in the node's `skills` array. Never submit a
URL or a skill whose `SKILL.md` is not already present; do not paste the skill
body into the prompt.

Search in two passes: first look for domain and product guidance (for example
game design, accessibility, persistence, or API architecture), then look for
the delivery and QA guidance needed to prove the node's actual output. A
popular skill is not automatically a good skill. Prefer a maintained, specific
source whose instructions match the node's runtime, language, and acceptance
path. Record the direct source URL and the reason it was selected in the
architecture research notes so downstream workers can audit the choice.

If the search finds no suitable skill, normally use the role-base skill and
leave the node's additional `skills` array empty. Create a project-scoped
`.turn/skills/<slug>/SKILL.md` only when the project genuinely needs reusable
domain or method guidance that will help an agent beyond the current
assignment. It must have YAML frontmatter with `name` and `description`, and
contain only that reusable guidance. Never copy the user's project prompt,
node objective, generated_prompt, graph, acceptance criteria, or one-off work
instructions into a skill. A project skill is referenced by `project:<slug>`
in the node's `skills` array; it is not a Turn status or result handoff.

Record the sources actually consulted as direct URLs in the relevant project
document. Do not claim research that was not performed.

Record a selected skill reference in a concrete node's `skills` array when the
research finds guidance that materially improves that node. If the search
finds no applicable guidance, the role-base skill is enough; do not invent a
placeholder reference just to fill the field. A skill is guidance, not a
substitute for the node's objective, contracts, acceptance criteria, or
evidence. Agents receive those assignment details directly and can inspect
the live graph and prerequisite outputs. Choose by fit, not by popularity or
a broad name alone.

When a source is selected, resolve it to the standard Markdown source before
the plan is submitted. A GitHub skill may be a directory containing
`SKILL.md`, `references/`, `scripts/`, or `assets/`; a skills.sh result may
return the same file tree. Never save an HTML catalog page as a skill. Verify
that every selected skill has a local `SKILL.md` before submitting.

After the plan is submitted, each worker reads the planner-installed skill from
`TURN_AGENT_SKILL_ROOT` or the paths in `TURN_AGENT_SKILLS`; skill bodies are
not appended to the initial prompt. Assign the smallest set that materially
improves the worker's decision quality and do not use a skill as a reason to
create another planner.
