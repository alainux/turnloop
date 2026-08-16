import { useMemo } from "react";
import type { Edge, GraphNode, Usage } from "../domain";
import {
  displayNodeTitle,
  primaryNodeAction,
  skillTooltip,
  tokens,
} from "../domain";
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
  const finalDepth = layout.stageCount - 1;
  const finalStageNodeCount = [...layout.positions.values()].filter(
    (position) => position.depth === finalDepth,
  ).length;
  return (
    <div
      className="graph-canvas"
      style={{
        width: layout.width + GRAPH_PADDING * 2,
        height: layout.height + GRAPH_PADDING * 2,
      }}
    >
      <div className="graph-stage-labels" aria-hidden="true">
        {Array.from({ length: layout.stageCount }, (_, depth) => (
          <span
            key={depth}
            className="graph-stage-label"
            style={{
              left: depth * (NODE_WIDTH + 54) + GRAPH_PADDING,
            }}
          >
            {depth === 0
              ? "Start"
              : depth === finalDepth && finalStageNodeCount === 1
                ? "Final integration"
                : depth === finalDepth
                  ? "Final stage"
                  : `Stage ${depth + 1}`}
          </span>
        ))}
      </div>
      <svg
        className="graph-edges"
        width={layout.width + GRAPH_PADDING * 2}
        height={layout.height + GRAPH_PADDING * 2}
        aria-hidden="true"
      >
        <defs>
          <marker
            id="dependency-arrow"
            viewBox="0 0 6 6"
            refX="5"
            refY="3"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
            markerUnits="strokeWidth"
          >
            <path d="M0,0 L6,3 L0,6 z" fill="var(--amber)" />
          </marker>
        </defs>
        {visibleEdges.map((edge) => {
          const a = layout.positions.get(edge.src),
            b = layout.positions.get(edge.dst);
          return a && b ? (
            <path
              key={edge.id}
              className={`edge-workflow ${edge.type === "DEPENDS_ON" ? "edge-depends" : "edge-contains"}`}
              d={pathBetween(a, b, edge.type)}
              data-edge-type={edge.type}
              markerEnd={edge.type === "DEPENDS_ON" ? "url(#dependency-arrow)" : undefined}
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
        const runnable = node.allowed_actions.includes("run");
        const running = node.status === "RUNNING";
        const preparing = node.ui_state === "preparing";
        const active = running || preparing;
        const primaryAction = primaryNodeAction(node);
        const title = displayNodeTitle(node);
        const skillRefs = node.agent?.skill_ids ?? [];
        const finalNode = p.depth === finalDepth && finalStageNodeCount === 1;
        return (
          <article
            key={node.id}
            data-node-id={node.id}
            className={`gnode ${node.ui_state} ${(runnable || active) ? "node-actionable" : ""} ${finalNode ? "graph-final-node" : ""} ${selected === node.id ? "selected" : ""}`}
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
              aria-label={`${title}, ${nodeStatusLabel(node)}`}
            >
              <span className="node-copy">
                <strong title={title}>{title}</strong>
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
              <span className="node-icons" aria-hidden={skillRefs.length === 0 ? true : undefined}>
                <span className="node-glyph">
                  <Icon
                    name={
                      preparing
                        ? "loader"
                        : node.agent?.type_id === "planner"
                        ? "git-branch"
                        : node.agent?.type_id === "verifier"
                          ? "check"
                          : "bot"
                    }
                  />
                </span>
                {skillRefs.length > 0 && (
                  <span
                    className="node-skill-indicator"
                    title={skillTooltip(skillRefs)}
                    aria-label={skillTooltip(skillRefs)}
                  >
                    <Icon name="file" />
                  </span>
                )}
              </span>
            </button>
            {(runnable || active) && primaryAction && (
              <button
                className={`node-run ${active ? "running" : ""}`}
                onClick={() => onRun(node, active ? "cancel" : "run")}
                aria-label={
                  active
                    ? `Stop ${title}`
                    : `Run ${title}`
                }
                title={
                  active
                    ? "Stop this node"
                    : "Run this node"
                }
              >
                <Icon name={active ? "stop" : "play"} />
              </button>
            )}
            <button
              className="node-menu-trigger"
              onClick={(event) => {
                const rect = event.currentTarget.getBoundingClientRect();
                onSelect(node.id);
                onContextMenu(node, rect.right, rect.bottom);
              }}
              aria-label={`Actions for ${title}`}
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
