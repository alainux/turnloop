import { useMemo } from "react";
import type { Edge, GraphNode, Usage } from "../domain";
import { tokens } from "../domain";
import {
  layoutDendrogram,
  NODE_HEIGHT,
  NODE_WIDTH,
  pathBetween,
} from "../layout";
import { Icon } from "./Icon";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  usage: Record<string, Usage>;
  selected: string | null;
  onSelect: (id: string) => void;
  onRun: (node: GraphNode) => void;
  onContextMenu: (node: GraphNode, x: number, y: number) => void;
}
const stateLabel = (node: GraphNode) =>
  node.status === "RUNNING"
    ? node.generation_active
      ? "generating"
      : "starting"
    : node.ui_state.replaceAll("_", " ");
export function Graph({
  nodes,
  edges,
  usage,
  selected,
  onSelect,
  onRun,
  onContextMenu,
}: Props) {
  const layout = useMemo(() => layoutDendrogram(nodes), [nodes]);
  return (
    <div
      className="graph-canvas"
      style={{ width: layout.width + 96, height: layout.height + 96 }}
    >
      <svg
        className="graph-edges"
        width={layout.width + 96}
        height={layout.height + 96}
        aria-hidden="true"
      >
        {edges.map((edge) => {
          const a = layout.positions.get(edge.src),
            b = layout.positions.get(edge.dst);
          return a && b ? (
            <path
              key={edge.id}
              className={
                edge.type === "CONTAINS" ? "edge-contains" : "edge-depends"
              }
              d={pathBetween(a, b, edge.type)}
            />
          ) : null;
        })}
      </svg>
      {nodes.map((node) => {
        const p = layout.positions.get(node.id);
        if (!p) return null;
        const runnable = node.allowed_actions.includes("run");
        return (
          <article
            key={node.id}
            data-node-id={node.id}
            className={`gnode ${node.ui_state} ${selected === node.id ? "selected" : ""}`}
            onContextMenu={(event) => {
              event.preventDefault();
              onSelect(node.id);
              onContextMenu(node, event.clientX, event.clientY);
            }}
            style={{
              transform: `translate(${p.x + 48}px,${p.y + 48}px)`,
              width: NODE_WIDTH,
              height: NODE_HEIGHT,
            }}
          >
            <button
              className="node-main"
              onClick={() => onSelect(node.id)}
              aria-label={`${node.objective}, ${stateLabel(node)}`}
            >
              <span className="node-glyph">
                <Icon
                  name={
                    node.agent?.type_id === "planner" ? "git-branch" : "bot"
                  }
                />
              </span>
              <span className="node-copy">
                <strong title={node.objective}>{node.objective}</strong>
                <small>
                  {stateLabel(node)} ·{" "}
                  {node.agent?.harness ?? node.executor ?? "agent"}
                </small>
                <small>
                  {node.agent?.model || "default model"} ·{" "}
                  {node.agent?.reasoning ?? "default"} ·{" "}
                  {tokens(usage[node.id])
                    ? `${tokens(usage[node.id]).toLocaleString()} tok`
                    : "—"}
                </small>
              </span>
            </button>
            {(runnable || node.status === "RUNNING") && (
              <button
                className={`node-run ${node.generation_active ? "spinning" : ""}`}
                disabled={!runnable || node.generation_active}
                onClick={() => onRun(node)}
                aria-label={
                  node.generation_active
                    ? `Generating ${node.objective}`
                    : `Run ${node.objective}`
                }
                title={
                  node.generation_active
                    ? "Model is generating"
                    : "Run this node"
                }
              >
                {node.generation_active ? (
                  <span className="run-spinner" />
                ) : (
                  <Icon name="play" />
                )}
              </button>
            )}
            <button
              className="node-menu-trigger"
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                onSelect(node.id);
                onContextMenu(node, rect.right, rect.bottom);
              }}
              aria-label={`Actions for ${node.objective}`}
              title="Node actions"
            >
              <Icon name="ellipsis" />
            </button>
          </article>
        );
      })}
    </div>
  );
}
