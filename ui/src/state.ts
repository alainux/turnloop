import type { GraphNode } from "./domain";
export function deriveStatus(nodes: GraphNode[]): string {
  const generating = nodes.filter((node) => node.generation_active).length;
  if (generating)
    return `${generating} model${generating === 1 ? "" : "s"} generating`;
  if (nodes.some((node) => node.status === "RUNNABLE"))
    return "Ready nodes available";
  if (
    nodes.length &&
    nodes.every((node) => ["COMPLETE", "EXPANDED"].includes(node.status))
  )
    return "Workgraph complete";
  return "Workgraph ready";
}
