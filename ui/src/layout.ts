import type { Edge, GraphNode } from "./domain";

export interface Position {
  x: number;
  y: number;
  depth: number;
}
export interface Layout {
  positions: Map<string, Position>;
  width: number;
  height: number;
  stageCount: number;
}
export const NODE_WIDTH = 224,
  NODE_HEIGHT = 64,
  GRAPH_PADDING = 48;

function hasAlternativePath(
  source: string,
  target: string,
  edges: Edge[],
  excludedId: string,
): boolean {
  const outgoing = new Map<string, string[]>();
  for (const edge of edges) {
    if (edge.id === excludedId) continue;
    outgoing.set(edge.src, [...(outgoing.get(edge.src) ?? []), edge.dst]);
  }
  const visited = new Set<string>();
  const pending = [...(outgoing.get(source) ?? [])];
  while (pending.length) {
    const id = pending.shift()!;
    if (id === target) return true;
    if (visited.has(id)) continue;
    visited.add(id);
    pending.push(...(outgoing.get(id) ?? []));
  }
  return false;
}

/**
 * Return the workflow edges that belong in the visual dendrogram.
 *
 * A planner may persist a useful direct dependency for scheduling even when
 * another dependency path already implies it. Rendering that shortcut makes
 * a start node appear to connect directly to an end node. The UI shows the
 * transitive reduction of explicit dependencies and retains containment only
 * for nodes that do not already enter through a workflow dependency.
 */
export function displayEdges(nodes: GraphNode[], edges: Edge[]): Edge[] {
  const ids = new Set(nodes.map((node) => node.id));
  const children = new Map<string, string[]>();
  for (const node of nodes) {
    if (node.parent_id && ids.has(node.parent_id)) {
      children.set(node.parent_id, [
        ...(children.get(node.parent_id) ?? []),
        node.id,
      ]);
    }
  }
  for (const edge of edges) {
    if (edge.type !== "CONTAINS" || !ids.has(edge.src) || !ids.has(edge.dst)) {
      continue;
    }
    children.set(edge.src, [...(children.get(edge.src) ?? []), edge.dst]);
  }
  const terminalDescendants = (id: string): string[] => {
    const descendants = [...new Set(children.get(id) ?? [])];
    if (!descendants.length) return [id];
    return descendants.flatMap(terminalDescendants);
  };
  const dependencies = edges.filter(
    (edge) =>
      edge.type === "DEPENDS_ON" && ids.has(edge.src) && ids.has(edge.dst),
  );
  // A dependency on a planner/container means that the dependent consumes
  // the completed branch, not the container card itself. Project that handoff
  // onto the branch's terminal outputs so nested decompositions remain visibly
  // connected to their downstream integrator.
  const expandedDependencies = dependencies.flatMap((edge) =>
    terminalDescendants(edge.src).map((source) =>
      source === edge.src
        ? edge
        : {
            ...edge,
            id: `${edge.id}:terminal:${source}`,
            src: source,
        },
    ),
  );
  // Older state files may contain the same dependency twice. Collapse
  // duplicate workflow edges before transitive reduction; otherwise each
  // duplicate looks like an alternative path and both get removed.
  const uniqueDependencies = [
    ...new Map(
      expandedDependencies.map((edge) => [`${edge.src}:${edge.dst}`, edge]),
    ).values(),
  ];
  const reducedDependencies = uniqueDependencies.filter(
    (edge) =>
      !hasAlternativePath(edge.src, edge.dst, uniqueDependencies, edge.id),
  );
  const dependencyTargets = new Set(
    reducedDependencies.map((edge) => edge.dst),
  );
  const containment = edges.filter(
    (edge) =>
      edge.type === "CONTAINS" &&
      ids.has(edge.src) &&
      ids.has(edge.dst) &&
      !dependencyTargets.has(edge.dst),
  );
  return [...containment, ...reducedDependencies];
}

/**
 * Lay the graph out as a left-to-right work pipeline.
 *
 * Containment still controls the vertical dendrogram, while both containment
 * and explicit workflow edges advance a node to a later stage. This means a
 * dependency is never rendered as a same-column vertical relationship: it is
 * a normal grey edge from one workflow stage to the next.
 */
