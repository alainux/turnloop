import { useEffect, useState } from "react";
import type { Trigger } from "../domain";
import { deleteTrigger, updateTrigger } from "../api/triggers";
import { Icon } from "./Icon";

function formatTriggerData(data: Record<string, unknown> | undefined): string {
  return JSON.stringify(data ?? {}, null, 2);
}

interface Props {
  trigger: Trigger;
  onClose: () => void;
  onChanged: () => Promise<void>;
  notify: (text: string) => void;
}

export function TriggerInspector({ trigger, onClose, onChanged, notify }: Props) {
  const [eventName, setEventName] = useState(trigger.event_name ?? "");
  const [kind, setKind] = useState(trigger.kind);
  const [schedule, setSchedule] = useState(trigger.schedule ?? "");
  const [dataText, setDataText] = useState(formatTriggerData(trigger.data));
  const [enabled, setEnabled] = useState(trigger.enabled);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setEventName(trigger.event_name ?? "");
    setKind(trigger.kind);
    setSchedule(trigger.schedule ?? "");
    setDataText(formatTriggerData(trigger.data));
    setEnabled(trigger.enabled);
  }, [trigger.id, trigger.updated_at]);

  const save = async () => {
    setBusy(true);
    try {
      const parsedData: unknown = JSON.parse(dataText.trim() || "{}");
      if (parsedData === null || Array.isArray(parsedData) || typeof parsedData !== "object") {
        throw new Error("Additional data must be a JSON object");
      }
      await updateTrigger(trigger.id, {
        event_name: kind === "event" ? eventName.trim() : null,
        kind,
        schedule: kind === "schedule" ? schedule.trim() : null,
        data: parsedData as Record<string, unknown>,
        enabled,
      });
      await onChanged();
      notify("Trigger updated");
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!confirm("Delete this trigger?")) return;
    setBusy(true);
    try {
      await deleteTrigger(trigger.id);
      onClose();
      await onChanged();
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <aside className="inspector trigger-inspector" id="inspector">
      <div className="panel-heading">
        <span>Inspector</span>
        <button className="quiet-icon" onClick={onClose} aria-label="Close trigger inspector">
          <Icon name="panel-right-close" />
        </button>
      </div>
      <div className="detail trigger-detail">
        <div className="trigger-kicker">
          <span className="trigger-kicker-icon"><Icon name={kind === "schedule" ? "calendar" : "activity"} /></span>
          <span>{kind === "schedule" ? "Scheduled trigger" : "Event trigger"}</span>
          <span className={`trigger-enabled ${enabled ? "is-enabled" : ""}`}>{enabled ? "Enabled" : "Disabled"}</span>
        </div>
        <section className="section">
          <div className="section-heading"><span>Configuration</span></div>
          <label className="field">
            <span>Type</span>
            <select value={kind} onChange={(event) => setKind(event.target.value as Trigger["kind"])}>
              <option value="event">Event</option>
              <option value="schedule">Schedule</option>
            </select>
          </label>
          {kind === "event" && (
            <label className="field">
              <span>Event name</span>
              <input value={eventName} onChange={(event) => setEventName(event.target.value)} />
              <small>Only this exact name activates the trigger.</small>
            </label>
          )}
          {kind === "schedule" && (
            <label className="field">
              <span>Schedule</span>
              <input value={schedule} onChange={(event) => setSchedule(event.target.value)} placeholder="*/5 * * * *" />
              <small>Use five-field UTC cron.</small>
            </label>
          )}
          <label className="field">
            <span>Additional event data</span>
            <textarea
              value={dataText}
              onChange={(event) => setDataText(event.target.value)}
              rows={4}
              spellCheck={false}
            />
            <small>Merged with emitted event data; existing event values are kept.</small>
          </label>
          <label className="check">
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
            Enabled
          </label>
          <button
            className="button accent"
            disabled={busy || (kind === "event" ? !eventName.trim() : !schedule.trim())}
            onClick={() => void save()}
          >
            Save trigger
          </button>
        </section>
        <section className="section trigger-actions">
          <button className="button compact danger" disabled={busy} onClick={() => void remove()}>Delete trigger</button>
        </section>
      </div>
    </aside>
  );
}
