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
  artifact_refs: [],
  progress: null,
  created_at: "",
  updated_at: "",
  needs_review: false,
  merge_accepted: false,
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
    expect(layout.positions.get("prerequisite")!.y).toBeLessThan(
      layout.positions.get("dependent")!.y,
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
});
