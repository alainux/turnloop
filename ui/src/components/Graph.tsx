import { useEffect, useMemo, useState } from "react";
import type { Edge, FlowEdge, GraphNode, PrimaryNodeAction, Trigger, Usage, WorkItem } from "../domain";
import {
  displayNodeTitle,
  organizationManagerPhase,
  primaryNodeAction,
  primaryNodeActionLabel,
  capabilityTooltip,
  tokens,
} from "../domain";
import {
  layoutDendrogram,
  NODE_HEIGHT,
  NODE_WIDTH,
  GRAPH_PADDING,
  TRIGGER_NODE_SIZE,
  triggerLayoutId,
  returnPathBetween,
  displayEdges,
} from "../layout";
import { Icon } from "./Icon";

interface Props {
  nodes: GraphNode[];
  edges: Edge[];
  flowEdges: FlowEdge[];
  workItems?: WorkItem[];
  usage: Record<string, Usage>;
  selected: string | null;
  onSelect: (id: string) => void;
  onRun: (node: GraphNode, action: PrimaryNodeAction) => void;
  onContextMenu: (node: GraphNode, x: number, y: number) => void;
  triggers: Trigger[];
  selectedTrigger: string | null;
  onTriggerSelect: (trigger: Trigger) => void;
}
export const nodeStatusLabel = (node: GraphNode) => {
  const machineState =
    node.status === "RUNNING" || node.generation_active
      ? node.agent_state ?? (node.generation_active ? "generating" : "starting")
      : node.ui_state.replaceAll("_", " ");
  const message =
    node.status === "RUNNING" || node.generation_active
      ? node.agent_message?.trim()
      : "";
  return message ? `${machineState} — ${message}` : machineState;
};

/** The glyph identifies the configured agent role; it is not a run indicator. */
export const nodeAgentIcon = (node: GraphNode): string =>
  node.agent?.type_id === "planner"
    ? "git-branch"
    : node.agent?.type_id === "verifier"
      ? "check"
      : "bot";

export const triggerIcon = (trigger: Trigger): string =>
  trigger.kind === "schedule" ? "calendar" : "activity";

export const nodeRunIcon = (
  active: boolean,
  action: PrimaryNodeAction | null = null,
  freshRun = false,
): string => {
  if (active || action === "cancel") return "stop";
  if (freshRun || action === "retry" || action === "regenerate") return "rotate-cw";
  return "play";
};

export const nodeRunLabel = (
  active: boolean,
  action: PrimaryNodeAction,
  freshRun = false,
): string => (active ? "Stop" : freshRun ? "Run again" : primaryNodeActionLabel(action));

