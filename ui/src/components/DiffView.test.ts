import { describe, expect, it } from "vitest";
import { parsePatch } from "./DiffView";
describe("diff parser", () => {
  it("groups files and counts material lines", () => {
    const files = parsePatch(
      "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-old\n+new",
    );
    expect(files).toHaveLength(1);
    expect(files[0]).toMatchObject({
      name: "a.ts",
      additions: 1,
      deletions: 1,
    });
  });
});
