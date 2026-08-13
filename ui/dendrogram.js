/** Pure, deterministic horizontal dendrogram layout for Turn's containment tree. */
export function layoutDendrogram(nodes, options = {}) {
  const nodeWidth = options.nodeWidth ?? 224;
  const nodeHeight = options.nodeHeight ?? 76;
  const columnGap = options.columnGap ?? 54;
  const rowGap = options.rowGap ?? 18;
  const forestGap = options.forestGap ?? 38;
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const children = new Map();
  const sortKey = (node) => `${node.created_at || ""}\u0000${node.objective || ""}\u0000${node.id}`;
  const ordered = (ids) => [...ids].sort((a, b) => sortKey(byId.get(a)).localeCompare(sortKey(byId.get(b))));

  for (const node of nodes) {
    if (!node.parent_id || !byId.has(node.parent_id)) continue;
    if (!children.has(node.parent_id)) children.set(node.parent_id, []);
    children.get(node.parent_id).push(node.id);
  }
  for (const [id, ids] of children) children.set(id, ordered(ids));

  const roots = ordered(nodes.filter((node) => !node.parent_id || !byId.has(node.parent_id)).map((node) => node.id));
  const positions = new Map();
  const visited = new Set();
  const visiting = new Set();
  let nextLeafY = 0;

  const place = (id, depth) => {
    if (visited.has(id)) return positions.get(id).y;
    if (visiting.has(id)) { // Defensive only; persisted plans are cycle-checked.
      const y = nextLeafY;
      nextLeafY += nodeHeight + rowGap;
      positions.set(id, { x: depth * (nodeWidth + columnGap), y, depth });
      visited.add(id);
      return y;
    }
    visiting.add(id);
    const childYs = (children.get(id) || []).filter((child) => !visiting.has(child)).map((child) => place(child, depth + 1));
    const y = childYs.length ? (childYs[0] + childYs.at(-1)) / 2 : nextLeafY;
    if (!childYs.length) nextLeafY += nodeHeight + rowGap;
    positions.set(id, { x: depth * (nodeWidth + columnGap), y, depth });
    visiting.delete(id);
    visited.add(id);
    return y;
  };

  roots.forEach((id, index) => {
    if (index) nextLeafY += forestGap;
    place(id, 0);
  });
  // Malformed imported data cannot make a node disappear from the canvas.
  for (const node of nodes) {
    if (!visited.has(node.id)) {
      if (positions.size) nextLeafY += forestGap;
      place(node.id, 0);
    }
  }

  let width = nodeWidth;
  let height = nodeHeight;
  for (const position of positions.values()) {
    width = Math.max(width, position.x + nodeWidth);
    height = Math.max(height, position.y + nodeHeight);
  }
  return { nodes, byId, children, roots, positions, width, height };
}

export function dendrogramPath(source, target, options = {}) {
  const nodeWidth = options.nodeWidth ?? 224;
  const nodeHeight = options.nodeHeight ?? 76;
  const x1 = source.x + nodeWidth;
  const y1 = source.y + nodeHeight / 2;
  const x2 = target.x;
  const y2 = target.y + nodeHeight / 2;
  const elbow = x1 + (x2 - x1) / 2;
  return `M${x1} ${y1}H${elbow}V${y2}H${x2}`;
}
