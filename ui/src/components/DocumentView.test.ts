import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { DocumentCapabilities, DocumentLinks, orderDocumentNodes } from "./DocumentView";
import type { DocumentRef, Edge, GraphNode } from "../domain";

const node = (id: string, parent_id: string | null): GraphNode =>
  ({
    id,
    project_id: "root",
    parent_id,
    objective: id,
    project_name: null,
    generated_prompt: null,
    repo_path: null,
    executor: "codex",
    agent: null,
    verification: null,
    status: "COMPLETE",
    paused: false,
    auto_run: false,
    run_policy: null,
    required_inputs: [],
    resource_refs: [],
    document_refs: [],
    artifact_refs: [],
    created_at: "",
    updated_at: "",
    progress: null,
    agent_state: null,
    agent_message: null,
    ui_state: "complete",
    allowed_actions: [],
    state_reason: null,
    generation_active: false,
  }) as GraphNode;

const edge = (src: string, dst: string): Edge => ({
  id: `${src}-${dst}`,
  src,
  dst,
  type: "DEPENDS_ON",
  created_at: "",
});

describe("document specification ordering", () => {
  it("keeps unresolved references visible for diagnosis", () => {
    const reference: DocumentRef = {
      ref: "future.md",
      title: null,
      media_type: null,
      imports: [],
    };

    const rendered = renderToStaticMarkup(
      createElement(DocumentLinks, { refs: [reference], projectId: "root" }),
    );

    expect(rendered).toContain("future.md");
    expect(rendered).toContain("/api/projects/root/documents/future.md");
  });

  it("shows clickable skills and MCP sources in document nodes", () => {
    const capabilityNode = node("build", "root");
    capabilityNode.agent = {
      type_id: "verifier",
      skill_ids: ["turn-verifying", "https://raw.example.test/game/SKILL.md"],
      mcp_servers: [{
        name: "playwright",
        source_url: "https://github.com/microsoft/playwright-mcp",
        transport: "stdio",
        command: "npx",
        args: [],
        url: null,
        env: {},
        headers: {},
        bearer_token_env_var: null,
        enabled: true,
      }],
    } as unknown as NonNullable<GraphNode["agent"]>;

    const rendered = renderToStaticMarkup(
      createElement(DocumentCapabilities, { node: capabilityNode }),
    );

    expect(rendered).toContain("Skills");
    expect(rendered).toContain("turn-verifying");
    expect(rendered).toContain("game");
    expect(rendered).toContain("/api/skills/turn-verifying");
    expect(rendered).toContain("MCP servers");
    expect(rendered).toContain("playwright");
    expect(rendered).toContain("https://github.com/microsoft/playwright-mcp");
  });

  it("puts explicit prerequisites above their integrator", () => {
    const nodes = [
      node("root", null),
      node("integrate", "root"),
      node("world", "root"),
      node("runtime", "root"),
    ];
    expect(orderDocumentNodes(nodes, [edge("world", "integrate"), edge("runtime", "integrate")], "root").map((item) => item.id)).toEqual([
      "world",
      "runtime",
      "integrate",
    ]);
  });

  it("keeps nested work under its containment heading", () => {
    const nodes = [node("root", null), node("narrative", "root"), node("choices", "narrative")];
    expect(orderDocumentNodes(nodes, [], "root").map((item) => item.id)).toEqual(["narrative"]);
    expect(orderDocumentNodes(nodes, [], "narrative").map((item) => item.id)).toEqual(["choices"]);
  });

  it("projects a verifier below its dependency without changing graph semantics", () => {
    const nodes = [
      node("root", null),
      node("implementation", "root"),
      node("verification", "root"),
      node("integrator", "root"),
    ];
    nodes[2].agent = { type_id: "verifier" } as NonNullable<GraphNode["agent"]>;
    const edges = [edge("implementation", "verification"), edge("verification", "integrator")];

    expect(orderDocumentNodes(nodes, edges, "root").map((item) => item.id)).toEqual([
      "implementation",
      "integrator",
    ]);
    expect(orderDocumentNodes(nodes, edges, "implementation").map((item) => item.id)).toEqual([
      "verification",
    ]);
  });
});
