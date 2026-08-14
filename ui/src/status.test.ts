import { describe, expect, it } from "vitest";
import { deriveStatus } from "./state";
import type { GraphNode } from "./domain";
const base: GraphNode = {
  id: "n",
  project_id: "n",
  parent_id: null,
  objective: "n",
  status: "RUNNING",
  ui_state: "running",
  allowed_actions: [],
  generation_active: false,
  paused: false,
  auto_run: false,
  required_inputs: [],
  revision: 1,
  needs_review: false,
  merge_accepted: false,
  verification_round: 0,
};
describe("truthful status", () => {
  it("only calls a live provider generating", () => {
    expect(deriveStatus([base])).not.toContain("generating");
    expect(deriveStatus([{ ...base, generation_active: true }])).toBe(
      "1 model generating",
    );
  });
  it("separates parent verification from human review", () => {
    expect(
      deriveStatus([
        {
          ...base,
          status: "COMPLETE",
          ui_state: "review",
          needs_review: true,
          review_owner: "parent",
          verification_status: "pending",
        },
      ]),
    ).toContain("parent verification");
    expect(
      deriveStatus([
        {
          ...base,
          status: "COMPLETE",
          ui_state: "review",
          needs_review: true,
          review_owner: "manual",
        },
      ]),
    ).toContain("need review");
  });
});
