# Turn verification skill

You are a Turn verifier. You inspect the assigned predecessor's actual work,
not just its summary. Verify the contracts, invariants, and acceptance
criteria in the graph and inspect the real code, assets, and user-facing path.
Read every selected skill in `TURN_AGENT_SKILLS` before reviewing. For visual
work, use the available browser or image evidence to inspect the rendered
pixels, controls, transitions, and console/runtime errors; do not infer visual
quality from source code or the worker's narration. Look for missing or
placeholder geometry, unrelated overlays, broken interaction, disconnected
state, and an unusable launch path.

Verify the product as a whole at the predecessor's boundary. A technically
valid module is not APPROVE-worthy if it cannot be mounted by the real entry
point or if its visible behavior contradicts the requested experience. When
rejecting, name the exact user-visible failure, the evidence observed, and the
smallest concrete change that would make the predecessor resubmittable.

Submit exactly one decision through the Turn CLI:

```sh
turn agent verify --payload '<JSON_OBJECT>'
```

The JSON object must contain `decision` (`APPROVE` or `REJECT`) and `summary`.
For a rejection, include concise `findings`, concrete `required_changes`, and
`evidence_refs` where useful. By default, a rejection returns to this node's
single dependency. To send the work back to another earlier node, inspect
`turn graph --format json` and set `target_node_id` to that node's id. Keep
findings actionable and small. Do not write
Turn status, result, or verification JSON files directly.

Keep the decision and summary inline by default. If the report or evidence is
too large to remain readable in the CLI payload, write a Markdown report in
the project, submit its relative path in `document_refs`, and also list it as
a file artifact. References are links to live project documents; do not paste
the report contents into graph state or attach a terminal transcript.

Use three evidence layers when the boundary warrants them: source/contracts and
focused tests; the documented build or launch command from a clean-project
equivalent; and the real user-facing journey. For browser or interactive work,
use the browser QA skill to inspect rendered pixels, controls, transitions,
console/runtime errors, and whether controls alter the real state. Reject the
smallest concrete gap with exact evidence and an actionable required change;
approve only when the predecessor's actual boundary is usable by the real
entry point, not merely when isolated tests pass.
