import type { GraphNode } from "./domain";
export interface Position {
  x: number;
  y: number;
  depth: number;
}
export interface Layout {
  positions: Map<string, Position>;
  width: number;
  height: number;
}
export const NODE_WIDTH = 224,
  NODE_HEIGHT = 64,
  GRAPH_PADDING = 48;

export function layoutDendrogram(nodes: GraphNode[]): Layout {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map<string, string[]>();
  for (const node of nodes)
    if (node.parent_id && byId.has(node.parent_id))
      children.set(node.parent_id, [
        ...(children.get(node.parent_id) ?? []),
        node.id,
      ]);
  const sort = (ids: string[]) =>
    ids.sort((a, b) =>
      (byId.get(a)?.objective ?? "").localeCompare(
        byId.get(b)?.objective ?? "",
      ),
    );
  children.forEach(sort);
  const roots = sort(
    nodes
      .filter((node) => !node.parent_id || !byId.has(node.parent_id))
      .map((node) => node.id),
  );
  const positions = new Map<string, Position>();
  let leaf = 0;
  const place = (id: string, depth: number): number => {
    const ys = (children.get(id) ?? []).map((child) => place(child, depth + 1));
    const y = ys.length
      ? (ys[0] + ys[ys.length - 1]) / 2
      : leaf++ * (NODE_HEIGHT + 18);
    positions.set(id, { x: depth * (NODE_WIDTH + 54), y, depth });
    return y;
  };
  roots.forEach((id, index) => {
    if (index) leaf += 1;
    place(id, 0);
  });
  let width = NODE_WIDTH,
    height = NODE_HEIGHT;
  positions.forEach((p) => {
    width = Math.max(width, p.x + NODE_WIDTH);
    height = Math.max(height, p.y + NODE_HEIGHT);
  });
  return { positions, width, height };
}

export function pathBetween(
  a: Position,
  b: Position,
  type: "CONTAINS" | "DEPENDS_ON" = "CONTAINS",
): string {
  const ax = a.x + GRAPH_PADDING,
    ay = a.y + GRAPH_PADDING,
    bx = b.x + GRAPH_PADDING,
    by = b.y + GRAPH_PADDING;
  if (type === "DEPENDS_ON" && a.depth === b.depth) {
    const center = ax + NODE_WIDTH / 2;
    const gap = by - (ay + NODE_HEIGHT);
    if (gap >= 0 && gap <= 24) return `M${center} ${ay + NODE_HEIGHT}V${by}`;
    const lane = ax + NODE_WIDTH + 18;
    if (gap > 24)
      return `M${center} ${ay + NODE_HEIGHT}V${ay + NODE_HEIGHT + 9}H${lane}V${by - 9}H${center}V${by}`;
    return `M${center} ${ay}V${ay - 9}H${lane}V${by + NODE_HEIGHT + 9}H${center}V${by + NODE_HEIGHT}`;
  }
  const x1 = ax + NODE_WIDTH,
    y1 = ay + NODE_HEIGHT / 2,
    x2 = bx,
    y2 = by + NODE_HEIGHT / 2,
    elbow = x1 + (x2 - x1) / 2;
  return `M${x1} ${y1}H${elbow}V${y2}H${x2}`;
}
