import { FormEvent, useMemo, useState } from "react";
import type { LeadTranscriptEntry, ProjectLead } from "../domain";
import { cancelLead, sendLeadMessage, waitLead } from "../api/lead";
import { Icon } from "./Icon";

interface Props {
  projectId: string;
  lead: ProjectLead | null;
  transcript: LeadTranscriptEntry[];
  bootstrapStatus: string;
  onOpenTerminal: () => void;
  onChanged: () => Promise<void>;
}

function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? ""
    : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function LeadChat({
  projectId,
  lead,
  transcript,
  bootstrapStatus,
  onOpenTerminal,
  onChanged,
}: Props) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [optimistic, setOptimistic] = useState<LeadTranscriptEntry[]>([]);
  const entries = useMemo(() => [...transcript, ...optimistic], [transcript, optimistic]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!message || !lead) return;
    setDraft("");
    setBusy(true);
    setError(null);
    const optimisticUser: LeadTranscriptEntry = {
      id: `pending-user-${Date.now()}`,
      project_id: projectId,
      role: "user",
      content: message,
      event_name: null,
      status: "QUEUED",
      run_id: null,
      created_at: new Date().toISOString(),
    };
    setOptimistic((current) => [...current, optimisticUser]);
    try {
      const response = await sendLeadMessage(projectId, message);
      // The API acknowledges the durable USER entry immediately. The Lead
      // reply arrives through the normal graph/event refresh after its next
      // safe retained-session turn.
      void response;
      await onChanged();
      setOptimistic([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const sleep = async () => {
    if (!lead || busy || waiting) return;
    setWaiting(true);
    setError(null);
    try {
      await waitLead(projectId);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWaiting(false);
    }
  };

  const cancel = async () => {
    if (!lead || lead.status !== "RUNNING") return;
    setWaiting(true);
    setError(null);
    try {
      await cancelLead(projectId);
      await onChanged();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setWaiting(false);
    }
  };

  const leadWaiting = lead?.status === "DORMANT" || (
    lead?.status === "RUNNING" && entries.some(
      (entry) => entry.role === "user" && entry.status === "QUEUED",
    )
  );

  return (
    <div className="lead-chat" aria-label="Lead Chat">
      <div className="lead-chat-heading">
        <div>
          <span className="eyebrow">Project conversation</span>
          <h1>Lead Chat</h1>
          <p>
            Talk to the Project Lead while the organization plans and works behind the scenes.
          </p>
        </div>
        <div className="lead-chat-actions">
          <span className={`lead-chat-status is-${(lead?.status ?? "IDLE").toLowerCase()}`}>
            <span aria-hidden="true" />
            {leadWaiting
              ? "Waiting for Lead"
              : lead?.status === "DORMANT"
                ? "Dormant"
                : bootstrapStatus === "BOOTSTRAPPING"
                  ? "Framing the work"
                  : "Listening"}
          </span>
          <button className="button quiet" type="button" onClick={onOpenTerminal}>
            <Icon name="terminal" /> Raw terminal
          </button>
          {lead?.status === "RUNNING" && (
            <button
              className="button quiet"
              type="button"
              onClick={() => void cancel()}
              disabled={waiting}
              title="Stop the current Lead Run before changing its assignment"
            >
              Stop turn
            </button>
          )}
          <button
            className="button quiet"
            type="button"
            onClick={() => void sleep()}
            disabled={!lead || lead.status === "DORMANT" || busy || waiting}
            title="Pause lead inference until you message it or a meaningful project event occurs"
          >
            {waiting ? <Icon name="loader" /> : <Icon name="pause" />}
            {lead?.status === "DORMANT" ? "Waiting" : "Wait"}
          </button>
        </div>
      </div>
      <div className="lead-chat-transcript" aria-live="polite">
        {entries.map((entry) => (
          <article className={`lead-chat-message is-${entry.role}`} key={entry.id}>
            <div className="lead-chat-message-meta">
              <strong>{entry.role === "user" ? "You" : entry.role === "lead" ? "Lead" : "Turn update"}</strong>
              <time>{timeLabel(entry.created_at)}</time>
            </div>
            <p>{entry.content}</p>
          </article>
        ))}
        {(busy || lead?.status === "RUNNING") && (
          <article className="lead-chat-message is-lead is-pending">
            <div className="lead-chat-message-meta"><strong>Lead</strong></div>
            <p className="lead-chat-thinking"><Icon name="loader" /> Thinking through the project…</p>
          </article>
        )}
        {entries.length === 0 && !busy && <p className="hint">The Lead has no conversation history yet.</p>}
      </div>
      <form className="lead-chat-composer" onSubmit={(event) => void submit(event)}>
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Ask what is happening, redirect the plan, or request a project action…"
          aria-label="Message Project Lead"
          rows={3}
          disabled={!lead}
        />
        <div className="lead-chat-composer-footer">
          <span className="hint">Every reply is retained here; the raw model terminal stays inspectable.</span>
          <button className="button accent" type="submit" disabled={!draft.trim() || !lead}>
            <Icon name="arrow-up" /> Send
          </button>
        </div>
      </form>
      {error && <p className="lead-chat-error" role="alert">{error}</p>}
    </div>
  );
}
