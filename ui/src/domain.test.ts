import { describe, expect, it } from "vitest";
import {
  displayPath,
  displayNodeTitle,
  displayProjectTitle,
  documentReferenceHref,
  documentReferenceContentHref,
  documentReferenceLabel,
  primaryNodeActionIcon,
  primaryNodeActionLabel,
  skillReferenceLabel,
  skillSourceHref,
  skillTooltip,
  stripMarkdown,
} from "./domain";
import type { Project } from "./domain";

const project = (overrides: Partial<Project> = {}): Project => {
  const { document_refs, ...rest } = overrides;
  return {
  id: "project",
  project_id: "project",
  parent_id: null,
  objective: "Build a **playable** game",
  project_name: null,
  generated_prompt: "Build a **playable** game",
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
    document_refs: document_refs ?? [],
    ...rest,
  };
};

describe("display labels", () => {
  it("labels cancelled runs as a fresh run on every action surface", () => {
    expect(primaryNodeActionLabel("run", true)).toBe("Run again");
    expect(primaryNodeActionIcon("run", true)).toBe("rotate-cw");
    expect(primaryNodeActionLabel("run")).toBe("Run");
    expect(primaryNodeActionIcon("run")).toBe("play");
  });

  it("strips Markdown without changing the authored prompt", () => {
    expect(stripMarkdown("Build a **playable** [game](https://example.test)"))
      .toBe("Build a playable game");
  });

  it("prefers an explicit project name over the objective", () => {
    expect(displayProjectTitle(project({ project_name: "My **Game**" }))).toBe("My Game");
    expect(displayProjectTitle(project({ project_name: null, objective: "Scoped **title**" }))).toBe("Scoped title");
  });

  it("uses the node objective for the root setup node", () => {
    expect(displayNodeTitle(project({ project_name: "Navigation only" }))).toBe(
      "Build a playable game",
    );
  });

  it("renders home paths with a portable prefix", () => {
    expect(displayPath("/Users/alain/Developer/project")).toBe("~/Developer/project");
    expect(displayPath("/workspace/project")).toBe("/workspace/project");
  });

  it("keeps local skill ids and makes URL skill references readable", () => {
    expect(skillReferenceLabel("turn-executing")).toBe("turn-executing");
    expect(skillReferenceLabel("https://example.test/visual/SKILL.md?rev=1")).toBe("visual");
    expect(skillReferenceLabel("https://raw.example.test/game/vanilla-js-game-dev/SKILL.md"))
      .toBe("vanilla-js-game-dev");
    expect(skillTooltip(["turn-executing", "https://example.test/visual/SKILL.md"]))
      .toContain("Skills (2)");
    expect(skillSourceHref("find-mcps")).toBe("/api/skills/find-mcps");
    expect(skillSourceHref("imagegen")).toBe("/api/skills/imagegen");
    expect(skillSourceHref("turn-executing")).toBe("/api/skills/turn-executing");
    expect(skillSourceHref("https://example.test/skill/SKILL.md")).toBe(
      "https://example.test/skill/SKILL.md",
    );
  });

  it("links local documents through the project endpoint and preserves external URLs", () => {
    const reference = {
      ref: "docs/architecture.md#runtime",
      title: "Runtime architecture",
      media_type: null,
      imports: [],
    };
    expect(documentReferenceHref(reference, "project/1")).toBe(
      "/api/projects/project%2F1/documents/docs/architecture.md#runtime",
    );
    expect(documentReferenceLabel(reference)).toBe("Runtime architecture");
    expect(documentReferenceHref({ ...reference, ref: "https://example.test/spec.md" }, "project"))
      .toBe("https://example.test/spec.md");
    expect(documentReferenceContentHref(reference, "project/1")).toBe(
      "/api/projects/project%2F1/documents/docs/architecture.md",
    );
  });
});
