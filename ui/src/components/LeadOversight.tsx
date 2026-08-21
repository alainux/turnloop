import { useMemo } from "react";
import type { GraphNode, ProjectLead, ReviewRequest, Run } from "../domain";
import { Icon } from "./Icon";
import { TerminalView } from "./TerminalView";

/**
 * The project lead is oversight identity, not a graph node: exactly one per
 * project, with its own durable terminal and retained session. This surface
 * keeps it visible outside the DAG and exposes the review trail it owns.
 * Interaction happens through the lead's terminal, like every other agent.
 */

interface OversightProps {
  lead: ProjectLead | null;
  bootstrapStatus: string;
  reviews: ReviewRequest[];
  onOpen: () => void;
}

export function LeadOversight({ lead, bootstrapStatus, reviews, onOpen }: OversightProps) {
  if (!lead) return null;
  const openReviews = reviews.filter(
    (item) => item.status === "PENDING" || item.status === "ACTIVE",
  );
  const running = lead.status === "RUNNING";
  const cue = openReviews.length > 0
    ? `${openReviews.length} review${openReviews.length > 1 ? "s" : ""} waiting`
    : bootstrapStatus === "BOOTSTRAPPING"
      ? "Bootstrapping"
      : null;
  return (
    <button
      className="lead-oversight"
      onClick={onOpen}
      title="Project lead — oversight, reviews and escalations"
      aria-label="Open project lead inspector"
    >
      <span className={`lead-status-dot ${running ? "is-running" : ""}`} aria-hidden="true" />
      <span className="lead-oversight-label">Lead</span>
      <span className="lead-oversight-state">{running ? "Working" : "Idle"}</span>
      {cue && <span className="lead-cue">{cue}</span>}
    </button>
  );
}

interface InspectorProps {
  projectId: string;
  lead: ProjectLead;
  bootstrapStatus: string;
  reviews: ReviewRequest[];
  runs: Run[];
  onClose: () => void;
}

function reviewTitle(kind: ReviewRequest["kind"]): string {
  return kind === "PLAN_REVIEW"
    ? "Plan review"
    : kind === "COMPLETION_REVIEW"
      ? "Completion review"
      : "Escalation";
}

export function LeadInspector({
  projectId,
  lead,
  bootstrapStatus,
  reviews,
  runs,
  onClose,
}: InspectorProps) {
  // The lead's terminal is keyed by its stable terminal owner id, so the
  // ordinary shell endpoint attaches to the lead's own durable pane.
  const terminalNode = useMemo(
    () =>
      ({
        id: lead.terminal_owner_id,
        project_id: projectId,
        objective: "Project lead",
        status: "RUNNING",
        runtime_guard: null,
        control_activity: null,
      }) as unknown as GraphNode,
    [lead.terminal_owner_id, projectId],
  );

  return (
    <aside className="inspector lead-inspector" id="inspector">
      <div className="panel-heading">
        <span>Inspector</span>
        <button className="quiet-icon" onClick={onClose} aria-label="Close lead inspector">
          <Icon name="panel-right-close" />
        </button>
      </div>
      <div className="detail">
        <div className="trigger-kicker">
          <span className="trigger-kicker-icon"><Icon name="user" /></span>
          <span>Project lead</span>
          <span className={`trigger-enabled ${lead.status === "RUNNING" ? "is-enabled" : ""}`}>
            {lead.status === "RUNNING" ? "Working" : "Idle"}
          </span>
        </div>
        <section className="section">
          <div className="section-heading"><span>Identity</span></div>
          <div className="lead-meta">
            <span>{lead.agent ? `${lead.agent.harness} · ${lead.agent.model ?? "default model"}` : "No agent configured"}</span>
            <span className="hint">
              {lead.session_id ? `Session retained · ${lead.session_id.slice(0, 12)}` : "No session yet"}
            </span>
            <span className="hint">Bootstrap: {bootstrapStatus === "BOOTSTRAPPING" ? "Bootstrapping" : "Ready"}</span>
          </div>
        </section>
        <section className="section">
          <div className="section-heading"><span>Terminal</span></div>
          <div className="terminal-host terminal-tab-panel">
            <TerminalView node={terminalNode} runs={runs} />
          </div>
        </section>
        <section className="section">
          <div className="section-heading"><span>Review trail</span></div>
          {reviews.length === 0 ? (
            <p className="hint">No reviews yet.</p>
          ) : (
            <ul className="review-list">
              {reviews.slice(0, 10).map((item) => (
                <li key={item.id} className={`review-item is-${item.status.toLowerCase()}`}>
                  <span className="review-kind">{reviewTitle(item.kind)}</span>
                  <span className="review-status">
                    {item.status === "SETTLED"
                      ? `${item.decision === "APPROVE" ? "Approved" : "Rejected"}`
                      : item.status === "ACTIVE"
                        ? "Reviewing"
                        : "Waiting"}
                  </span>
                  {item.summary && <p className="review-summary">{item.summary}</p>}
                  {item.required_changes.length > 0 && (
                    <ul className="review-changes">
                      {item.required_changes.map((change, index) => (
                        <li key={index}>{change}</li>
                      ))}
                    </ul>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </aside>
  );
}