export function layoutDendrogram(nodes: GraphNode[], edges: Edge[] = []): Layout {
  const visibleEdges = displayEdges(nodes, edges);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map<string, string[]>();
  for (const node of nodes) {
    if (node.parent_id && byId.has(node.parent_id)) {
      children.set(node.parent_id, [
        ...(children.get(node.parent_id) ?? []),
        node.id,
      ]);
    }
  }
  const alphabetical = (a: string, b: string) =>
    (byId.get(a)?.objective ?? "").localeCompare(byId.get(b)?.objective ?? "");
  const orderSiblings = (ids: string[]) => {
    const members = new Set(ids);
    const outgoing = new Map<string, string[]>();
    const indegree = new Map(ids.map((id) => [id, 0]));
    for (const edge of visibleEdges) {
      if (
        edge.type !== "DEPENDS_ON" ||
        !members.has(edge.src) ||
        !members.has(edge.dst)
      ) {
        continue;
      }
      outgoing.set(edge.src, [...(outgoing.get(edge.src) ?? []), edge.dst]);
      indegree.set(edge.dst, (indegree.get(edge.dst) ?? 0) + 1);
    }
    const ready = ids.filter((id) => indegree.get(id) === 0).sort(alphabetical);
    const ordered: string[] = [];
    while (ready.length) {
      const id = ready.shift()!;
      ordered.push(id);
      for (const dependent of outgoing.get(id) ?? []) {
        const next = (indegree.get(dependent) ?? 0) - 1;
        indegree.set(dependent, next);
        if (next === 0) ready.push(dependent);
      }
      ready.sort(alphabetical);
    }
    return ordered.length === ids.length
      ? ordered
      : (() => {
          throw new Error("graph contains a containment dependency cycle");
        })();
  };
  children.forEach((ids, parent) => children.set(parent, orderSiblings(ids)));
  const roots = orderSiblings(
    nodes
      .filter((node) => !node.parent_id || !byId.has(node.parent_id))
      .map((node) => node.id),
  );

  // Compute the longest left-to-right stage across the complete graph. The
  // server validates this DAG; malformed state is surfaced to the UI.
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const stageEdges = visibleEdges
    .filter((edge) => byId.has(edge.src) && byId.has(edge.dst))
    .map((edge) => ({ src: edge.src, dst: edge.dst }));
  const stagePairs = new Set(stageEdges.map((edge) => `${edge.src}:${edge.dst}`));
  // Parent metadata is also authoritative during partial loads where the
  // corresponding CONTAINS edge has not arrived yet.
  for (const node of nodes) {
    if (node.parent_id && byId.has(node.parent_id)) {
      const pair = `${node.parent_id}:${node.id}`;
      if (!stagePairs.has(pair)) {
        stagePairs.add(pair);
        stageEdges.push({ src: node.parent_id, dst: node.id });
      }
    }
  }

  // A dependency on a container means that the dependent consumes the
  // container's completed workgraph, not merely the container card. Expand
  // that dependency for stage calculation so a final integration node is
  // placed after the container's deepest work, while keeping the original
  // edge for rendering and graph semantics.
  const terminalDescendants = (id: string): string[] => {
    const descendants = children.get(id) ?? [];
    if (!descendants.length) return [id];
    return descendants.flatMap(terminalDescendants);
  };
  for (const edge of visibleEdges) {
    if (edge.type !== "DEPENDS_ON") continue;
    for (const source of terminalDescendants(edge.src)) {
      if (source === edge.dst) continue;
      const pair = `${source}:${edge.dst}`;
      if (stagePairs.has(pair)) continue;
      stagePairs.add(pair);
      stageEdges.push({ src: source, dst: edge.dst });
    }
  }
  for (const edge of stageEdges) {
    outgoing.set(edge.src, [...(outgoing.get(edge.src) ?? []), edge.dst]);
    indegree.set(edge.dst, (indegree.get(edge.dst) ?? 0) + 1);
  }
  const stage = new Map<string, number>(nodes.map((node) => [node.id, 0]));
  const ready = nodes
    .filter((node) => indegree.get(node.id) === 0)
    .map((node) => node.id)
    .sort(alphabetical);
  const visited = new Set<string>();
  while (ready.length) {
    const id = ready.shift()!;
    visited.add(id);
    for (const dst of outgoing.get(id) ?? []) {
      stage.set(dst, Math.max(stage.get(dst) ?? 0, (stage.get(id) ?? 0) + 1));
      const next = (indegree.get(dst) ?? 0) - 1;
      indegree.set(dst, next);
      if (next === 0) ready.push(dst);
    }
    ready.sort(alphabetical);
  }
  if (visited.size !== nodes.length) {
    throw new Error("graph contains a workflow dependency cycle");
  }

  const positions = new Map<string, Position>();
  let leaf = 0;
  const place = (id: string): number => {
    const ys = (children.get(id) ?? []).map((child) => place(child));
    const y = ys.length
      ? (ys[0] + ys[ys.length - 1]) / 2
      : leaf++ * (NODE_HEIGHT + 18);
    const depth = stage.get(id) ?? 0;
    positions.set(id, {
      x: depth * (NODE_WIDTH + 54),
      y,
      depth,
    });
    return y;
  };
  roots.forEach((id, index) => {
    if (index) leaf += 1;
    place(id);
  });
  // Include disconnected nodes that are not discoverable from a parent root.
  nodes.forEach((node) => {
    if (!positions.has(node.id)) place(node.id);
  });

  // Keep every stage on one visual center line. The recursive placement above
  // is still responsible for meaningful branch spacing, but independently
  // sized stages otherwise make a single final node drift into a zig-zag.
  // Aligning stage bounds preserves branch structure while making the
  // left-to-right sequence readable at a glance.
  if (positions.size > 0) {
    const stageBounds = new Map<number, { min: number; max: number }>();
    let graphMin = Number.POSITIVE_INFINITY;
    let graphMax = Number.NEGATIVE_INFINITY;
    positions.forEach((position) => {
      graphMin = Math.min(graphMin, position.y);
      graphMax = Math.max(graphMax, position.y);
      const bounds = stageBounds.get(position.depth);
      if (bounds) {
        bounds.min = Math.min(bounds.min, position.y);
        bounds.max = Math.max(bounds.max, position.y);
      } else {
        stageBounds.set(position.depth, { min: position.y, max: position.y });
      }
    });
    const graphCenter = (graphMin + graphMax + NODE_HEIGHT) / 2;
    positions.forEach((position) => {
      const bounds = stageBounds.get(position.depth)!;
      const stageCenter = (bounds.min + bounds.max + NODE_HEIGHT) / 2;
      position.y += graphCenter - stageCenter;
    });
  }

  // A one-to-one implementation → verification sequence is one visual row.
  // Verification remains a later stage and an ordinary dependency, but
  // aligning the pair prevents the graph from becoming a ladder when several
  // independent branches each have their own verifier.
  const incomingDependencies = new Map<string, string[]>();
  const outgoingDependencies = new Map<string, string[]>();
  visibleEdges.forEach((edge) => {
    if (edge.type !== "DEPENDS_ON") return;
    incomingDependencies.set(edge.dst, [
      ...(incomingDependencies.get(edge.dst) ?? []),
      edge.src,
    ]);
    outgoingDependencies.set(edge.src, [
      ...(outgoingDependencies.get(edge.src) ?? []),
      edge.dst,
    ]);
  });
  const occupied = new Map<number, number[]>();
  positions.forEach((position) => {
    occupied.set(position.depth, [
      ...(occupied.get(position.depth) ?? []),
      position.y,
    ]);
  });
  for (const node of nodes) {
    if (node.agent?.type_id !== "verifier") continue;
    const sources = incomingDependencies.get(node.id) ?? [];
    if (sources.length !== 1) continue;
    const source = byId.get(sources[0]);
    const sourceDependents = outgoingDependencies.get(sources[0]) ?? [];
    const sourcePosition = positions.get(sources[0]);
    const targetPosition = positions.get(node.id);
    if (!source || sourceDependents.length !== 1 || !sourcePosition || !targetPosition) continue;
    const peers = (occupied.get(targetPosition.depth) ?? []).filter(
      (y) => Math.abs(y - sourcePosition.y) > 1,
    );
    if (peers.some((y) => Math.abs(y - sourcePosition.y) < NODE_HEIGHT + 18)) continue;
    targetPosition.y = sourcePosition.y;
    occupied.set(targetPosition.depth, [...peers, targetPosition.y]);
  }

  let width = NODE_WIDTH,
    height = NODE_HEIGHT,
    maxDepth = 0;
  positions.forEach((p) => {
    width = Math.max(width, p.x + NODE_WIDTH);
    height = Math.max(height, p.y + NODE_HEIGHT);
    maxDepth = Math.max(maxDepth, p.depth);
  });
  return { positions, width, height, stageCount: maxDepth + 1 };
}

