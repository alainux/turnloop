import { describe, expect, it } from "vitest";
import { orderDocumentNodes } from "./DocumentView";
import type { Edge, GraphNode } from "../domain";

const node = (id: string, parent_id: string | null): GraphNode =>
  ({
    id,
    project_id: "root",
    parent_id,
    objective: id,
    project_name: null,
    generated_prompt: null,
    architecture_spec: null,
    repo_path: null,
    executor: "codex",
    agent: null,
    status: "COMPLETE",
    paused: false,
    auto_run: false,
    run_policy: null,
    required_inputs: [],
    resource_refs: [],
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
});
