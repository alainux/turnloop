import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ArtifactLinks,
  DocumentCapabilities,
  DocumentLinks,
  DocumentView,
  GraphSourceDocument,
  SubgraphLinks,
  orderDocumentNodes,
  orderGraphSourceNodes,
  parseGraphSource,
} from "./DocumentView";
import type { DocumentRef, Edge, GraphNode, SubgraphRef } from "../domain";

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
    capability_status: [],
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

  it("exposes composed graph sources as navigable work-breakdown links", () => {
    const reference: SubgraphRef = {
      ref: ".turn/graphs/branch.json",
      title: "Branch graph",
      media_type: "application/json",
      managed: false,
    };
    const rendered = renderToStaticMarkup(
      createElement(SubgraphLinks, { refs: [reference], projectId: "root" }),
    );

    expect(rendered).toContain("Composed subgraphs");
    expect(rendered).toContain("Branch graph");
    expect(rendered).toContain("/api/projects/root/documents/.turn/graphs/branch.json");
  });

  it("shows clickable capability plugins and component counts in document nodes", () => {
    const capabilityNode = node("build", "root");
    capabilityNode.agent = { type_id: "verifier", capabilities: ["secret-word"] } as unknown as NonNullable<GraphNode["agent"]>;
    capabilityNode.capability_status = [{ capability_id: "secret-word", skills: 1, mcps: 1, loaded: true, installed: false }];

    const rendered = renderToStaticMarkup(
      createElement(DocumentCapabilities, { node: capabilityNode }),
    );

    expect(rendered).toContain("Capabilities");
    expect(rendered).toContain("secret-word (1/1)");
    expect(rendered).toContain("/api/capability-catalog/secret-word");
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

  it("keeps composed work behind its source link in the default document", () => {
    const root = node("root", null);
    root.project_name = "Composed project";
    root.subgraph_refs = [{
      ref: ".turn/graphs/root.json",
      title: "Root graph",
      media_type: "application/json",
      managed: false,
    }];
    const child = node("nested implementation", "root");

    const rendered = renderToStaticMarkup(
      createElement(DocumentView, {
        nodes: [root, child],
        edges: [],
        artifacts: [],
        projectId: "root",
      }),
    );

    expect(rendered).toContain("Root graph");
    expect(rendered).not.toContain("nested implementation");
    expect(rendered).not.toContain("work-specification");
  });

  it("keeps inline work breakdowns for graphs without a composed source", () => {
    const root = node("root", null);
    root.project_name = "Inline project";
    const child = node("inline implementation", "root");

    const rendered = renderToStaticMarkup(
      createElement(DocumentView, {
        nodes: [root, child],
        edges: [],
        artifacts: [],
        projectId: "root",
      }),
    );

    expect(rendered).toContain("inline implementation");
    expect(rendered).toContain("work-specification");
    expect(rendered).not.toContain("<details open");
  });

  it("parses and orders recursively composed graph sources", () => {
    const source = parseGraphSource({
      project_name: "Root graph",
      nodes: [
        { key: "integrate", objective: "Integrate", depends_on: ["world"], parent_key: null },
        { key: "world", objective: "World", parent_key: null },
        { key: "verify", objective: "Verify world", depends_on: ["author"], parent_key: "world" },
        { key: "author", objective: "Author world", parent_key: "world" },
      ],
    });

    expect(orderGraphSourceNodes(source.nodes, source.edges, null).map((item) => item.key)).toEqual([
      "world",
      "integrate",
    ]);
    expect(orderGraphSourceNodes(source.nodes, source.edges, "world").map((item) => item.key)).toEqual([
      "author",
      "verify",
    ]);
  });

  it("renders graph work breakdowns with artifacts and recursive graph links", () => {
    const source = parseGraphSource({
      project_name: "Narrative graph",
      notes: "The narrative boundary.",
      artifacts: [{ kind: "file", name: "NARRATIVE.md", ref: "docs/NARRATIVE.md" }],
      nodes: [{
        key: "chapter",
        objective: "Author chapter",
        generated_prompt: "Write the chapter contract.",
        subgraph_refs: [{ ref: ".turn/graphs/chapter.json", title: "Chapter graph" }],
        artifacts: [{ kind: "file", name: "chapter.md", ref: "docs/chapter.md" }],
      }],
    });

    const rendered = renderToStaticMarkup(
      createElement(GraphSourceDocument, {
        source,
        reference: {
          ref: ".turn/graphs/narrative.json",
          title: "Narrative graph",
          media_type: "application/json",
          imports: [],
          kind: "graph",
        },
        projectId: "root",
        contextNodes: [],
        stateArtifacts: [],
        onOpenDocument: () => undefined,
        onBack: () => undefined,
      }),
    );

    expect(rendered).toContain("Graph work breakdown");
    expect(rendered).toContain("The narrative boundary.");
    expect(rendered).toContain("Author chapter");
    expect(rendered).toContain("NARRATIVE.md");
    expect(rendered).toContain("docs/chapter.md");
    expect(rendered).toContain("Chapter graph");
    expect(rendered).not.toContain("<details open");
  });

  it("keeps artifact documentation in the same navigable document", () => {
    const rendered = renderToStaticMarkup(
      createElement(ArtifactLinks, {
        projectId: "root",
        artifacts: [
          { kind: "file", name: "ARCHITECTURE.md", ref: "ARCHITECTURE.md" },
          { kind: "file", name: "main.ts", ref: "src/main.ts" },
        ],
      }),
    );

    expect(rendered).toContain("Artifacts");
    expect(rendered).toContain("document-artifact-links");
    expect(rendered).toContain("/api/projects/root/documents/ARCHITECTURE.md");
    expect(rendered).toContain('target="_blank"');
  });

  it("makes submission responses expandable in the document", () => {
    const rendered = renderToStaticMarkup(
      createElement(ArtifactLinks, {
        projectId: "root",
        artifacts: [{
          kind: "json",
          name: "result-submission",
          ref: null,
          content: { outcome: "COMPLETE", summary: "finished" },
        }],
      }),
    );

    expect(rendered).toContain("View response");
    expect(rendered).toContain("COMPLETE");
    expect(rendered).toContain("finished");
    expect(rendered).not.toContain("<details open");
  });
});
