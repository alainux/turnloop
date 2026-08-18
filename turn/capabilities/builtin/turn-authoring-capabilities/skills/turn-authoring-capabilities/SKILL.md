---
name: turn-authoring-capabilities
description: Create, inspect, validate, and load portable Turn capability plugins.
metadata:
  opencode/slash: "true"
---

# Turn capability authoring

Capability plugins are portable Agent Plugins v1 packages. Turn supports the
portable `skills/` and `mcp.json` component locations today.

## Find before creating

Search the local catalog first:

```sh
turn capabilities search "specific domain or workflow"
turn capabilities show <capability-id>
```

Inspect the complete package through the catalog API or the path shown by the
CLI. If no useful package exists, research a specific skill or MCP server
online. Good sources include:

- https://github.com/msitarzewski/agency-agents
- https://github.com/topics/agent-skills
- https://awesome-copilot.github.com/
- https://skills.sh/
- https://github.com/mcp
- https://glama.ai/mcp/servers
- https://www.pulsemcp.com/
- https://smithery.ai/

Prefer maintained, narrowly scoped sources. Verify the actual skill format,
MCP transport, command, runtime prerequisites, and licensing. Never claim that
credentials, binaries, or remote access are already available.

## Create a package

Create a directory with this exact shape:

```text
my-capability/
├── plugin.json
├── skills/
│   └── my-skill/
│       └── SKILL.md
└── mcp.json
```

`plugin.json` must use the Agent Plugins v1 schema and a lowercase id:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "my-capability",
  "version": "1.0.0",
  "description": "A concise description of the reusable capability."
}
```

Each `SKILL.md` needs YAML frontmatter with a lowercase `name` matching its
directory and a concrete `description`, followed by reusable instructions.
Keep one skill focused. Put reusable scripts, references, and assets beside its
`SKILL.md`; do not copy a one-off user prompt or graph state into a skill.

`mcp.json` must use the matching v1 MCP schema. Use `stdio` with a single
executable token and separate `args`, or `streamable-http`/`sse` with an
absolute URL. Package-local executable paths start with `./`; use
`${PLUGIN_ROOT}` and `${PLUGIN_DATA}` only where the portable spec allows it.
Never embed secrets in a plugin.

## Validate and load

Before submitting a plan, validate the package by showing it through Turn and
then load it:

```sh
turn capabilities show /absolute/path/to/my-capability
turn capabilities load /absolute/path/to/my-capability
turn capabilities load my-capability
```

The first command checks the authored directory. Loading a directory adds it
to Turn's local catalog. Loading an id copies that catalog package into the
current project's `.turn/capabilities/<id>` and performs no harness install.
Only submit ids that are loaded in the current project. In every plan node,
use a simple `capabilities` array, for example:

```json
{"capabilities": ["game-architecture", "turn-executing"]}
```

Turn rejects plan submissions that name a capability that is not loaded. The
selected harness installs a loaded package immediately before launch and
verifies the native project-level skill/MCP surface before starting the agent.
