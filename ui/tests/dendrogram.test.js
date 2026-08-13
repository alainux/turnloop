import test from "node:test";
import assert from "node:assert/strict";
import { dendrogramPath, layoutDendrogram } from "../dendrogram.js";

const nodes = [
  { id: "root", parent_id: null, objective: "Root", created_at: "1" },
  { id: "alpha", parent_id: "root", objective: "Alpha", created_at: "2" },
  { id: "beta", parent_id: "root", objective: "Beta", created_at: "3" },
  { id: "a1", parent_id: "alpha", objective: "A1", created_at: "4" },
  { id: "a2", parent_id: "alpha", objective: "A2", created_at: "5" },
];

test("dendrogram assigns depth to columns and evenly spaced leaves to rows", () => {
  const graph = layoutDendrogram(nodes);
  assert.equal(graph.positions.get("root").depth, 0);
  assert.equal(graph.positions.get("alpha").depth, 1);
  assert.equal(graph.positions.get("a1").depth, 2);
  assert.equal(graph.positions.get("a2").y - graph.positions.get("a1").y, 94);
  assert.equal(graph.positions.get("beta").y - graph.positions.get("a2").y, 94);
});

test("each internal node is centered across its first and last descendant", () => {
  const graph = layoutDendrogram(nodes);
  const a1 = graph.positions.get("a1").y;
  const a2 = graph.positions.get("a2").y;
  const beta = graph.positions.get("beta").y;
  assert.equal(graph.positions.get("alpha").y, (a1 + a2) / 2);
  assert.equal(graph.positions.get("root").y, (graph.positions.get("alpha").y + beta) / 2);
});

test("layout is deterministic and dendrogram edges use orthogonal elbows", () => {
  const first = layoutDendrogram(nodes);
  const shuffled = layoutDendrogram([...nodes].reverse());
  assert.deepEqual([...first.positions], [...shuffled.positions]);
  assert.match(dendrogramPath(first.positions.get("root"), first.positions.get("alpha")), /^M\d+(?:\.\d+)? \d+(?:\.\d+)?H\d+(?:\.\d+)?V\d+(?:\.\d+)?H\d+(?:\.\d+)?$/);
});

test("orphaned nodes are retained as separate dendrogram roots", () => {
  const graph = layoutDendrogram([...nodes, { id: "orphan", parent_id: "missing", objective: "Orphan", created_at: "6" }]);
  assert.ok(graph.positions.has("orphan"));
  assert.deepEqual(graph.roots, ["root", "orphan"]);
  assert.ok(graph.positions.get("orphan").y > graph.positions.get("beta").y);
});