export function pathBetween(
  a: Position,
  b: Position,
  _type: Edge["type"] = "CONTAINS",
): string {
  const ax = a.x + GRAPH_PADDING,
    ay = a.y + GRAPH_PADDING,
    bx = b.x + GRAPH_PADDING,
    by = b.y + GRAPH_PADDING;
  const x1 = ax + NODE_WIDTH,
    y1 = ay + NODE_HEIGHT / 2,
    x2 = bx,
    y2 = by + NODE_HEIGHT / 2,
    elbow = x1 + (x2 - x1) / 2;
  return `M${x1} ${y1}H${elbow}V${y2}H${x2}`;
}

/**
 * Draw a transient backwards handoff without making it part of DAG layout.
 * The source exits through its left edge and the target is entered through its
 * right edge, so a verifier-to-executor return reads in the correct direction.
 * A shallow upper bend keeps the return leg legible without competing with the
 * ordinary dependency edge when cards are close.
 */
export function returnPathBetween(a: Position, b: Position): string {
  const ax = a.x + GRAPH_PADDING,
    ay = a.y + GRAPH_PADDING,
    bx = b.x + GRAPH_PADDING,
    by = b.y + GRAPH_PADDING;
  const x1 = ax,
    y1 = ay,
    x2 = bx + NODE_WIDTH,
    y2 = by;
  const distance = Math.abs(x1 - x2);
  const bend = Math.max(24, Math.min(42, distance * 0.42));
  const midpoint = (x1 + x2) / 2;
  return `M${x1} ${y1}Q${midpoint} ${Math.min(y1, y2) - bend} ${x2} ${y2}`;
}
