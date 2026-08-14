import { useMemo } from "react";
import type { Artifact } from "../domain";
import { Icon } from "./Icon";

interface DiffFile {
  name: string;
  lines: string[];
  additions: number;
  deletions: number;
}
export function parsePatch(patch: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  for (const line of patch.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const match = line.match(/ b\/(.+)$/);
      current = {
        name: match?.[1] ?? "changed file",
        lines: [line],
        additions: 0,
        deletions: 0,
      };
      files.push(current);
    } else if (current) {
      current.lines.push(line);
      if (line.startsWith("+") && !line.startsWith("+++")) current.additions++;
      if (line.startsWith("-") && !line.startsWith("---")) current.deletions++;
    }
  }
  return files;
}
export function DiffView({ artifacts }: { artifacts: Artifact[] }) {
  const patch = artifacts.find(
    (item) => item.kind === "code_diff" && typeof item.content === "string",
  )?.content as string | undefined;
  const files = useMemo(() => (patch ? parsePatch(patch) : []), [patch]);
  if (!patch)
    return (
      <p className="empty-note">No code diff was produced by this node.</p>
    );
  return (
    <div className="diff-view">
      {files.map((file, index) => (
        <details
          className="diff-file"
          key={`${file.name}-${index}`}
          open={index === 0}
        >
          <summary>
            <Icon name="file" />
            <span>{file.name}</span>
            <small>
              <b>+{file.additions}</b> <i>−{file.deletions}</i>
            </small>
          </summary>
          <pre>
            {file.lines.map((line, lineIndex) => (
              <span
                key={lineIndex}
                className={
                  line.startsWith("+") && !line.startsWith("+++")
                    ? "diff-add"
                    : line.startsWith("-") && !line.startsWith("---")
                      ? "diff-del"
                      : line.startsWith("@@")
                        ? "diff-hunk"
                        : ""
                }
              >
                {line || " "}
                {"\n"}
              </span>
            ))}
          </pre>
        </details>
      ))}
    </div>
  );
}
