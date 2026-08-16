# Find MCP servers

Use this skill whenever a workflow node would benefit from an external tool,
service, data source, or application connector. MCP procurement is part of the
plan: research the smallest useful server, inspect its real documentation, and
assign it only to the nodes that need it.

Before selecting a server, identify the target worker's required capability and
compare it with the capabilities declared by that worker's harness. Do not
procure an MCP just to duplicate a harness-provided capability such as browser
or computer use. Select an MCP when it fills a missing capability, supplies a
specialized service or data source, or is explicitly requested. The declared
harness profile is intentionally small today and may later be replaced by
active capability discovery.

Search reputable MCP directories and the server's own documentation:

- MCP GitHub organization: https://github.com/mcp
- Glama MCP directory: https://glama.ai/mcp/servers
- PulseMCP: https://www.pulsemcp.com/
- Smithery: https://smithery.ai/

Prefer a maintained, domain-specific server over a broad catalog result. Read
the server documentation enough to identify its actual transport, launch
command or endpoint, and user-owned prerequisites such as API keys, OAuth,
desktop applications, package managers, or local permissions. Do not claim
that a server is installed, authenticated, or reachable: the user owns those
requirements.

For each selected server, add an object to the concrete node's
`mcp_servers` array:

```json
{
  "name": "context7",
  "source_url": "https://github.com/upstash/context7",
  "transport": "http",
  "url": "https://mcp.context7.com/mcp",
  "bearer_token_env_var": "CONTEXT7_API_KEY"
}
```

Use `transport: "stdio"` with `command` and `args` for local servers. Use
`transport: "configured"` when the user already manages that named server in
the selected harness and Turn should only carry the access assignment. Keep
credentials as environment-variable references, never literal secrets.

Record the source URL and the user-owned setup requirements in the relevant
architecture or research document. Assign the server to the smallest set of
workers that materially needs it; procurement is not a reason to expose every
connector to every agent. If no MCP improves the node, leave `mcp_servers`
empty rather than inventing a placeholder.
