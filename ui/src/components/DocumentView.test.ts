import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ArtifactLinks,
  DocumentCapabilities,
  DocumentLinks,
  DocumentView,
  GraphSourceDocument,
  liveGraphSource,
  SubgraphLinks,
  SubmissionSummary,
  WorkflowSourceLinks,
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
  provides: [],
  consumes: [],
  outputs: {},
  route_taken: null,
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
  type: "FOLLOWS",
  route: null,
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

  it("exposes workflow sources as navigable work-breakdown links", () => {
    const reference: SubgraphRef = {
      ref: ".turn/graphs/branch.json",
      title: "Branch graph",
      media_type: "application/json",
      managed: false,
    };
    const rendered = renderToStaticMarkup(
      createElement(SubgraphLinks, { refs: [reference], projectId: "root" }),
    );

    expect(rendered).toContain("Workflow source");
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

  it("projects a verifier after its sequence stage without changing graph semantics", () => {
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

  it("keeps the live composed work visible beside its source link", () => {
    const root = node("root", null);
    root.project_name = "Composed project";
    root.generated_prompt = "Stale planner receipt";
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
    expect(rendered).toContain("nested implementation");
    expect(rendered).toContain("Project goal");
    expect(rendered).toContain("Stale planner receipt");
    expect(rendered).toContain("work-specification");
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

  it("presents a step as instructions, result, then generated files", () => {
    const root = node("root", null);
    root.project_name = "Readable project";
    const child = node("Write the result", "root");
    child.generated_prompt = "Write the final project summary.";
    child.artifact_refs = ["submission", "output"];
    const rendered = renderToStaticMarkup(
      createElement(DocumentView, {
        nodes: [root, child],
        edges: [],
        artifacts: [
          {
            id: "submission",
            node_id: "child",
            kind: "json",
            name: "result-submission",
            content: { outcome: "COMPLETE", summary: "Finished the summary." },
            ref: null,
            created_at: "",
          },
          {
            id: "output",
            node_id: "child",
            kind: "file",
            name: "summary.md",
            content: null,
            ref: "docs/summary.md",
            created_at: "",
          },
        ],
        projectId: "root",
      }),
    );

    expect(rendered.indexOf("Instructions")).toBeLessThan(rendered.indexOf("Result"));
    expect(rendered.indexOf("Result")).toBeLessThan(rendered.indexOf("Generated files"));
    expect(rendered).toContain("Finished the summary.");
    expect(rendered).toContain("summary.md");
    expect(rendered).not.toContain("result-submission");
  });

  it("parses and orders recursively composed graph sources", () => {
    const source = parseGraphSource({
      project_name: "Root graph",
      nodes: [
        { key: "integrate", objective: "Integrate", follows: ["world"], parent_key: null },
        { key: "world", objective: "World", parent_key: null },
        { key: "verify", objective: "Verify world", follows: ["author"], parent_key: "world" },
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

  it("projects the live node objective into the workflow graph document", () => {
    const root = node("root", null);
    root.project_name = "Live project";
    root.generated_prompt = "Old planner receipt";
    const child = node("current objective", "root");
    child.generated_prompt = "Only write the current artifact.";

    const source = liveGraphSource([root, child], [edge("root", "current objective")], []);

    expect(source.project_name).toBe("Live project");
    expect(source.nodes.find((item) => item.key === "root")?.generated_prompt).toBeNull();
    expect(source.nodes.find((item) => item.key === "current objective")?.objective).toBe("current objective");
    expect(source.nodes.find((item) => item.key === "current objective")?.generated_prompt).toBe(
      "Only write the current artifact.",
    );
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

    expect(rendered).toContain("Generated files");
    expect(rendered).toContain("document-artifact-links");
    expect(rendered).toContain("/api/projects/root/documents/ARCHITECTURE.md");
    expect(rendered).toContain('target="_blank"');
  });

  it("turns submission receipts into a useful result summary", () => {
    const rendered = renderToStaticMarkup(
      createElement(SubmissionSummary, {
        artifacts: [{
          kind: "json",
          name: "result-submission",
          ref: null,
          content: { outcome: "COMPLETE", summary: "finished" },
        }],
      }),
    );

    expect(rendered).toContain("Result");
    expect(rendered).toContain("complete");
    expect(rendered).toContain("finished");
    expect(rendered).not.toContain("result-submission");
    expect(rendered).not.toContain("View response");
  });

  it("keeps duplicate basenames distinguishable by their full artifact paths", () => {
    const rendered = renderToStaticMarkup(
      createElement(ArtifactLinks, {
        projectId: "root",
        artifacts: [{
          kind: "file",
          name: "idea.json",
          ref: "cycles/first/idea.json",
        }, {
          kind: "file",
          name: "idea.json",
          ref: "cycles/second/idea.json",
        }],
      }),
    );

    expect(rendered.match(/data-artifact-ref=/g)).toHaveLength(2);
    expect(rendered).toContain("cycles/first/idea.json");
    expect(rendered).toContain("cycles/second/idea.json");
  });

  it("keeps workflow exploration available without making it the main document", () => {
    const rendered = renderToStaticMarkup(
      createElement(WorkflowSourceLinks, {
        refs: [{
          ref: ".turn/graphs/planner.json",
          title: "planner.json",
          media_type: "application/json",
          managed: false,
        }],
        projectId: "root",
        onOpenDocument: () => undefined,
      }),
    );

    expect(rendered).toContain("Explore workflow source");
    expect(rendered).toContain("planner.json");
    expect(rendered).not.toContain("Composed subgraphs");
    expect(rendered).not.toContain("<details open");
  });

  it("renders generated image references inside the document reader", () => {
    const root = node("root", null);
    root.artifact_refs = ["image-artifact"];
    const rendered = renderToStaticMarkup(
      createElement(DocumentView, {
        nodes: [root],
        edges: [],
        artifacts: [{
          id: "image-artifact",
          node_id: "root",
          kind: "file",
          name: "architecture.png",
          ref: "docs/architecture.png",
          content: null,
          created_at: "",
        }],
        projectId: "root",
      }),
    );

    expect(rendered).toContain("architecture.png");
    expect(rendered).toContain("/api/projects/root/documents/docs/architecture.png");
    expect(rendered).toContain("document-image-preview");
  });

  it("opens an image artifact in the simple document reader", () => {
    const root = node("root", null);
    const previousHash = window.location.hash;
    window.history.replaceState(null, "", "#document=docs%2Farchitecture.png");
    try {
      const rendered = renderToStaticMarkup(
        createElement(DocumentView, {
          nodes: [root],
          edges: [],
          artifacts: [],
          projectId: "root",
        }),
      );
      expect(rendered).toContain("document-reader-image");
      expect(rendered).toContain("/api/projects/root/documents/docs/architecture.png");
      expect(rendered).not.toContain("Loading document");
    } finally {
      window.history.replaceState(null, "", previousHash || "/");
    }
  });
});
