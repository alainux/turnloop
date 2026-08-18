import { describe, expect, it } from "vitest";
import {
  displayEdges,
  GRAPH_PADDING,
  layoutDendrogram,
  NODE_HEIGHT,
  NODE_WIDTH,
  returnPathBetween,
  workflowLeafIds,
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
  capability_status: [],
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
  it("shares the one-leaf composition invariant with the graph contract", () => {
    const nodes = [
      node("root", null),
      node("left", "root"),
      node("right", "root"),
      node("join", "root"),
    ];
    const edges: Edge[] = [
      ["root", "left"],
      ["root", "right"],
      ["root", "join"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      { id: "left-join", src: "left", dst: "join", type: "FOLLOWS", created_at: "" },
      { id: "right-join", src: "right", dst: "join", type: "FOLLOWS", created_at: "" },
    );

    expect(workflowLeafIds(nodes, edges).get("root")).toEqual(["join"]);
  });

  it("keeps cards in distinct layered ranks and routes containment to them", async () => {
    const layout = await layoutDendrogram(
      [node("root", null), node("a", "root"), node("b", "root")],
      ["a", "b"].map((id) => ({
        id: `root-${id}`,
        src: "root",
        dst: id,
        type: "CONTAINS" as const,
        created_at: "",
      })),
    );
    const root = layout.positions.get("root")!,
      a = layout.positions.get("a")!,
      b = layout.positions.get("b")!;
    expect(root.x).toBeLessThan(a.x);
    expect(a.x).toBe(b.x);
    expect(a.y).not.toBe(b.y);
    expect(layout.edgePaths.get("root-a")).toMatch(/^M.*H/);
  });

  it("keeps a branched workflow collision-free while routing every visible edge", async () => {
    const nodes = [
      node("root", null),
      node("left", "root"),
      node("right", "root"),
      node("left-output", "left"),
      node("right-output", "right"),
      node("final", "root"),
    ];
    const edges: Edge[] = [
      ["root", "left"],
      ["root", "right"],
      ["left", "left-output"],
      ["right", "right-output"],
      ["root", "final"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      { id: "left-final", src: "left", dst: "final", type: "FOLLOWS", created_at: "" },
      { id: "right-final", src: "right", dst: "final", type: "FOLLOWS", created_at: "" },
    );

    const shown = displayEdges(nodes, edges);
    const layout = await layoutDendrogram(nodes, edges);
    const rectangles = [...layout.positions.values()];
    for (let index = 0; index < rectangles.length; index += 1) {
      for (let next = index + 1; next < rectangles.length; next += 1) {
        const a = rectangles[index];
        const b = rectangles[next];
        const overlaps =
          a.x < b.x + NODE_WIDTH &&
          a.x + NODE_WIDTH > b.x &&
          a.y < b.y + NODE_HEIGHT &&
          a.y + NODE_HEIGHT > b.y;
        expect(overlaps).toBe(false);
      }
    }
    expect(layout.edgePaths.size).toBe(shown.length);
    expect([...layout.edgePaths.values()].every((path) => path.startsWith("M"))).toBe(true);
    expect(layout.positions.get("left")!.x).toBeLessThan(
      layout.positions.get("final")!.x,
    );
    expect(layout.positions.get("right")!.x).toBeLessThan(
      layout.positions.get("final")!.x,
    );
  });

  it("keeps edge endpoints aligned with padded node boundaries", async () => {
    const layout = await layoutDendrogram(
      [node("root", null), node("child", "root")],
      [{ id: "root-child", src: "root", dst: "child", type: "CONTAINS", created_at: "" }],
    );
    const root = layout.positions.get("root")!,
      child = layout.positions.get("child")!;
    const path = layout.edgePaths.get("root-child")!;

    expect(path).toMatch(
      new RegExp(
        `^M${root.x + GRAPH_PADDING + NODE_WIDTH} ${root.y + GRAPH_PADDING + NODE_HEIGHT / 2}`,
      ),
    );
    expect(path.endsWith(`H${child.x + GRAPH_PADDING}`)).toBe(true);
  });

  it("curves a return flow from the verifier back into the executor", () => {
    const verifier = { x: 278, y: 20, depth: 1 };
    const executor = { x: 0, y: 20, depth: 0 };
    const path = returnPathBetween(verifier, executor);

    expect(path).toMatch(/^M326 68Q299 44/);
    expect(path).toMatch(/272 68$/);
    expect(path).toContain("Q");
  });

  it("adapts return endpoints when the target is to the right", () => {
    const reviewer = { x: 0, y: 20, depth: 0 };
    const target = { x: 278, y: 20, depth: 1 };
    const path = returnPathBetween(reviewer, target);

    expect(path).toMatch(/^M272 68Q299 44/);
    expect(path).toMatch(/326 68$/);
  });

  it("orders sibling sequence stages left to right", async () => {
    const layout = await layoutDendrogram(
      [node("root", null), node("dependent", "root"), node("prerequisite", "root")],
      [
        {
          id: "sequence",
          src: "prerequisite",
          dst: "dependent",
          type: "FOLLOWS",
          created_at: "",
        },
      ],
    );
    expect(layout.positions.get("prerequisite")!.x).toBeLessThan(
      layout.positions.get("dependent")!.x,
    );
    expect(layout.edgePaths.get("sequence")).toMatch(/^M.*H/);
  });

  it("keeps a verifier after its executor when sequence relations are duplicated", async () => {
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
        id: "executor-verifier-sequence",
        src: "executor",
        dst: "verifier",
        type: "FOLLOWS",
        created_at: "",
      },
      {
        id: "executor-verifier-sequence-duplicate",
        src: "executor",
        dst: "verifier",
        type: "FOLLOWS",
        created_at: "",
      },
    );

    const shown = displayEdges(nodes, edges);
    expect(shown).toContainEqual(expect.objectContaining({
      src: "executor",
      dst: "verifier",
      type: "FOLLOWS",
    }));
    const layout = await layoutDendrogram(nodes, edges);
    expect(layout.positions.get("verifier")!.depth).toBeGreaterThan(
      layout.positions.get("executor")!.depth,
    );
    expect(
      layout.edgePaths.get("executor-verifier-sequence") ??
        layout.edgePaths.get("executor-verifier-sequence-duplicate"),
    ).toMatch(/^M.*H/);
  });

  it("renders a sequence diamond without inventing long-range shortcuts", () => {
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
      ].map(([src, dst]) => ({
        id: `${src}-${dst}`,
        src,
        dst,
        type: "FOLLOWS" as const,
        created_at: "",
      })),
    );

    const shown = displayEdges(nodes, edges);
    const workflow = shown
      .filter((edge) => edge.type === "FOLLOWS")
      .map((edge) => `${edge.src}->${edge.dst}`);
    expect(workflow).toEqual([
      "first->left",
      "first->right",
      "left->end",
      "right->end",
    ]);
  });

  it("uses the sequence handoff into a child instead of the ownership shortcut", () => {
    const nodes = [node("root", null), node("source", "root"), node("target", "root")];
    const edges: Edge[] = [
      { id: "root-source", src: "root", dst: "source", type: "CONTAINS", created_at: "" },
      { id: "root-target", src: "root", dst: "target", type: "CONTAINS", created_at: "" },
      { id: "source-target", src: "source", dst: "target", type: "FOLLOWS", created_at: "" },
    ];

    const shown = displayEdges(nodes, edges);

    expect(shown).not.toContainEqual(expect.objectContaining({
      src: "root",
      dst: "target",
      type: "CONTAINS",
    }));
    expect(shown).toContainEqual(expect.objectContaining({
      src: "source",
      dst: "target",
      type: "FOLLOWS",
    }));
  });

  it("places a final integration after a nested branch completes", async () => {
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
        type: "FOLLOWS",
        created_at: "",
      },
      {
        id: "branch-final",
        src: "branch",
        dst: "final",
        type: "FOLLOWS",
        created_at: "",
      },
    );

    const shown = displayEdges(nodes, edges);
    expect(shown.filter((edge) => edge.dst === "final")).toEqual([
      expect.objectContaining({ src: "branch-end", dst: "final" }),
    ]);
    const layout = await layoutDendrogram(nodes, edges);

    expect(layout.positions.get("final")!.x).toBeGreaterThan(
      layout.positions.get("branch-end")!.x,
    );
    for (const edge of shown) {
      const source = layout.positions.get(edge.src)!;
      const target = layout.positions.get(edge.dst)!;
      expect(target.x).toBeGreaterThan(source.x);
      expect(target.y).toBeGreaterThanOrEqual(source.y);
    }
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
      type: "FOLLOWS",
      created_at: "",
    });

    const workflow = displayEdges(nodes, edges)
      .filter((edge) => edge.type === "FOLLOWS")
      .map((edge) => `${edge.src}->${edge.dst}`);

    expect(workflow).toEqual([
      "first-output->final",
      "second-output->final",
    ]);
    expect(workflow).not.toContain("branch->final");
  });

  it("places a singular final integration in the last stage", async () => {
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
        type: "FOLLOWS" as const,
        created_at: "",
      })),
    );

    const layout = await layoutDendrogram(nodes, edges);
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

  it("lays out a nested sequence and diamond with horizontal ports and no backward rails", async () => {
    const nodes = [
      node("root", null),
      node("setup", "root"),
      node("branch", "root"),
      node("left", "branch"),
      node("right", "branch"),
      node("join", "branch"),
      node("final", "root"),
    ];
    const edges: Edge[] = [
      ["root", "setup"],
      ["root", "branch"],
      ["branch", "left"],
      ["branch", "right"],
      ["branch", "join"],
      ["root", "final"],
    ].map(([src, dst]) => ({
      id: `contains-${src}-${dst}`,
      src,
      dst,
      type: "CONTAINS" as const,
      created_at: "",
    }));
    edges.push(
      { id: "setup-branch", src: "setup", dst: "branch", type: "FOLLOWS", created_at: "" },
      { id: "left-join", src: "left", dst: "join", type: "FOLLOWS", created_at: "" },
      { id: "right-join", src: "right", dst: "join", type: "FOLLOWS", created_at: "" },
      { id: "branch-final", src: "branch", dst: "final", type: "FOLLOWS", created_at: "" },
    );

    expect(workflowLeafIds(nodes, edges).get("root")).toEqual(["final"]);
    expect(workflowLeafIds(nodes, edges).get("branch")).toEqual(["join"]);

    const shown = displayEdges(nodes, edges);
    expect(new Set(shown.map((edge) => `${edge.src}->${edge.dst}`))).toEqual(new Set([
      "root->setup",
      "setup->branch",
      "branch->left",
      "branch->right",
      "left->join",
      "right->join",
      "join->final",
    ]));

    const layout = await layoutDendrogram(nodes, edges);
    expect(layout.edgePaths.size).toBe(shown.length);
    for (const edge of shown) {
      const source = layout.positions.get(edge.src)!;
      const target = layout.positions.get(edge.dst)!;
      const sourceX = source.x + GRAPH_PADDING + NODE_WIDTH;
      const sourceY = source.y + GRAPH_PADDING + NODE_HEIGHT / 2;
      const targetX = target.x + GRAPH_PADDING;
      const targetY = target.y + GRAPH_PADDING + NODE_HEIGHT / 2;
      const path = layout.edgePaths.get(edge.id)!;

      expect(target.x).toBeGreaterThan(source.x);
      expect(target.y).toBeGreaterThanOrEqual(source.y);
      expect(path.startsWith(`M${sourceX} ${sourceY}H`)).toBe(true);
      expect(path.endsWith(`H${targetX}`)).toBe(true);
      if (sourceY !== targetY) {
        expect(path).toContain(`V${targetY}H${targetX}`);
      }
    }

    const segments = shown.flatMap((edge) => {
      const path = layout.edgePaths.get(edge.id)!;
      const routed = path.match(
        /^M(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)H(-?\d+(?:\.\d+)?)(?:V(-?\d+(?:\.\d+)?)H(-?\d+(?:\.\d+)?))?$/,
      );
      expect(routed).not.toBeNull();
      const [, x1, y1, x2, y2, x3] = routed!;
      const start = { x: Number(x1), y: Number(y1) };
      const first = { x: Number(x2), y: Number(y1) };
      if (y2 === undefined || x3 === undefined) {
        return [[start, first]];
      }
      const second = { x: Number(x2), y: Number(y2) };
      const end = { x: Number(x3), y: Number(y2) };
      return [[start, first], [first, second], [second, end]];
    });
    for (let index = 0; index < segments.length; index += 1) {
      for (let next = index + 1; next < segments.length; next += 1) {
        const [a, b] = segments[index];
        const [c, d] = segments[next];
        const aHorizontal = a.y === b.y;
        const cHorizontal = c.y === d.y;
        const crosses = aHorizontal === cHorizontal
          ? false
          : aHorizontal
            ? c.x > Math.min(a.x, b.x) &&
              c.x < Math.max(a.x, b.x) &&
              a.y > Math.min(c.y, d.y) &&
              a.y < Math.max(c.y, d.y)
            : a.x > Math.min(c.x, d.x) &&
              a.x < Math.max(c.x, d.x) &&
              c.y > Math.min(a.y, b.y) &&
              c.y < Math.max(a.y, b.y);
        expect(crosses).toBe(false);
      }
    }

    const rectangles = [...layout.positions.values()];
    for (let index = 0; index < rectangles.length; index += 1) {
      for (let next = index + 1; next < rectangles.length; next += 1) {
        const a = rectangles[index];
        const b = rectangles[next];
        expect(
          a.x >= b.x + NODE_WIDTH ||
            b.x >= a.x + NODE_WIDTH ||
            a.y >= b.y + NODE_HEIGHT ||
            b.y >= a.y + NODE_HEIGHT,
        ).toBe(true);
      }
    }
  });
});
