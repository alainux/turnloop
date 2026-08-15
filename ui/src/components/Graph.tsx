import { useMemo } from "react";
import type { Edge, GraphNode, Usage } from "../domain";
import { primaryNodeAction, tokens } from "../domain";
import {
  layoutDendrogram,
  NODE_HEIGHT,
  NODE_WIDTH,
  GRAPH_PADDING,
  pathBetween,
  displayEdges,
} from "../layout";
import { Icon } from "./Icon";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  usage: Record<string, Usage>;
  selected: string | null;
  onSelect: (id: string) => void;
  onRun: (node: GraphNode, action: "run" | "cancel") => void;
  onContextMenu: (node: GraphNode, x: number, y: number) => void;
}
export const nodeStatusLabel = (node: GraphNode) => {
  const machineState =
    node.status === "RUNNING"
      ? node.agent_state ?? (node.generation_active ? "generating" : "starting")
      : node.ui_state.replaceAll("_", " ");
  const message = node.status === "RUNNING" ? node.agent_message?.trim() : "";
  return message ? `${machineState} — ${message}` : machineState;
};
export function Graph({
  nodes,
  edges,
  usage,
  selected,
  onSelect,
  onRun,
  onContextMenu,
}: Props) {
  const visibleEdges = useMemo(() => displayEdges(nodes, edges), [nodes, edges]);
  const layout = useMemo(() => layoutDendrogram(nodes, visibleEdges), [nodes, visibleEdges]);
  return (
    <div
      className="graph-canvas"
      style={{
        width: layout.width + GRAPH_PADDING * 2,
        height: layout.height + GRAPH_PADDING * 2,
      }}
    >
      <svg
        className="graph-edges"
        width={layout.width + GRAPH_PADDING * 2}
        height={layout.height + GRAPH_PADDING * 2}
        aria-hidden="true"
      >
        {visibleEdges.map((edge) => {
          const a = layout.positions.get(edge.src),
            b = layout.positions.get(edge.dst);
          return a && b ? (
            <path
              key={edge.id}
              className="edge-workflow"
              d={pathBetween(a, b, edge.type)}
            />
          ) : null;
        })}
      </svg>
      {nodes.map((node) => {
        const p = layout.positions.get(node.id);
        if (!p) return null;
        // The action projection is authoritative, but keep the status guard
        // here as a last line of defense against a stale event arriving while
        // a completed/cancelled PTY is being released.
        const runnable =
          node.allowed_actions.includes("run") && node.status !== "COMPLETE" && node.status !== "RUNNING";
        const running = node.status === "RUNNING";
        const primaryAction = primaryNodeAction(node);
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
              transform: `translate(${p.x + GRAPH_PADDING}px,${p.y + GRAPH_PADDING}px)`,
              width: NODE_WIDTH,
              height: NODE_HEIGHT,
            }}
          >
            <button
              className="node-main"
              onClick={() => onSelect(node.id)}
              aria-label={`${node.objective}, ${nodeStatusLabel(node)}`}
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
                <small className={node.status === "RUNNING" && node.agent_message?.trim() ? "node-working-message" : undefined}>
                  {nodeStatusLabel(node)}
                </small>
                <small>
                  {node.agent?.harness ?? node.executor ?? "agent"}/{node.agent?.model || "default model"} ·{" "}
                  {node.agent?.reasoning ?? "default"} ·{" "}
                  {tokens(usage[node.id])
                    ? `${tokens(usage[node.id]).toLocaleString()} tok`
                    : "—"}
                </small>
              </span>
            </button>
            {(runnable || running) && primaryAction && (
              <button
                className={`node-run ${running ? "running" : ""}`}
                onClick={() => onRun(node, running ? "cancel" : "run")}
                aria-label={
                  running
                    ? `Stop ${node.objective}`
                    : `Run ${node.objective}`
                }
                title={
                  running
                    ? "Stop this node"
                    : "Run this node"
                }
              >
                <Icon name={running ? "square-stop" : "play"} />
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
