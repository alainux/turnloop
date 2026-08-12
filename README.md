# Turn

**Turn** is a live, editable workgraph. You give it a vague objective; it creates a
root node, invokes one initial planner, shows the generated graph immediately, and
begins executing every runnable node. The graph may contain decomposition trees,
linear sequences, parallel branches, and dependency joins. You can inspect, pause,
cancel, edit, retry, regenerate, or fork any node — and a blocked node tells you
exactly what input, account, credential, file, decision, or approval it is missing.

> Hosted at `turnloop.tech`.

---

## Core interaction

```
prompt → root node → initial planner → visible graph → execute ready leaves
       → expand or block as necessary → accept user edits/input → continue
```

The architecture succeeds when this loop works across *unrelated* objectives while
the kernel stays little more than **a versioned graph, a runner, and worker
adapters**.

---

## The four persistent primitives

| Primitive | Contains |
|-----------|----------|
| **Node**  | objective/prompt, parent, executor, status, required inputs, resource references, artifact references, revision + lineage |
| **Edge**  | a relationship between two nodes — only `CONTAINS` (decomposition / hierarchy / inherited context) or `DEPENDS_ON` (ordering / parallelism / joins) |
| **Run**   | one execution attempt for one node: worker, timestamps, logs, outcome, cancellation state |
| **Artifact** | any persistent input/output: text, structured data, user input, evidence, files, code changes, test results, links, credential references, external ids |

A **WorkGraph** is versioned Nodes + Edges. A **project** is a root node and
everything descended from it. The execution graph is kept acyclic; repeated or
ongoing work appends new nodes rather than creating cycles.

## The two operations

* **Plan** — the initial planner and any later decomposition use the *same*
  operation: *given this node, its ancestry, available artifacts, inherited
  resources, and available executors, produce the smallest useful workgraph that
  can begin executing now.* Decompose only far enough to expose concrete runnable
  work; use dependencies only where required; mark missing information explicitly
  instead of inventing it; assign each executable leaf an executor and resources.
* **Execute** — a runnable leaf is sent to its worker. Every worker returns
  exactly one outcome:
  * `COMPLETE` — return output artifacts
  * `EXPAND` — return child nodes + edges (decompose instead of pretending to finish)
  * `BLOCK` — return explicit missing requirements
  * `FAIL` — return an error + whether retry is appropriate

## Runtime behavior

A node is **runnable** when it is active, not paused, has no unsatisfied
dependencies, and has all required inputs. The runner continuously: finds runnable
nodes → starts their Runs → invokes workers → stores artifacts + outcomes → updates
node state → dispatches newly runnable nodes. Expanded nodes become containers whose
progress is derived from their descendants.

## Skills & resources

Resources are *context, not orchestration primitives*. A project or subtree may
contain local skills, instructions, docs, examples, or coding standards. Resources
attached to a parent are inherited by descendants unless overridden. Adding skills
adds data, not core code.

---

## Architecture / execution stack

| Concern | Choice |
|---------|--------|
| Backend language | **Python** |
| Schemas | **Pydantic** (strict Node / Edge / Run / Artifact / worker-result) |
| Authoritative store | **Postgres** (SQLite default for local runs; schema-identical) |
| Execution orchestration | **Prefect 3** behind a thin adapter (optional) |
| Software-engineering worker | **Codex SDK / `codex exec`** |
| Resource / tool boundary | **MCP** (optional) |

**Turn owns the workgraph and node state. Prefect does not.** One node Run is one
Prefect-managed execution, so Prefect handles retries / timeouts / cancellation /
scheduling / worker infra without leaking Prefect concepts into Turn's data model.
By default Turn runs workers directly (`TURN_EXECUTION_BACKEND=direct`); set
`prefect` to wrap each Run in a flow.

For a software node, the Codex worker receives the node objective, ancestor context,
project-local skills, repository state, constraints, and acceptance criteria. It
runs Codex in an **isolated Git worktree**, capturing its diff / commits / logs /
generated files as artifacts. A Turn subgraph is not inherently a Git worktree —
worktrees are only an execution mechanism for software branches.

## Editing & branching

* **Editing a node** creates a new revision (a snapshot artifact is stored) rather
  than destructively rewriting history.
* **Regenerate descendants** supersedes the existing downstream branch (old nodes
  are marked `CANCELLED` and remain inspectable), re-plans from the revised node,
  and begins executing the replacement branch. It does **not** pretend that
  irreversible external side effects were undone.
* **Fork from here** creates an alternative active branch with the same ancestral
  context; the original branch remains inspectable and may continue, pause, or be
  discarded.

---

## Run it

```bash
pip install -e .                 # core deps (sqlite + postgres drivers)
# optional: pip install -e ".[postgres,llm,prefect]"

# minimal local run (SQLite, deterministic heuristic planner + echo leaves)
TURN_DATABASE_URL="sqlite+aiosqlite:///./turnloop.db" \
TURN_PLANNER=heuristic \
TURN_DEFAULT_EXECUTOR=echo \
python -m turn
# open http://127.0.0.1:8000

# real run (Codex-backed planner + Codex workers, needs `codex` on PATH + auth)
python -m turn
```

Or with the helper script:

```bash
./scripts/run.sh                 # sqlite, heuristic planner, echo leaves
TURN_PLANNER=codex ./scripts/run.sh   # real Codex planning/execution
```

### Postgres (authoritative store)

```bash
./scripts/setup_postgres.sh     # starts Postgres, creates role + db, prints URL
export TURN_DATABASE_URL="postgresql+asyncpg://turn:turn@localhost:5432/turn"
python -m turn
```

`.env.example` lists every configurable variable.

---

## Project layout

```
turn/
  domain/schemas.py   # Node, Edge, Run, Artifact, PlanResult, WorkerResult (Pydantic)
  db/                 # async SQLAlchemy store (Postgres-ready, SQLite default)
  graph/logic.py      # runnability, ancestry, derived progress (pure)
  workers/            # base protocols, registry, planner, codex/shell/echo adapters
  runner/             # runner loop, event bus, optional Prefect adapter
  server/             # FastAPI REST + SSE, UI mount
  tests/test_slice.py # offline vertical-slice proof
ui/                   # single-page UI (prompt, live graph, node detail)
scripts/              # run.sh, setup_postgres.sh
```

## Minimal vertical slice (proven)

`turn/tests/test_slice.py` proves the smallest path with **zero external services**
(temp SQLite + deterministic workers):

```
prompt → root node → initial planner → visible graph → execute ready leaves
       → block (missing input) → provide input → complete
       → edit parent → regenerate descendants → execution resumes
```

Run it:

```bash
PYTHONPATH=. python turn/tests/test_slice.py
```

## Hard constraints (honored)

No custom workflow engine. No persistent agent organizations. No domain-specific
workflow classes. No large policy / meta-learning framework. No separate planners
per domain. No business / education / research / software concepts baked into the
kernel. Domain behavior arises from the root objective, the generated graph, the
selected workers, and the attached skills.
