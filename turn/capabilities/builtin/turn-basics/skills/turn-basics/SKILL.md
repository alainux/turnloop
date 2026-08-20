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

Turn is an adaptive workflow planner and execution system. It is best suited
to outcomes that are too large, cross-disciplinary, or quality-sensitive for
one agent: the graph is the shared plan and specification, and the project
directory is shared state. It also supports genuinely small requests. Do not
inflate an MVP or a focused task into an organization merely because Turn can
model one.

The graph is inspectable at every boundary. Project information explains the
root intent, persisted role defaults, loaded capabilities, and harnesses;
nodes explain ownership, instructions, status, sequence, inputs, and outputs;
artifacts and document references are the durable evidence that downstream
agents consume. The terminal is a live workspace: the user can inspect it and
type follow-up direction while your node is active.

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

Use the CLI for every action on the Turn server: status, plan, result,
verification, and trigger operations. The CLI is the only control-plane
interface. Project files are ordinary workspace files that agents may create,
edit, and inspect; editing one is not a substitute for invoking the CLI.
Never write Turn protocol state directly. Finish the assigned work, then use
the appropriate CLI command below to publish the handoff.
The runtime may classify a node as failed only when the harness cannot launch,
crashes, or exits with a non-zero code. If the work itself is unrecoverable,
publish `FAIL` explicitly through the CLI.

When your work is complete, submit exactly one JSON object through stdin. The
submission is the completion signal; do not print a fenced protocol block.

Execution result:

```sh
turn agent submit --kind result --stdin <<'TURN_PAYLOAD'
{"outcome":"COMPLETE","summary":"What happened","missing_inputs":[],"artifacts":[]}
TURN_PAYLOAD
```

Use `EXPAND` only when the node genuinely needs a child plan, `BLOCK` only for
an external human gate, and `FAIL` for an unrecoverable execution failure. For
an executor that discovers it is not leaf-fit, the safe adaptive form is one
nested planner child (`agent_type: "planner"`, `plan: true`); that planner then
authors the real descendant topology. Do not use `EXPAND` to smuggle a
hand-authored executor subgraph around the planning boundary.
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
explicitly assigned a planning boundary. The sole adaptive exception is the
one-child planner escalation above: it promotes an oversized leaf into a new
planning boundary without letting the executor decide that boundary's internal
organization.

## Triggers and events

Triggers are durable subscriptions that activate the immediately next node in
a workflow. An event trigger has an exact event name; a schedule trigger has a
classic five-field cron expression. Both have a target node and an enabled
state. Event data is arbitrary JSON. If
an event matches, Turn stores the full event envelope as the target node's
trigger context and includes it in `TURN_CONTEXT` when the agent starts; the
same activity is available in the project logs.

Events can come from persisted node transitions, agent plan/result/
verification submissions, schedules, or an explicit CLI event. Event names
are case-sensitive and are never fuzzy-matched. Agents and humans can queue a
custom event while the daemon is running. From a project directory, Turn
resolves the project id automatically; otherwise pass `--project-id`
explicitly. The payload must be a JSON object and is delivered unchanged in
the target node's trigger context:

```sh
turn trigger emit deployment.succeeded \
  --project-id PROJECT_ID \
  --data '{"environment":"staging"}'
```

Use the exact configured event name, including case. Emit only after the Turn
daemon is running; the command queues the event and returns an accepted event
record. Agents should use this CLI path for deliberate on-demand starts, while
transition, agent-action, and schedule events are emitted by the runtime.
Schedule triggers use classic five-field UTC cron only, such as `*/5 * * * *`;
interval forms such as `@every 5m` are not supported.

Configured trigger data is merged into the emitted event object without
discarding the event source's existing values. A schedule trigger's configured
data is merged with its scheduled timestamp and trigger id. The event is
workspace-wide, so a trigger in another project can respond to it. Common event names include `node.status.changed`, `project.completed`,
`agent.submitted`, `agent.plan.submitted`, `agent.result.submitted`,
`agent.verification.submitted`, and `verification.completed`. Event matching is
exact-name only. Keep trigger routing simple: normally target the workflow's
start node, use `project.completed` for a repeat loop, and use a named CLI
event for an on-demand organization start.

### Triggered nodes

`trigger_context` is input to the node run, not an instruction to recreate the
activation. Read its `event_name`, `source`, `occurred_at`, and `data` when the
work depends on why the node started. A node activated by an event must not emit
that same event as part of its own work: doing so creates an accidental
self-trigger loop. Emit a different, intentional follow-up event only when the
workflow contract calls for one. For tests, supply a trigger context or use a
direct local entrypoint; reserve emitting the activating event for a deliberate
end-to-end demonstration.
