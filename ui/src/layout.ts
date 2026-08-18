import type { Edge, GraphNode } from "./domain";

export interface Position {
  x: number;
  y: number;
  depth: number;
}
export interface Layout {
  positions: Map<string, Position>;
  /** Routed paths are in the same padded coordinate space as the SVG. */
  edgePaths: Map<string, string>;
  /** Left edge of each rendered rank, also in the unpadded layout space. */
  stageXs: number[];
  width: number;
  height: number;
  stageCount: number;
}
export const NODE_WIDTH = 224,
  NODE_HEIGHT = 64,
  NODE_VERTICAL_GAP = 14,
  STAGE_HORIZONTAL_GAP = 54,
  GRAPH_PADDING = 48;

function containmentEdges(nodes: GraphNode[], edges: Edge[]): Edge[] {
  const ids = new Set(nodes.map((node) => node.id));
  return edges.filter(
    (edge) =>
      edge.type === "CONTAINS" &&
      ids.has(edge.src) &&
      ids.has(edge.dst),
  );
}

function directChildren(nodes: GraphNode[], edges: Edge[]): Map<string, string[]> {
  const parents = parentMap(nodes, edges);
  const children = new Map<string, string[]>();
  for (const node of nodes) {
    const parent = parents.get(node.id);
    if (parent) children.set(parent, [...(children.get(parent) ?? []), node.id]);
  }
  return children;
}

function localSequenceSuccessors(
  nodes: GraphNode[],
  edges: Edge[],
): Map<string, Set<string>> {
  const ids = new Set(nodes.map((node) => node.id));
  const parents = parentMap(nodes, edges);
  const outgoing = new Map<string, Set<string>>();
  for (const edge of edges) {
    if (
      edge.type === "FOLLOWS" &&
      ids.has(edge.src) &&
      ids.has(edge.dst) &&
      parents.get(edge.src) === parents.get(edge.dst)
    ) {
      outgoing.set(edge.src, new Set([...(outgoing.get(edge.src) ?? []), edge.dst]));
    }
  }
  return outgoing;
}

/**
 * Return the local workflow leaves for each composition boundary.
 *
 * This mirrors the server's submission guard. Keeping the projection here
 * means the layout engine reasons about the same terminal stages that the
 * planner contract accepts, while the server remains authoritative.
 */
export function workflowLeafIds(
  nodes: GraphNode[],
  edges: Edge[],
): Map<string | null, string[]> {
  const parents = parentMap(nodes, edges);
  const outgoing = localSequenceSuccessors(nodes, edges);
  const leaves = new Map<string | null, string[]>();
  for (const node of nodes) {
    if (outgoing.get(node.id)?.size) continue;
    const boundary = parents.get(node.id) ?? null;
    leaves.set(boundary, [...(leaves.get(boundary) ?? []), node.id]);
  }
  return leaves;
}

function sequenceProjection(nodes: GraphNode[], edges: Edge[]): Edge[] {
  const ids = new Set(nodes.map((node) => node.id));
  const children = directChildren(nodes, edges);
  const leavesByBoundary = workflowLeafIds(nodes, edges);
  // A composition anchor owns every stage in its boundary, so its direct
  // children are not all outputs. Only the stages with no later sibling in
  // that boundary are outputs. Recurse through a nested anchor so an outer
  // handoff lands on the nested workflow's actual final stage(s), rather than
  // drawing a separate long edge from every intermediate task.
  const boundaryExits = (id: string, seen = new Set<string>()): string[] => {
    if (seen.has(id)) return [id];
    const nextSeen = new Set(seen).add(id);
    const descendants = [...new Set(children.get(id) ?? [])];
    if (!descendants.length) return [id];
    const exits = leavesByBoundary.get(id) ?? descendants;
    return exits.flatMap((child) =>
      children.has(child) ? boundaryExits(child, nextSeen) : [child],
    );
  };
  const sequences = edges.filter(
    (edge) =>
      edge.type === "FOLLOWS" && ids.has(edge.src) && ids.has(edge.dst),
  );
  const projected = sequences.flatMap((edge) =>
    boundaryExits(edge.src).map((source) =>
      source === edge.src
        ? edge
        : {
            ...edge,
            id: `${edge.id}:terminal:${source}`,
            src: source,
          },
    ),
  );
  return [
    ...new Map(projected.map((edge) => [`${edge.src}:${edge.dst}`, edge])).values(),
  ];
}

