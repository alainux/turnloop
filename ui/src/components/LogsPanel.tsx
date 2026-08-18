import { useEffect, useMemo, useState } from "react";
import { getProjectLogs, type LogRecord } from "../api/logs";
import { Icon } from "./Icon";

export function LogsPanel({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const [search, setSearch] = useState("");
  const [records, setRecords] = useState<LogRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    addEventListener("keydown", closeOnEscape);
    return () => removeEventListener("keydown", closeOnEscape);
  }, [onClose]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    void getProjectLogs(projectId, search)
      .then((value) => { if (active) setRecords(value.records); })
      .catch((reason) => { if (active) setError(String(reason)); })
      .finally(() => { if (active) setLoading(false); });
    const stream = new EventSource(
      `/api/projects/${encodeURIComponent(projectId)}/logs/stream?search=${encodeURIComponent(search)}`,
    );
    stream.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as { type?: string; record?: LogRecord };
        if (active && message.type === "log" && message.record) {
          setRecords((current) => current.some((item) => item.event_id === message.record!.event_id)
            ? current
            : [...current, message.record!]);
        }
      } catch {
        // Ignore malformed external records; the next snapshot remains usable.
      }
    };
    stream.onerror = () => { if (active) setError("Log stream disconnected; retrying…"); };
    return () => { active = false; stream.close(); };
  }, [projectId, search]);

  const visible = useMemo(() => records.slice(-10000), [records]);
  return (
    <section className="logs-panel" role="dialog" aria-modal="true" aria-labelledby="logs-title">
      <header className="logs-head">
        <div><p className="eyebrow">Project event history</p><h2 id="logs-title">Logs</h2></div>
        <div className="logs-head-actions">
          <label className="logs-search"><Icon name="search" /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search logs" aria-label="Search logs" autoFocus /></label>
          <button className="quiet-icon" onClick={onClose} aria-label="Close logs" title="Close logs"><Icon name="x" /></button>
        </div>
      </header>
      <div className="logs-meta" aria-live="polite">{loading ? "Loading…" : `${visible.length.toLocaleString()} records`}{error && <span className="logs-error">{error}</span>}</div>
      <div className="logs-list">
        {!loading && !visible.length && <div className="logs-empty">No log records match this search.</div>}
        {visible.map((record) => (
          <details className={`log-line log-${record.status}`} key={record.event_id}>
            <summary><time dateTime={record.timestamp}>{formatTime(record.timestamp)}</time><span className="log-kind">{record.kind}</span><span className="log-message">{record.message || record.action || "—"}</span><span className="log-source">{record.source}</span></summary>
            <pre>{JSON.stringify(record, null, 2)}</pre>
          </details>
        ))}
      </div>
    </section>
  );
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}
