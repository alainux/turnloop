import { afterEach, describe, expect, it, vi } from "vitest";
import {
  closeProjectTerminals,
  deleteProject,
  getProjectGraph,
  renameProject,
  setProjectMode,
  stepProject,
} from "./api/projects";
import { editNode, getNodeDetail, provideNodeInput, runNodeAction } from "./api/nodes";
import { chooseDirectory, getSettings, saveSettings } from "./api/workspace";

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

afterEach(() => vi.restoreAllMocks());

describe("typed API modules", () => {
  it("builds project requests with encoded identifiers and stable bodies", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() =>
        Promise.resolve(response({
          project_id: "project/one",
          nodes: [],
          edges: [],
        })),
      );

    await getProjectGraph("project/one");
    await renameProject("project/one", "Renamed");
    await setProjectMode("project/one", true);
    await stepProject("project/one");
    await closeProjectTerminals("project/one");
    await deleteProject("project/one", {
      delete_files: true,
      delete_conversations: false,
    });

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["/api/projects/project%2Fone/graph", undefined, undefined],
      ["/api/projects/project%2Fone", "PATCH", JSON.stringify({ name: "Renamed" })],
      ["/api/projects/project%2Fone/mode", "POST", JSON.stringify({ auto_run: true })],
      ["/api/projects/project%2Fone/step", "POST", undefined],
      ["/api/projects/project%2Fone/workspace/close", "POST", undefined],
      [
        "/api/projects/project%2Fone",
        "DELETE",
        JSON.stringify({ delete_files: true, delete_conversations: false }),
      ],
    ]);
  });

  it("keeps node mutations and workspace requests typed at the boundary", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() =>
        Promise.resolve(response({ node: {}, runs: [], artifacts: [] })),
      );

    await getNodeDetail("node/one");
    await runNodeAction("node/one", "retry");
    await editNode("node/one", { objective: "Updated" });
    await provideNodeInput("node/one", "input", "value");
    await getSettings();
    await saveSettings({ theme: "dark" });
    await chooseDirectory();

    expect(fetchMock.mock.calls.map(([url, init]) => [url, init?.method, init?.body])).toEqual([
      ["/api/nodes/node%2Fone", undefined, undefined],
      ["/api/nodes/node%2Fone/retry", "POST", undefined],
      ["/api/nodes/node%2Fone/edit", "POST", JSON.stringify({ objective: "Updated" })],
      [
        "/api/nodes/node%2Fone/provide-input",
        "POST",
        JSON.stringify({ input_id: "input", value: "value" }),
      ],
      ["/api/settings", undefined, undefined],
      ["/api/settings", "POST", JSON.stringify({ theme: "dark" })],
      ["/api/system/pick-directory", "POST", undefined],
    ]);
  });
});