/**
 * Return the workflow edges shown in the dendrogram.
 *
 * A composition anchor connects to the entry points of its child workflow.
 * Once a child has an incoming handoff, that handoff is the branch connection
 * and the ownership edge is not rendered as a competing shortcut. The result
 * is one visual language for sequence, fan-out, and fan-in: a child workflow
 * can split again, and its outputs can merge into the next product.
 */
export function displayEdges(nodes: GraphNode[], edges: Edge[]): Edge[] {
  const parents = parentMap(nodes, edges);
  const children = directChildren(nodes, edges);
  const incoming = new Map<string, Set<string>>();
  for (const edge of edges) {
    if (
      edge.type === "FOLLOWS" &&
      parents.get(edge.src) === parents.get(edge.dst) &&
      parents.has(edge.dst)
    ) {
      incoming.set(edge.dst, new Set([...(incoming.get(edge.dst) ?? []), edge.src]));
    }
  }
  const sequence = sequenceProjection(nodes, edges);
  const workflowEntries = containmentEdges(nodes, edges).filter(
    (edge) =>
      children.has(edge.src) &&
      !(incoming.get(edge.dst)?.size ?? 0),
  );
  return [
    ...new Map(
      [...workflowEntries, ...sequence].map((edge) => [
        `${edge.src}:${edge.dst}`,
        edge,
      ]),
    ).values(),
  ];
}

function parentMap(nodes: GraphNode[], edges: Edge[]): Map<string, string> {
  const parents = new Map<string, string>();
  for (const node of nodes) {
    if (node.parent_id) parents.set(node.id, node.parent_id);
  }
  for (const edge of edges) {
    if (edge.type === "CONTAINS" && !parents.has(edge.dst)) {
      parents.set(edge.dst, edge.src);
    }
  }
  return parents;
}

function stableTopologicalOrder(
  nodes: GraphNode[],
  edges: Edge[],
): { order: string[]; outgoing: Map<string, string[]> } {
  const ids = new Set(nodes.map((node) => node.id));
  const orderIndex = new Map(nodes.map((node, index) => [node.id, index]));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const pairs = new Set<string>();
  for (const edge of edges) {
    if (!ids.has(edge.src) || !ids.has(edge.dst)) continue;
    const pair = `${edge.src}:${edge.dst}`;
    if (pairs.has(pair)) continue;
    pairs.add(pair);
    outgoing.set(edge.src, [...(outgoing.get(edge.src) ?? []), edge.dst]);
    indegree.set(edge.dst, (indegree.get(edge.dst) ?? 0) + 1);
  }
  const queue = nodes
    .filter((node) => indegree.get(node.id) === 0)
    .map((node) => node.id);
  const order: string[] = [];
  while (queue.length) {
    queue.sort((a, b) => (orderIndex.get(a) ?? 0) - (orderIndex.get(b) ?? 0));
    const current = queue.shift()!;
    order.push(current);
    for (const next of outgoing.get(current) ?? []) {
      const nextDegree = (indegree.get(next) ?? 0) - 1;
      indegree.set(next, nextDegree);
      if (nextDegree === 0) queue.push(next);
    }
  }
  // The server validates the graph as acyclic. Keep the UI deterministic if
  // it receives a stale event during a replacement rather than allowing a
  // partial layout to scramble the stage order.
  if (order.length !== nodes.length) {
    for (const node of nodes) {
      if (!order.includes(node.id)) order.push(node.id);
    }
  }
  return { order, outgoing };
}

