---
name: turn-basics
description: The minimal Turn operating model, graph inspection, capability activation, and CLI handoff.
metadata:
  opencode/slash: "true"
---

# Turn basics

Turn is a project-local workgraph. You are one node in that graph. Your node
has an objective, a project directory, an agent role, and a set of activated
capabilities. Work in the assigned project directory and leave durable work
there for dependent nodes.

## First turn

1. Invoke every capability marker in the launch message through the harness's
   native mechanism. A marker such as `$turn-basics`, `/turn-basics`, or
   `/skill:turn-basics` is an activation request, not a file-reading hint.
2. Run `turn project info` from the project root when you need project identity,
   role defaults, loaded capabilities, or native harness discovery metadata.
3. Run `turn graph <project-id> --requester <node-id> --tree` before planning or
   changing work. Use `--format json` when exact node, sequence, run, prompt,
   artifact, or document data is needed.
4. Read preceding-stage artifacts from the project directory. A `FOLLOWS` edge
   means that stage runs before yours; do not ask the user to paste it.

Use the harness-native skill and MCP surfaces that Turn prepared. Do not read a
capability's `SKILL.md` to simulate activation and do not write directly to
`.turn/state.json`, result files, status files, or other Turn protocol state.

## Status and handoff

The installed `turn` command is the only control-plane interface:

```sh
turn agent status --state working --message "short current action"
```

When your work is complete, submit exactly one JSON object through stdin. The
submission is the completion signal; do not print a fenced protocol block.

Execution result:

```sh
turn agent submit --kind result --stdin <<'TURN_PAYLOAD'
{"outcome":"COMPLETE","summary":"What happened","missing_inputs":[],"artifacts":[]}
TURN_PAYLOAD
```

Use `EXPAND` only when the node genuinely needs a child plan, `BLOCK` only for
an external human gate, and `FAIL` for an unrecoverable execution failure.
Artifacts are small repo-relative files or directories created by the node;
do not list every changed file.

Planners submit `--kind plan` with a `PlanResult` object. Verifiers submit:

```sh
turn agent verify --stdin <<'TURN_PAYLOAD'
{"decision":"APPROVE","summary":"What was verified","findings":[],"required_changes":[],"evidence_refs":[],"target_node_id":null}
TURN_PAYLOAD
```

Keep reports concise. Use project Markdown plus `document_refs` when evidence
is too large for a handoff summary. Continue using the harness terminal for
ordinary work and keep the session available for follow-up when possible.

Use a short objective for every planned node (at most 72 characters). The
objective is the graph/card label; put detailed instructions in
`generated_prompt` or a project document.

Graph construction is a planner concern. Planners must use `$turn-planning` for
topology, ownership, and source-file handoffs; executors, integrators, and
verifiers inspect the graph but do not author or revise it unless they are
explicitly assigned a planning boundary.
