import { useMemo, useState } from "react";
import type { GraphNode, WorkItem, WorkItemStatus } from "../domain";
import { displayNodeTitle } from "../domain";

type Filter = "ALL" | "BACKLOG" | "ACTIVE" | "BLOCKED" | "COMPLETE";

interface Props {
  items: WorkItem[];
  nodes: GraphNode[];
  onSelectNode: (nodeId: string) => void;
}

const filterLabels: Array<[Filter, string]> = [
  ["ALL", "All"],
  ["BACKLOG", "Backlog"],
  ["ACTIVE", "Active"],
  ["BLOCKED", "Blocked"],
  ["COMPLETE", "Done"],
];

function isActive(status: WorkItemStatus): boolean {
  return ["ACTIVE", "READY", "CLAIMED", "RUNNING"].includes(status);
}

function matches(item: WorkItem, filter: Filter): boolean {
  if (filter === "ALL") return true;
  if (filter === "ACTIVE") return isActive(item.status);
  return item.status === filter;
}

function statusLabel(status: WorkItemStatus): string {
  if (isActive(status)) return "ACTIVE";
  if (status === "COMPLETE") return "DONE";
  return status;
}

export function WorkView({ items, nodes, onSelectNode }: Props) {
  const [filter, setFilter] = useState<Filter>("ALL");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const nodeNames = useMemo(
    () => new Map(nodes.map((node) => [node.id, displayNodeTitle(node)])),
    [nodes],
  );
  const counts = useMemo(() => ({
    all: items.length,
    backlog: items.filter((item) => item.status === "BACKLOG").length,
    active: items.filter((item) => isActive(item.status)).length,
    blocked: items.filter((item) => item.status === "BLOCKED").length,
    done: items.filter((item) => item.status === "COMPLETE").length,
  }), [items]);
  const visible = useMemo(
    () => items
      .filter((item) => matches(item, filter))
      .slice()
      .sort((left, right) => right.priority - left.priority || right.updated_at.localeCompare(left.updated_at)),
    [items, filter],
  );
  const selected = items.find((item) => item.id === selectedId) ?? null;
  const focusedWorkflow = nodes.some(
    (node) => node.parent_id === null && node.organization_contract?.scale === "focused",
  );
  return (
    <section className="work-view" aria-label="Project work">
      <header className="work-heading">
        <div>
          <span className="eyebrow">WORK</span>
          <h1>Work</h1>
          <p>Durable work items stay here even when only the active slice is materialized in the graph.</p>
        </div>
      </header>
      <div className="work-filters" role="tablist" aria-label="Work status">
        {filterLabels.map(([value, label]) => {
          const count = value === "ALL"
            ? counts.all
            : value === "BACKLOG"
              ? counts.backlog
              : value === "ACTIVE"
                ? counts.active
                : value === "BLOCKED"
                  ? counts.blocked
                  : counts.done;
          return (
            <button
              key={value}
              role="tab"
              aria-selected={filter === value}
              className={filter === value ? "selected" : ""}
              onClick={() => setFilter(value)}
            >
              {label} <span>{count}</span>
            </button>
          );
        })}
      </div>
      {visible.length === 0 ? (
        <div className="work-empty">
          {focusedWorkflow
            ? "This focused workflow has no durable work-item backlog; run its graph steps directly."
            : "No work in this view."}
        </div>
      ) : (
        <div className="work-list">
          {visible.map((item) => {
            const materialized = item.node_id && nodeNames.has(item.node_id);
            return (
              <article className={`work-row ${selectedId === item.id ? "selected" : ""}`} key={item.id}>
                <div className={`work-status work-status-${item.status.toLowerCase()}`}>
                  {statusLabel(item.status)}
                </div>
                <button
                  className="work-row-select"
                  onClick={() => setSelectedId(item.id)}
                  aria-label={`Inspect work item ${item.title}`}
                >
                  <strong>{item.title}</strong>
                  <span>{item.objective}</span>
                  <small>
                    {nodeNames.get(item.organization_id) ?? "Organization"}
                    {item.acceptance_criteria.length > 0
                      ? ` · ${item.acceptance_criteria.length} acceptance ${item.acceptance_criteria.length === 1 ? "criterion" : "criteria"}`
                      : ""}
                  </small>
                </button>
                <div className="work-row-meta">
                  <span>Priority {item.priority}</span>
                  {materialized ? (
                    <button className="quiet-link" onClick={() => onSelectNode(item.node_id!)}>
                      Open node
                    </button>
                  ) : (
                    <span className="work-unmaterialized">Backlog</span>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {selected && (
        <WorkItemDetail
          item={selected}
          items={items}
          organizationName={nodeNames.get(selected.organization_id) ?? "Organization"}
          nodeName={selected.node_id ? nodeNames.get(selected.node_id) : undefined}
          onSelectNode={onSelectNode}
        />
      )}
    </section>
  );
}

function WorkItemDetail({
  item,
  items,
  organizationName,
  nodeName,
  onSelectNode,
}: {
  item: WorkItem;
  items: WorkItem[];
  organizationName: string;
  nodeName?: string;
  onSelectNode: (nodeId: string) => void;
}) {
  const dependencies = item.depends_on.map((id) =>
    items.find((candidate) => candidate.id === id),
  );
  return (
    <aside className="work-detail" aria-label={`Work item detail: ${item.title}`}>
      <div className="work-detail-heading">
        <div>
          <span className="eyebrow">WORK ITEM</span>
          <h2>{item.title}</h2>
        </div>
        <span className={`work-status work-status-${item.status.toLowerCase()}`}>
          {statusLabel(item.status)}
        </span>
      </div>
      <dl className="work-detail-facts">
        <div><dt>Objective</dt><dd>{item.objective}</dd></div>
        <div><dt>Organization</dt><dd>{organizationName}</dd></div>
        <div><dt>Assignment</dt><dd>{item.claimed_by ? `Claimed by ${item.claimed_by}` : "Backlog — not materialized"}</dd></div>
        <div><dt>Priority</dt><dd>{item.priority}</dd></div>
      </dl>
      {nodeName && item.node_id && (
        <button className="quiet-link" onClick={() => onSelectNode(item.node_id!)}>
          Open materialized node · {nodeName}
        </button>
      )}
      {item.rejection_reason && (
        <p className="work-detail-warning"><strong>Block or rejection reason:</strong> {item.rejection_reason}</p>
      )}
      <div className="work-detail-columns">
        <div>
          <h3>Dependencies</h3>
          {dependencies.length > 0 ? (
            <ul>{dependencies.map((dependency, index) => <li key={item.depends_on[index]}>{dependency?.title ?? item.depends_on[index]}</li>)}</ul>
          ) : <p>None</p>}
        </div>
        <div>
          <h3>Acceptance criteria</h3>
          {item.acceptance_criteria.length > 0 ? (
            <ul>{item.acceptance_criteria.map((criterion) => <li key={criterion.id}>{criterion.description}</li>)}</ul>
          ) : <p>None recorded</p>}
        </div>
      </div>
      <div className="work-detail-columns">
        <div><h3>Evidence</h3><p>{item.evidence_refs.length ? item.evidence_refs.join(", ") : "No evidence refs"}</p></div>
        <div><h3>Artifacts</h3><p>{item.artifact_refs.length ? item.artifact_refs.join(", ") : "No artifact refs"}</p></div>
      </div>
    </aside>
  );
}