function workflowBranch(
  id: string,
  parents: Map<string, string>,
): string {
  let current = id;
  const seen = new Set<string>();
  while (parents.has(current) && !seen.has(current)) {
    seen.add(current);
    const parent = parents.get(current);
    if (!parent || !parents.has(parent)) return current;
    current = parent;
  }
  return current;
}

function routeWorkflowEdge(
  source: Position,
  target: Position,
): string {
  const x1 = source.x + GRAPH_PADDING + NODE_WIDTH;
  const y1 = source.y + GRAPH_PADDING + NODE_HEIGHT / 2;
  const x2 = target.x + GRAPH_PADDING;
  const y2 = target.y + GRAPH_PADDING + NODE_HEIGHT / 2;
  if (y1 === y2) return `M${x1} ${y1}H${x2}`;
  // Every target is placed at or below its predecessors. The vertical leg is
  // therefore a deliberate downward branch/merge rail, never a line that
  // shoots back up into an earlier lane. Keeping it in the first inter-stage
  // gap also prevents long handoffs from crossing cards in later stages.
  const channel = Math.min(x2 - 12, x1 + Math.max(18, (x2 - x1) / 2));
  return `M${x1} ${y1}H${channel}V${y2}H${x2}`;
}

/**
 * Lay the graph out as a deterministic left-to-right workflow dendrogram.
 *
 * Ranks come from the validated DAG. Sibling order follows composition
 * branches, and targets never move to an earlier rank or lane than their
 * predecessors. Every card has exactly two ports: WEST for incoming work and
 * EAST for outgoing work. That makes the visual contract explicit: arrows
 * leave and enter cards through their horizontal ends, while vertical rails
 * only connect those ports between cards.
 */
