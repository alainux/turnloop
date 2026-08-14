import type { GraphNode } from "./domain";
export function deriveStatus(nodes: GraphNode[]): string {
  const generating = nodes.filter((node) => node.generation_active).length;
  const manual = nodes.filter(
    (node) => node.needs_review && node.review_owner === "manual",
  ).length;
  const parent = nodes.filter(
    (node) =>
      node.verification_status === "pending" && node.review_owner === "parent",
  ).length;
  if (generating)
    return `${generating} model${generating === 1 ? "" : "s"} generating`;
  if (manual) return `${manual} result${manual === 1 ? "" : "s"} need review`;
  if (parent) return `${parent} awaiting parent verification`;
  if (nodes.some((node) => node.status === "RUNNABLE"))
    return "Ready nodes available";
  if (
    nodes.length &&
    nodes.every((node) => ["COMPLETE", "EXPANDED"].includes(node.status))
  )
    return "Workgraph complete";
  return "Workgraph ready";
}
