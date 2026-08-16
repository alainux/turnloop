import { describe, expect, it } from "vitest";
import {
  displayPath,
  displayProjectTitle,
  skillReferenceLabel,
  skillTooltip,
  stripMarkdown,
} from "./domain";
import type { Project } from "./domain";

const project = (overrides: Partial<Project> = {}): Project => ({
  id: "project",
  project_id: "project",
  parent_id: null,
  objective: "Build a **playable** game",
  project_name: null,
  generated_prompt: "Build a **playable** game",
  architecture_spec: null,
  repo_path: "/Users/alain/Developer/ialan/turnloop/projects/proj-test",
  executor: "planner",
  agent: null,
  verification: null,
  status: "PENDING",
  paused: false,
  auto_run: false,
  run_policy: null,
  required_inputs: [],
  resource_refs: [],
  artifact_refs: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  progress: null,
  agent_state: null,
  agent_message: null,
  ...overrides,
});

describe("display labels", () => {
  it("strips Markdown without changing the authored prompt", () => {
    expect(stripMarkdown("Build a **playable** [game](https://example.test)"))
      .toBe("Build a playable game");
  });

  it("prefers an explicit name over the scoped architecture title", () => {
    expect(displayProjectTitle(project({
      project_name: "My **Game**",
      architecture_spec: { title: "Scoped title" } as Project["architecture_spec"],
    }))).toBe("My Game");
    expect(displayProjectTitle(project({
      architecture_spec: { title: "Scoped **title**" } as Project["architecture_spec"],
    }))).toBe("Scoped title");
  });

  it("renders home paths with a portable prefix", () => {
    expect(displayPath("/Users/alain/Developer/project")).toBe("~/Developer/project");
    expect(displayPath("/workspace/project")).toBe("/workspace/project");
  });

  it("keeps local skill ids and makes URL skill references readable", () => {
    expect(skillReferenceLabel("turn-executing")).toBe("turn-executing");
    expect(skillReferenceLabel("https://example.test/visual/SKILL.md?rev=1")).toBe("SKILL.md");
    expect(skillTooltip(["turn-executing", "https://example.test/visual/SKILL.md"]))
      .toContain("Skills (2)");
  });
});
