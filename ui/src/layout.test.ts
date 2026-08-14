import { describe, expect, it } from "vitest";
import { layoutDendrogram, pathBetween } from "./layout";
import type { GraphNode } from "./domain";
const node = (id: string, parent_id: string | null): GraphNode => ({
  id,
  parent_id,
  project_id: "root",
  objective: id,
  status: "RUNNABLE",
  ui_state: "ready",
  allowed_actions: ["run"],
  generation_active: false,
  paused: false,
  auto_run: false,
  required_inputs: [],
  revision: 1,
  needs_review: false,
  merge_accepted: false,
  verification_round: 0,
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
    expect(pathBetween(root, a)).toMatch(/^M.*H.*V.*H/);
    expect(pathBetween(a, b, "DEPENDS_ON")).toMatch(/^M.*V/);
    expect(pathBetween(root, a)).toContain("272");
    expect(pathBetween({ ...a, y: 0 }, { ...b, y: 164 }, "DEPENDS_ON")).toMatch(
      /^M.*V.*H.*V.*H.*V/,
    );
  });
});