export async function layoutDendrogram(
  nodes: GraphNode[],
  edges: Edge[] = [],
): Promise<Layout> {
  const visibleEdges = displayEdges(nodes, edges);
  if (!nodes.length) {
    return {
      positions: new Map(),
      edgePaths: new Map(),
      stageXs: [0],
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      stageCount: 1,
    };
  }

  const sequenceTargets = new Set(sequenceProjection(nodes, edges).map((edge) => edge.dst));
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const layoutEdges = [...visibleEdges];
  const layoutPairs = new Set(layoutEdges.map((edge) => `${edge.src}:${edge.dst}`));
  // Parent metadata may arrive one event before its explicit CONTAINS edge.
  // Use it for rank constraints, but only render the canonical visible edge.
  for (const node of nodes) {
    if (!node.parent_id || !byId.has(node.parent_id) || sequenceTargets.has(node.id)) continue;
    const pair = `${node.parent_id}:${node.id}`;
    if (layoutPairs.has(pair)) continue;
    layoutPairs.add(pair);
    layoutEdges.push({
      id: `parent:${node.parent_id}:${node.id}`,
      src: node.parent_id,
      dst: node.id,
      type: "CONTAINS",
      created_at: "",
    });
  }

  const { order, outgoing } = stableTopologicalOrder(nodes, layoutEdges);
  const rank = new Map(nodes.map((node) => [node.id, 0]));
  for (const id of order) {
    const currentRank = rank.get(id) ?? 0;
    for (const next of outgoing.get(id) ?? []) {
      rank.set(next, Math.max(rank.get(next) ?? 0, currentRank + 1));
    }
  }
  const predecessors = new Map<string, string[]>();
  for (const edge of layoutEdges) {
    predecessors.set(edge.dst, [...(predecessors.get(edge.dst) ?? []), edge.src]);
  }

  const parents = parentMap(nodes, edges);
  const nodeOrder = new Map(nodes.map((node, index) => [node.id, index]));
  const branchIds = [...new Set(nodes.map((node) => workflowBranch(node.id, parents)))];
  const branchOrder = new Map(branchIds.map((id, index) => [id, index]));
  const rankNodes = new Map<number, string[]>();
  for (const node of nodes) {
    const depth = rank.get(node.id) ?? 0;
    rankNodes.set(depth, [...(rankNodes.get(depth) ?? []), node.id]);
  }
  for (const ids of rankNodes.values()) {
    ids.sort((a, b) => {
      const branchDelta =
        (branchOrder.get(workflowBranch(a, parents)) ?? 0) -
        (branchOrder.get(workflowBranch(b, parents)) ?? 0);
      return branchDelta || (nodeOrder.get(a) ?? 0) - (nodeOrder.get(b) ?? 0);
    });
  }

  const positions = new Map<string, Position>();
  const maxBottomByRank = new Map<number, number>();
  const maxRank = Math.max(...rank.values(), 0);
  for (let depth = 0; depth <= maxRank; depth += 1) {
    let cursor = 0;
    for (const id of rankNodes.get(depth) ?? []) {
      let y = Math.max(
        ...(predecessors.get(id) ?? []).map((predecessor) => positions.get(predecessor)?.y ?? 0),
        0,
      );
      // A projected handoff can cross expanded child stages. Put its target
      // below every intermediate lane so the long route has a clear channel.
      for (const predecessor of predecessors.get(id) ?? []) {
        const sourceDepth = rank.get(predecessor) ?? 0;
        if (depth - sourceDepth <= 1) continue;
        for (let intermediate = sourceDepth + 1; intermediate < depth; intermediate += 1) {
          y = Math.max(y, maxBottomByRank.get(intermediate) ?? 0);
        }
      }
      y = Math.max(y, cursor);
      positions.set(id, { x: depth * (NODE_WIDTH + STAGE_HORIZONTAL_GAP), y, depth });
      cursor = y + NODE_HEIGHT + NODE_VERTICAL_GAP;
    }
    const bottom = Math.max(
      ...(rankNodes.get(depth) ?? []).map((id) => (positions.get(id)?.y ?? 0) + NODE_HEIGHT),
      NODE_HEIGHT,
    );
    maxBottomByRank.set(depth, bottom);
  }

  const edgePaths = new Map<string, string>();
  for (const edge of visibleEdges) {
    const source = positions.get(edge.src);
    const target = positions.get(edge.dst);
    if (source && target) edgePaths.set(edge.id, routeWorkflowEdge(source, target));
  }

  const width = Math.max(NODE_WIDTH, maxRank * (NODE_WIDTH + STAGE_HORIZONTAL_GAP) + NODE_WIDTH);
  const height = Math.max(
    NODE_HEIGHT,
    ...[...positions.values()].map((position) => position.y + NODE_HEIGHT),
  );
  return {
    positions,
    edgePaths,
    stageXs: Array.from({ length: maxRank + 1 }, (_, depth) =>
      depth * (NODE_WIDTH + STAGE_HORIZONTAL_GAP),
    ),
    width,
    height,
    stageCount: maxRank + 1,
  };
}

/**
 * Draw a transient handoff without making it part of DAG layout. The endpoints
 * adapt to the relative horizontal positions, so a return to an earlier node
 * exits left/enters right while a return to a node on the right exits
 * right/enters left. A shallow upper bend keeps the leg legible without
 * competing with ordinary workflow edges when cards are close.
 */
export function returnPathBetween(a: Position, b: Position): string {
  const ax = a.x + GRAPH_PADDING,
    ay = a.y + GRAPH_PADDING,
    bx = b.x + GRAPH_PADDING,
    by = b.y + GRAPH_PADDING;
  const sourceIsLeftOfTarget = ax <= bx;
  const x1 = sourceIsLeftOfTarget ? ax + NODE_WIDTH : ax,
    y1 = ay,
    x2 = sourceIsLeftOfTarget ? bx : bx + NODE_WIDTH,
    y2 = by;
  const distance = Math.abs(x1 - x2);
  const bend = Math.max(24, Math.min(42, distance * 0.42));
  const midpoint = (x1 + x2) / 2;
  return `M${x1} ${y1}Q${midpoint} ${Math.min(y1, y2) - bend} ${x2} ${y2}`;
}