export function Graph({
  nodes,
  edges,
  flowEdges,
  workItems = [],
  usage,
  selected,
  onSelect,
  onRun,
  onContextMenu,
  triggers,
  selectedTrigger,
  onTriggerSelect,
}: Props) {
  const visibleEdges = useMemo(() => displayEdges(nodes, edges), [nodes, edges]);
  // `displayEdges` is the single workflow projection: it keeps composition
  // entry branches and all reduced handoffs, so layout and rendering share
  // the same sequence/fan-out/fan-in geometry.
  const [layout, setLayout] = useState<Awaited<ReturnType<typeof layoutDendrogram>> | null>(null);
  useEffect(() => {
    let cancelled = false;
    void layoutDendrogram(nodes, edges, triggers)
      .then((nextLayout) => {
        if (!cancelled) setLayout(nextLayout);
      })
      .catch((error: unknown) => {
        if (!cancelled) console.error("[Turn] Unable to lay out workgraph", error);
      });
    return () => {
      cancelled = true;
    };
  }, [nodes, edges, triggers]);
  if (!layout) {
    return <div className="graph-canvas graph-layout-pending" aria-label="Arranging workgraph" />;
  }
  const finalDepth = layout.stageCount - 1;
  const finalStageNodeCount = nodes.filter(
    (node) => layout.positions.get(node.id)?.depth === finalDepth,
  ).length;
  const startDepth = Math.min(
    ...nodes.map((node) => layout.positions.get(node.id)?.depth ?? finalDepth),
  );
  const stageHasNodes = (depth: number) =>
    nodes.some((node) => layout.positions.get(node.id)?.depth === depth);
  const stageHasTriggers = (depth: number) =>
    triggers.some((trigger) => layout.positions.get(triggerLayoutId(trigger))?.depth === depth);
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
              left: (layout.stageXs[depth] ?? depth * (NODE_WIDTH + 54)) + GRAPH_PADDING,
            }}
          >
            {stageHasTriggers(depth) && !stageHasNodes(depth)
              ? "Triggers"
              : depth === startDepth
              ? "Start"
              : depth === finalDepth && finalStageNodeCount === 1
                ? "Final integration"
                : depth === finalDepth
                  ? "Final stage"
                  : `Stage ${depth - startDepth + 2}`}
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
            id="workflow-arrow"
            viewBox="0 0 6 6"
            refX="5"
            refY="3"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
            markerUnits="strokeWidth"
          >
            <path d="M0,0 L6,3 L0,6 z" fill="var(--border-strong)" />
          </marker>
          <marker
            id="return-arrow"
            viewBox="0 0 6 6"
            refX="5"
            refY="3"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
            markerUnits="strokeWidth"
          >
            <path d="M0,0 L6,3 L0,6 z" fill="var(--red)" />
          </marker>
        </defs>
        {visibleEdges.map((edge) => {
          const a = layout.positions.get(edge.src),
            b = layout.positions.get(edge.dst);
          const path = layout.edgePaths.get(edge.id);
          return a && b && path ? (
            <path
              key={edge.id}
              className="edge-workflow"
              d={path}
              markerEnd="url(#workflow-arrow)"
            />
          ) : null;
        })}
        {layout.triggerEdges.map((edge) => {
          const path = layout.edgePaths.get(edge.id);
          return path ? (
            <path
              key={edge.id}
              className="edge-trigger"
              d={path}
              markerEnd="url(#workflow-arrow)"
              data-trigger-edge="true"
            />
          ) : null;
        })}
        {flowEdges.map((edge) => {
          const a = layout.positions.get(edge.src),
            b = layout.positions.get(edge.dst);
          return a && b ? (
            <path
              key={edge.id}
              className="edge-flow-return"
              d={returnPathBetween(a, b)}
              data-edge-type={edge.type}
              data-flow-edge="true"
              markerEnd="url(#return-arrow)"
            />
          ) : null;
        })}
      </svg>
      {triggers.map((trigger) => {
        const position = layout.positions.get(triggerLayoutId(trigger));
        if (!position) return null;
        return (
          <button
            key={trigger.id}
            className={`graph-trigger-node ${selectedTrigger === trigger.id ? "selected" : ""} ${trigger.enabled ? "" : "disabled"}`}
            style={{
              transform: `translate(${position.x + GRAPH_PADDING + (NODE_WIDTH - TRIGGER_NODE_SIZE) / 2}px,${position.y + GRAPH_PADDING + (NODE_HEIGHT - TRIGGER_NODE_SIZE) / 2}px)`,
            }}
            onClick={() => onTriggerSelect(trigger)}
            aria-label={`Trigger ${trigger.kind === "schedule" ? "schedule" : trigger.event_name ?? "event"}`}
            aria-pressed={selectedTrigger === trigger.id}
            aria-disabled={!trigger.enabled}
            title={trigger.kind === "schedule" ? "Schedule trigger" : `Event: ${trigger.event_name ?? ""}`}
          >
            <Icon name={triggerIcon(trigger)} />
          </button>
        );
      })}
      {nodes.map((node) => {
        const p = layout.positions.get(node.id);
        if (!p) return null;
        // The action projection is authoritative, but keep the status guard
        // here as a last line of defense against a stale event arriving while
        // a completed/cancelled PTY is being released.
        const running = node.status === "RUNNING" || node.generation_active;
        const preparing = node.ui_state === "preparing";
        const active = node.allowed_actions.includes("cancel") && (running || preparing);
        const primaryAction = primaryNodeAction(node);
        const runAction = active ? "cancel" : primaryAction;
        const actionable = runAction !== null;
        const freshRun = node.ui_state === "cancelled";
        const runLabel = runAction ? nodeRunLabel(active, runAction, freshRun) : "";
        const title = displayNodeTitle(node);
        const capabilities = node.capability_status ?? [];
        const capabilityIds = node.agent?.capabilities ?? [];
        const capabilityTitle = capabilities.length
          ? capabilityTooltip(capabilities)
          : capabilityIds.join("\n");
        const organizationWork = node.organization_contract
          ? workItems.filter((item) => item.organization_id === node.id)
          : [];
        const managerPhase = organizationManagerPhase(node);
        return (
          <article
            key={node.id}
            data-node-id={node.id}
            className={`gnode ${node.ui_state} ${actionable ? "node-actionable" : ""} ${selected === node.id ? "selected" : ""}`}
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
                <small className={active && node.agent_message?.trim() ? "node-working-message" : undefined}>
                  {nodeStatusLabel(node)}
                </small>
                <small>
                  {node.agent?.harness ?? node.executor ?? "agent"}/{node.agent?.model || "default model"} ·{" "}
                  {node.agent?.reasoning ?? "default"} ·{" "}
                  {tokens(usage[node.id])
                    ? `${tokens(usage[node.id]).toLocaleString()} tok`
                    : "—"}
                </small>
                {node.organization_contract && (
                  <small className="node-organization-state">
                    {managerPhase ? `Manager ${managerPhase.replaceAll("_", " ")}` : "Organization"}
                    {organizationWork.length > 0
                      ? ` · ${organizationWork.filter((item) => item.status === "COMPLETE").length}/${organizationWork.length} work done`
                      : ""}
                  </small>
                )}
                {node.control_activity && (
                  <small className="node-control-activity">
                    {node.control_activity.kind === "plan_audit" ? "Plan audit running…" : "Manager review running…"}
                  </small>
                )}
              </span>
              <span className="node-icons">
                <span className="node-glyph">
                  <Icon name={nodeAgentIcon(node)} />
                </span>
                {capabilityIds.length > 0 && (
                  <span
                    className="node-capability-indicator"
                    title={capabilityTitle}
                    aria-label={capabilityTitle}
                  >
                    <Icon name="plug" />
                  </span>
                )}
              </span>
            </button>
            {runAction && (
              <button
                className={`node-run ${active ? "running" : ""}`}
                onClick={() => onRun(node, runAction)}
                aria-label={`${runLabel} ${title}`}
                title={
                  active ? "Stop this node" : runLabel
                }
              >
                <Icon name={nodeRunIcon(active, runAction, freshRun)} />
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
