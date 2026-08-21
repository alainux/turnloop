import { useMemo } from "react";
import type { GraphNode, ProjectLead, ReviewRequest, Run } from "../domain";
import { TerminalView } from "./TerminalView";

interface OversightProps {
  lead: ProjectLead | null;
  bootstrapStatus: string;
  reviews: ReviewRequest[];
  onOpen: () => void;
}

/** Compact Lead status affordance shown on the graph surface. */
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
      title="Open the retained Project Lead terminal"
      aria-label="Open project lead terminal"
    >
      <span className={`lead-status-dot ${running ? "is-running" : ""}`} aria-hidden="true" />
      <span className="lead-oversight-label">Lead</span>
      <span className="lead-oversight-state">{running ? "Working" : "Idle"}</span>
      {cue && <span className="lead-cue">{cue}</span>}
    </button>
  );
}

interface LeadTerminalProps {
  projectId: string;
  lead: ProjectLead;
  bootstrapStatus: string;
  reviews: ReviewRequest[];
  runs: Run[];
}

function reviewTitle(kind: ReviewRequest["kind"]): string {
  return kind === "PLAN_REVIEW"
    ? "Plan review"
    : kind === "COMPLETION_REVIEW"
      ? "Completion review"
      : "Escalation";
}

/** The primary project surface: the Lead's real provider-owned terminal. */
export function LeadTerminalView({
  projectId,
  lead,
  bootstrapStatus,
  reviews,
  runs,
}: LeadTerminalProps) {
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
  const running = lead.status === "RUNNING";

  return (
    <div className="lead-terminal-view" aria-label="Project Lead terminal">
      <div className="lead-terminal-heading">
        <div>
          <span className="eyebrow">Project lead</span>
          <h1>Lead terminal</h1>
          <p>The retained Herdr pane for the Lead provider and human steering.</p>
        </div>
        <div className={`lead-terminal-status ${running ? "is-running" : ""}`}>
          <span aria-hidden="true" />
          {running ? "Working" : bootstrapStatus === "BOOTSTRAPPING" ? "Bootstrapping" : "Idle"}
        </div>
      </div>
      <div className="lead-terminal-layout">
        <div className="lead-terminal-host">
          <TerminalView node={terminalNode} runs={runs} endpoint="terminal" />
        </div>
        <aside className="lead-terminal-context" aria-label="Lead context">
          <section className="section">
            <div className="section-heading"><span>Lead</span></div>
            <div className="lead-meta">
              <span>{lead.agent ? `${lead.agent.harness} · ${lead.agent.model ?? "default model"}` : "No agent configured"}</span>
              <span className="hint">
                {lead.session_id ? `Session retained · ${lead.session_id.slice(0, 12)}` : "No provider session yet"}
              </span>
              <span className="hint">Bootstrap: {bootstrapStatus === "BOOTSTRAPPING" ? "Bootstrapping" : "Ready"}</span>
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
                        ? item.decision === "APPROVE" ? "Approved" : "Rejected"
                        : item.status === "ACTIVE" ? "Reviewing" : "Waiting"}
                    </span>
                    {item.summary && <p className="review-summary">{item.summary}</p>}
                    {item.required_changes.length > 0 && (
                      <ul className="review-changes">
                        {item.required_changes.map((change, index) => <li key={index}>{change}</li>)}
                      </ul>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>
      </div>
      <div className="lead-terminal-note">
        Provider output and keystrokes use this pane directly. Agent messages remain durable workgraph state and are not injected into the terminal.
      </div>
    </div>
  );
}
