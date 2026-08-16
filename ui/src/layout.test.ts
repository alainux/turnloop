import { describe, expect, it } from "vitest";
import {
  displayEdges,
  GRAPH_PADDING,
  layoutDendrogram,
  NODE_HEIGHT,
  NODE_WIDTH,
  pathBetween,
} from "./layout";
import type { Edge, GraphNode } from "./domain";
const node = (id: string, parent_id: string | null): GraphNode => ({
  id,
  parent_id,
  project_id: "root",
  objective: id,
  project_name: null,
  generated_prompt: null,
  repo_path: null,
  executor: null,
  agent: null,
  verification: null,
  status: "RUNNABLE",
  run_policy: null,
  ui_state: "ready",
  state_reason: null,
  agent_state: null,
  agent_message: null,
  allowed_actions: ["run"],
  generation_active: false,
  paused: false,
  auto_run: false,
  required_inputs: [],
  resource_refs: [],
  document_refs: [],
  artifact_refs: [],
  progress: null,
  created_at: "",
  updated_at: "",
});
describe("dendrogram", () => {
  it("centers parents and uses orthogonal paths", () => {
    const layout = layoutDendrogram([
      node("root", null),
      node("a", "root"),
      node("b", "root"),
    ]);
    const root = layout.positions.get("root")!,
      a = layout.positions.get("a")!,
      b = layout.positions.get("b")!;
    expect(root.y).toBe((a.y + b.y) / 2);
    expect(root.x).toBeLessThan(a.x);
    expect(pathBetween(root, a)).toMatch(/^M.*H.*V.*H/);
    expect(pathBetween(a, b, "DEPENDS_ON")).toMatch(/^M.*H.*V.*H/);
    expect(pathBetween(root, a)).toContain("272");
  });

  it("keeps edge endpoints aligned with padded node boundaries", () => {
    const layout = layoutDendrogram([node("root", null), node("child", "root")]);
    const root = layout.positions.get("root")!,
      child = layout.positions.get("child")!;
    const path = pathBetween(root, child);

    expect(path).toMatch(
      new RegExp(
        `^M${root.x + GRAPH_PADDING + NODE_WIDTH} ${root.y + GRAPH_PADDING + NODE_HEIGHT / 2}`,
      ),
    );
    expect(path.endsWith(`H${child.x + GRAPH_PADDING}`)).toBe(true);
  });

  it("orders sibling dependencies before their dependents", () => {
    const layout = layoutDendrogram(
      [node("root", null), node("dependent", "root"), node("prerequisite", "root")],
      [
        {
          id: "dependency",
          src: "prerequisite",
          dst: "dependent",
          type: "DEPENDS_ON",
          created_at: "",
        },
      ],
    );
    expect(layout.positions.get("prerequisite")!.x).toBeLessThan(
      layout.positions.get("dependent")!.x,
    );
    expect(layout.positions.get("prerequisite")!.y).toBe(
      layout.positions.get("dependent")!.y,
    );
  });

  it("keeps a verifier after its executor when dependency relations are duplicated", () => {
    const nodes = [
      node("root", null),
      node("executor", "root"),
      node("verifier", "root"),
    ];
    const edges: Edge[] = [
      ["root", "executor"],
      ["root", "verifier"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      {
        id: "executor-verifier-dependency",
        src: "executor",
        dst: "verifier",
        type: "DEPENDS_ON",
        created_at: "",
      },
      {
        id: "executor-verifier-dependency-duplicate",
        src: "executor",
        dst: "verifier",
        type: "DEPENDS_ON",
        created_at: "",
      },
    );

    const shown = displayEdges(nodes, edges);
    expect(
      shown.filter((edge) => edge.type === "DEPENDS_ON").map((edge) => `${edge.src}->${edge.dst}`),
    ).toEqual(["executor->verifier"]);
    const layout = layoutDendrogram(nodes, edges);
    expect(layout.positions.get("verifier")!.depth).toBeGreaterThan(
      layout.positions.get("executor")!.depth,
    );
    expect(layout.positions.get("verifier")!.y).toBe(
      layout.positions.get("executor")!.y,
    );
  });

  it("removes transitive workflow shortcuts from the dendrogram", () => {
    const nodes = [
      node("root", null),
      node("first", "root"),
      node("left", "root"),
      node("right", "root"),
      node("end", "root"),
    ];
    const edges: Edge[] = [
      "first",
      "left",
      "right",
      "end",
    ].map((id) => ({
      id: `contains-${id}`,
      src: "root",
      dst: id,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      ...[
        ["first", "left"],
        ["first", "right"],
        ["left", "end"],
        ["right", "end"],
        ["first", "end"],
      ].map(([src, dst]) => ({
        id: `${src}-${dst}`,
        src,
        dst,
        type: "DEPENDS_ON" as const,
        created_at: "",
      })),
    );

    const shown = displayEdges(nodes, edges);
    const workflow = shown
      .filter((edge) => edge.type === "DEPENDS_ON")
      .map((edge) => `${edge.src}->${edge.dst}`);
    expect(workflow).toEqual([
      "first->left",
      "first->right",
      "left->end",
      "right->end",
    ]);
    expect(shown).not.toContainEqual(expect.objectContaining({ src: "root", dst: "end" }));
    expect(shown).not.toContainEqual(expect.objectContaining({ src: "first", dst: "end" }));
  });

  it("places a final integration after a nested branch completes", () => {
    const nodes = [
      node("root", null),
      node("branch", "root"),
      node("leaf", "branch"),
      node("branch-end", "branch"),
      node("final", "root"),
    ];
    const edges: Edge[] = [
      ["root", "branch"],
      ["branch", "leaf"],
      ["branch", "branch-end"],
      ["root", "final"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      {
        id: "leaf-branch-end",
        src: "leaf",
        dst: "branch-end",
        type: "DEPENDS_ON",
        created_at: "",
      },
      {
        id: "branch-final",
        src: "branch",
        dst: "final",
        type: "DEPENDS_ON",
        created_at: "",
      },
    );

    const layout = layoutDendrogram(nodes, edges);

    expect(layout.positions.get("final")!.x).toBeGreaterThan(
      layout.positions.get("branch-end")!.x,
    );
  });

  it("connects every nested branch output to a singular final product", () => {
    const nodes = [
      node("root", null),
      node("branch", "root"),
      node("first-output", "branch"),
      node("second-output", "branch"),
      node("final", "root"),
    ];
    const edges: Edge[] = [
      ["root", "branch"],
      ["branch", "first-output"],
      ["branch", "second-output"],
      ["root", "final"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push({
      id: "branch-final",
      src: "branch",
      dst: "final",
      type: "DEPENDS_ON",
      created_at: "",
    });

    const workflow = displayEdges(nodes, edges)
      .filter((edge) => edge.type === "DEPENDS_ON")
      .map((edge) => `${edge.src}->${edge.dst}`);

    expect(workflow).toEqual([
      "first-output->final",
      "second-output->final",
    ]);
    expect(workflow).not.toContain("branch->final");
  });

  it("places a singular final integration in the last stage", () => {
    const nodes = [
      node("root", null),
      node("branch-a", "root"),
      node("a-leaf", "branch-a"),
      node("branch-b", "root"),
      node("b-leaf", "branch-b"),
      node("integrate", "root"),
    ];
    const edges: Edge[] = [
      ["root", "branch-a"],
      ["branch-a", "a-leaf"],
      ["root", "branch-b"],
      ["branch-b", "b-leaf"],
      ["root", "integrate"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      ...["branch-a", "branch-b"].map((src) => ({
        id: `${src}-integrate`,
        src,
        dst: "integrate",
        type: "DEPENDS_ON" as const,
        created_at: "",
      })),
    );

    const layout = layoutDendrogram(nodes, edges);
    const finalDepth = layout.stageCount - 1;
    const finalStage = [...layout.positions.entries()]
      .filter(([, position]) => position.depth === finalDepth)
      .map(([id]) => id);

    expect(finalStage).toEqual(["integrate"]);
    expect(layout.positions.get("integrate")!.x).toBeGreaterThan(
      layout.positions.get("a-leaf")!.x,
    );
    expect(layout.positions.get("integrate")!.x).toBeGreaterThan(
      layout.positions.get("b-leaf")!.x,
    );
  });
});
