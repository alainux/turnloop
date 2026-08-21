import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import xtermCss from "@xterm/xterm/css/xterm.css?inline";
import type { ControlActivity, GraphNode, Run } from "../domain";
import { getProjectLogs, type LogRecord } from "../api/logs";
import { Icon } from "./Icon";

interface Props {
  node: GraphNode;
  runs: Run[];
  control?: ControlActivity | null;
}

const HERDR_OPERATOR_WARNING =
  "CAUTION: HERDR CANNOT BE LAUNCHED INSIDE SUBPROCESSES OR FROM HERDR ITSELF. " +
  "DO NOT TRY TO LAUNCH HERDR; REQUEST/USE THE ALREADY-RUNNING DAEMON.";

export function TerminalView({ node, control }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);
  const [idle, setIdle] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);
  const [runtimeGuard, setRuntimeGuard] = useState<string | null>(null);
  const [view, setView] = useState<"terminal" | "activity">("terminal");
  // The organization's own terminal is the primary surface. A control process
  // (plan audit / manager review) gets an explicitly selectable second
  // surface; it must never silently replace the agent terminal.
  const [surface, setSurface] = useState<"agent" | "control">("agent");
  useEffect(() => {
    if (!control && surface === "control") setSurface("agent");
  }, [control, surface]);
  // A project owns one Herdr space. A node gets a durable pane only when an
  // agent runs or a user explicitly opens its terminal; the inspector only
  // attaches to that pane.
  const [connection, setConnection] = useState<"connecting" | "connected" | "disconnected" | "transcript">("connecting");
  const showControl = Boolean(control) && surface === "control";
  const endpoint = showControl ? "terminal" : "shell";
  const streamNodeId = showControl ? control!.terminal_node_id : node.id;

  useEffect(() => {
    if (!host.current) return;
    const mountHost = host.current;
    // Keep xterm in the normal DOM. The PTY stream is already native and raw;
    // a regular host lets the browser paint the terminal canvas consistently
    // across embedded browser surfaces and makes its sizing observable.
    mountHost.replaceChildren();
    // xterm is intentionally mounted in the regular DOM. The old renderer
    // used a shadow root, so its `:host` rule no longer applies here. Set the
    // sizing contract on the actual host before opening xterm; otherwise
    // FitAddon can measure a zero/tiny box and the PTY is resized to a handful
    // of columns, which makes Herdr redraws appear scrambled.
    Object.assign(mountHost.style, {
      display: "block",
      width: "100%",
      height: "100%",
      minWidth: "0",
      minHeight: "0",
      overflow: "hidden",
    });
    const style = document.createElement("style");
    style.textContent = `${xtermCss}
.mount{display:block;width:100%;height:100%;min-width:0;min-height:0;padding:8px 12px 8px 8px;box-sizing:border-box;overflow:hidden;background:#090b0e}
.mount .xterm{display:block;width:100%;height:100%;min-width:0;min-height:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace!important;font-size:11px!important;line-height:1!important}
.mount .xterm-viewport{width:100%!important;height:100%!important;overflow:hidden!important}`;
    const mount = document.createElement("div");
    mount.className = "mount";
    mountHost.append(style, mount);
    const terminal = new Terminal({
      convertEol: true,
      // Keep xterm's input plumbing installed even while the panel is a
      // transcript. We gate writes in onData below; toggling disableStdin at
      // runtime is unreliable in embedded browsers and can leave the helper
      // textarea alive without a renderer/input connection.
      disableStdin: false,
      cursorBlink: false,
      fontFamily: "ui-monospace,SFMono-Regular,Menlo,monospace",
      fontSize: 11,
      lineHeight: 1,
      // Herdr owns the scrollback. xterm is only a renderer for the snapshot
      // Herdr returns, so it must not create a second browser-local history.
      scrollback: 0,
      theme: {
        background: "#090b0e",
        black: "#090b0e",
        foreground: "#c5ccd6",
        cursor: "#78a9ff",
        red: "#ef7373",
        green: "#65c98a",
        yellow: "#dfb15b",
        blue: "#78a9ff",
        magenta: "#b28cff",
        cyan: "#65c9c4",
        white: "#e6ebf2",
        brightBlack: "#5d6878",
        brightRed: "#ff8b8b",
        brightGreen: "#8ee6aa",
        brightYellow: "#f4d27d",
        brightBlue: "#9abaff",
        brightMagenta: "#c9adff",
        brightCyan: "#91e5df",
        brightWhite: "#ffffff",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(mount);
    try {
      fit.fit();
    } catch {
      /* hidden panel */
    }

    let disposed = false;
    let sessionActive = false;
    let replaying = false;
    let socket: WebSocket | null = null;
    let guarded = false;
    // A clean inactive status is a terminal lifecycle event, not a transport
    // failure. Keep the transcript rendered and suppress the reconnect path
    // when the backend has explicitly told us the session ended.
    let sessionEnded = false;
    let terminalWriteQueue = Promise.resolve();
    const queueTerminalWrite = (data: string) => {
      // Do not wait for xterm's optional completion callback here. A native
      // full-screen program can leave a parser sequence pending while it is
      // repainting; waiting on that callback would block every later chunk
      // and make a perfectly live PTY appear blank. xterm.write itself keeps
      // the bytes in order, so the queue only needs to serialize calls.
      const write = terminalWriteQueue.then(() => {
        try {
          terminal.write(data);
          terminal.refresh(0, Math.max(0, terminal.rows - 1));
        } catch {
          // The terminal may be disposed while a queued Herdr snapshot is
          // still resolving.
        }
      });
      terminalWriteQueue = write.catch(() => undefined);
      return write;
    };
    const fitTerminal = () => {
      try {
        fit.fit();
        // FitAddon measures xterm's fractional cell height, while the DOM
        // renderer rounds each row up to a whole pixel. That can leave the
        // final row extending below the viewport by a few pixels. Reconcile
        // the actual rendered row height with the mount's usable height so
        // the last line remains fully visible at every panel size.
        const firstRow = mount.querySelector<HTMLElement>('.xterm-rows > div');
        if (!firstRow) return;
        const mountStyle = window.getComputedStyle(mount);
        const verticalPadding =
          parseFloat(mountStyle.paddingTop) + parseFloat(mountStyle.paddingBottom);
        const rowHeight = firstRow.getBoundingClientRect().height;
        const usableHeight = mount.clientHeight - verticalPadding;
        if (rowHeight <= 0 || usableHeight <= 0) return;
        const maxRows = Math.max(8, Math.floor((usableHeight - 1) / rowHeight));
        if (maxRows < terminal.rows) {
          terminal.resize(terminal.cols, maxRows);
        }
      } catch {
        /* hidden panel */
      }
    };
    const syncSize = () => {
      const rect = mountHost.getBoundingClientRect();
      if (rect.width < 80 || rect.height < 80) return;
      fitTerminal();
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({
            type: "resize",
            cols: Math.max(40, terminal.cols),
            rows: Math.max(8, terminal.rows),
          }),
        );
      }
    };
    const refitAfterLayout = () => {
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          if (!disposed) syncSize();
        });
      });
    };
    void document.fonts?.ready.then(() => {
      if (!disposed) refitAfterLayout();
    });
    const activate = (value: boolean, idleValue = false) => {
      sessionActive = value;
      setActive(value);
      setIdle(value && idleValue);
      terminal.options.cursorBlink = value;
      const helper = mountHost.querySelector<HTMLTextAreaElement>(
        ".xterm-helper-textarea",
      );
      if (helper) {
        helper.disabled = !value;
        helper.readOnly = !value;
        helper.tabIndex = value ? 0 : -1;
        helper.setAttribute(
          "aria-label",
          value ? "Terminal input" : "Terminal transcript",
        );
      }
      if (value) terminal.focus();
    };

    setConnection("connecting");
    setRuntimeGuard(null);
    if (endpoint) {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(
        `${protocol}://${location.host}/api/nodes/${streamNodeId}/${endpoint}`,
      );
      socket.onopen = () => {
        sessionEnded = false;
        setConnection("connected");
        syncSize();
      };
      socket.onmessage = async (event) => {
        let message: Record<string, unknown>;
        try {
          message = JSON.parse(String(event.data)) as Record<string, unknown>;
        } catch {
          return;
        }
        if (message.type === "runtime_guard") {
          guarded = true;
          setRuntimeGuard(String(message.message ?? "Runtime guard is active."));
          activate(false);
          setConnection("disconnected");
        } else if (message.type === "snapshot") {
          const snapshotActive = Boolean(message.active);
          sessionEnded = !snapshotActive;
          setConnection(snapshotActive ? "connected" : "transcript");
          activate(snapshotActive, Boolean(message.idle));
          terminal.reset();
          if (message.output) {
            const fitted = { cols: terminal.cols, rows: terminal.rows };
            replaying = true;
            const snapshotCols = Number(message.cols) || fitted.cols;
            const snapshotRows = Number(message.rows) || fitted.rows;
            if (snapshotCols !== fitted.cols || snapshotRows !== fitted.rows) {
              terminal.resize(snapshotCols, snapshotRows);
            }
            await queueTerminalWrite(String(message.output));
            if (snapshotCols !== fitted.cols || snapshotRows !== fitted.rows) {
              terminal.resize(fitted.cols, fitted.rows);
            }
            replaying = false;
          }
        } else if (message.type === "output") {
          if (!sessionActive) activate(true);
          setIdle(false);
          await queueTerminalWrite(String(message.data ?? ""));
        } else if (message.type === "status") {
          const statusActive = Boolean(message.active);
          if (!statusActive) {
            sessionEnded = true;
            setConnection("transcript");
          }
          activate(statusActive, Boolean(message.idle));
          if (!statusActive && socket?.readyState === WebSocket.OPEN) {
            // The outer attach client ended (for example after a cancelled
            // harness). Close this subscription so the durable Herdr pane is
            // reattached by the normal retry path instead of leaving xterm
            // displaying the old client's "[terminated]" final frame.
            socket.close();
          }
        }
      };
      socket.onclose = () => {
        activate(false);
        if (!disposed && sessionEnded) {
          setConnection("transcript");
        } else if (!disposed) {
          setConnection("disconnected");
          if (!guarded && !node.runtime_guard) {
            // The Herdr pane is durable. A dropped websocket should be visibly
            // retried, not briefly presented as a manual reconnect failure.
            window.setTimeout(() => {
              if (!disposed) setReconnectKey((value) => value + 1);
            }, 500);
          }
        }
      };
    } else {
      activate(false);
    }

    const sendTerminalInput = (value: string, binary = false) => {
      if (sessionActive && socket?.readyState === WebSocket.OPEN) {
        // xterm emits terminal-identification replies through onData too.
        // They are protocol responses, not user input; forwarding them makes
        // a harness echo fragments such as `0;276;0c` into its own screen.
        if (!binary && /^\x1b\[[?>]?[0-9;]*c$/.test(value)) return;
        if (binary) {
          // xterm's binary event is a byte string (used for non-UTF-8 mouse
          // reports). Preserve those bytes across JSON instead of allowing
          // the browser's UTF-8 encoder to turn them into visible garbage.
          let encoded = "";
          for (let index = 0; index < value.length; index += 1) {
            encoded += String.fromCharCode(value.charCodeAt(index) & 0xff);
          }
          socket.send(
            JSON.stringify({
              type: "input",
              encoding: "base64",
              data: btoa(encoded),
            }),
          );
          return;
        }
        socket.send(JSON.stringify({ type: "input", data: value }));
      }
    };
    terminal.onData((value) => sendTerminalInput(value));
    terminal.onBinary((value) => sendTerminalInput(value, true));
    terminal.onResize(({ cols, rows }) => {
      if (!replaying && socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({
          type: "resize",
          cols: Math.max(40, cols),
          rows: Math.max(8, rows),
        }));
      }
    });
    // Herdr owns terminal history and viewport position. Do not let xterm's
    // local scrollback become a second history: forward the wheel gesture to
    // the persistent Herdr pane, then redraw the returned snapshot.
    const wheelTarget = mount;
    const forwardWheel = (event: WheelEvent) => {
      if (!sessionActive || socket?.readyState !== WebSocket.OPEN) return;
      const delta = event.deltaY || event.deltaX;
      if (!delta) return;
      event.preventDefault();
      event.stopPropagation();
      socket.send(JSON.stringify({
        type: "scroll",
        direction: delta < 0 ? "up" : "down",
        amount: Math.min(10, Math.max(1, Math.round(Math.abs(delta) / 24))),
      }));
    };
    // Capture before xterm's own viewport handler. Herdr owns scrollback, so
    // xterm must not first move an independent local viewport and then pass
    // the same wheel event through as a second scroll operation.
    wheelTarget.addEventListener("wheel", forwardWheel, { passive: false, capture: true });
    const observer = new ResizeObserver(() => refitAfterLayout());
    observer.observe(mountHost);
    refitAfterLayout();
    // Inspector and graph panes can finish their CSS grid transition after
    // xterm has opened. FitAddon only measures the current box, so retry a
    // few times while the layout settles; otherwise a terminal can remain a
    // tiny 13-column viewport inside a correctly sized panel.
    const refitTimers = [50, 200, 500, 1000].map((delay) =>
      window.setTimeout(refitAfterLayout, delay),
    );
    return () => {
      disposed = true;
      refitTimers.forEach((timer) => window.clearTimeout(timer));
      observer.disconnect();
      wheelTarget.removeEventListener("wheel", forwardWheel, true);
      socket?.close();
      terminal.dispose();
    };
  }, [endpoint, node.id, reconnectKey, streamNodeId]);

  return (
    <div
      className={`terminal-shell ${!active ? "terminal-shell-collapsed" : ""}`}
    >
      <div className="terminal-head">
        <Icon name="terminal" />
        <span className="terminal-title">
          {showControl
            ? node.control_activity?.kind === "manager_review" ? "control · manager" : "control · plan audit"
            : "shell"} · {streamNodeId.slice(0, 8)}
        </span>
        {control && (
          <span className="terminal-view-switch" role="tablist" aria-label="Terminal surface">
            <button role="tab" aria-selected={!showControl} className={!showControl ? "active" : ""} onClick={() => setSurface("agent")}>Agent</button>
            <button role="tab" aria-selected={showControl} className={showControl ? "active" : ""} onClick={() => setSurface("control")}>
              {node.control_activity?.kind === "manager_review" ? "Manager review" : "Plan audit"}
            </button>
          </span>
        )}
        <span className="terminal-view-switch" role="tablist" aria-label="Node output views">
          <button role="tab" aria-selected={view === "terminal"} className={view === "terminal" ? "active" : ""} onClick={() => setView("terminal")}>Terminal</button>
          <button role="tab" aria-selected={view === "activity"} className={view === "activity" ? "active" : ""} onClick={() => setView("activity")}>Activity</button>
        </span>
        <span className="terminal-mode">
          {active
            ? (idle ? "LIVE · waiting" : "LIVE")
            : connection === "connecting"
            ? "CONNECTING"
            : connection === "transcript"
            ? "TRANSCRIPT"
            : "RECONNECTING"}
        </span>
      </div>
      {!active && (
        <div className="terminal-disconnected">
          <span>
            {runtimeGuard
              ? `${runtimeGuard} ${HERDR_OPERATOR_WARNING}`
            : connection === "connecting"
              ? "Connecting to this node’s persistent Herdr shell…"
              : connection === "transcript"
              ? "Session finished; transcript retained."
              : "Connection interrupted; reconnecting to the persistent Herdr shell…"}
          </span>
        </div>
      )}
      <div
        ref={host}
        className={`terminal-shadow-host ${(!active && connection !== "transcript") || view !== "terminal" ? "is-collapsed" : ""}`}
        aria-label="Agent terminal"
      />
      {view === "activity" && <HarnessActivity node={node} />}
    </div>
  );
}

function HarnessActivity({ node }: { node: GraphNode }) {
  const [records, setRecords] = useState<LogRecord[]>([]);

  useEffect(() => {
    let alive = true;
    const include = (record: LogRecord) => isNodeActivity(record, node.id);
    const append = (record: LogRecord) => {
      if (!include(record)) return;
      setRecords((current) => current.some((item) => item.event_id === record.event_id)
        ? current
        : [...current.slice(-79), record]);
    };
    void getProjectLogs(node.project_id, node.id)
      .then(({ records: existing }) => { if (alive) existing.forEach(append); })
      .catch(() => { /* Existing terminal remains usable if evidence is unavailable. */ });
    const stream = new EventSource(
      `/api/projects/${encodeURIComponent(node.project_id)}/logs/stream?search=${encodeURIComponent(node.id)}`,
    );
    stream.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as { type?: string; record?: LogRecord };
        if (alive && message.type === "log" && message.record) append(message.record);
      } catch {
        // A future malformed log must not affect the terminal or project run.
      }
    };
    return () => { alive = false; stream.close(); };
  }, [node.id, node.project_id]);

  const telemetry = [...records].reverse().find(isTelemetryStatus);
  return <div className="harness-activity" aria-live="polite">
    {telemetry && <div className={`harness-telemetry telemetry-${telemetryStatus(telemetry)}`}>
      <strong>Telemetry {telemetryStatus(telemetry)}</strong>
      <span>{activityDetail(telemetry)}</span>
    </div>}
    {!records.length && <p>Waiting for telemetry status. The raw terminal remains available and interactive.</p>}
    {records.map((record) => <div className="harness-activity-line" key={record.event_id}>
      <time dateTime={record.timestamp}>{activityTime(record.timestamp)}</time>
      <span>{activityLabel(record)}</span>
      <small>{activityDetail(record)}</small>
    </div>)}
  </div>;
}

function isTelemetryStatus(record: LogRecord) {
  if (record.kind !== "harness.event") return false;
  const data = record.data && typeof record.data === "object" ? record.data as Record<string, unknown> : {};
  return data.kind === "status" && data.name === "telemetry";
}

function telemetryStatus(record: LogRecord) {
  const data = record.data && typeof record.data === "object" ? record.data as Record<string, unknown> : {};
  return String(data.status ?? "unknown");
}

function isNodeActivity(record: LogRecord, nodeId: string) {
  if (![
    "harness.launch", "harness.return", "harness.event", "application.error",
  ].includes(record.kind)) return false;
  const data = record.data;
  return Boolean(data && typeof data === "object" && (data as Record<string, unknown>).node_id === nodeId);
}

function activityLabel(record: LogRecord) {
  const data = record.data && typeof record.data === "object" ? record.data as Record<string, unknown> : {};
  if (isTelemetryStatus(record)) return `telemetry ${String(data.status ?? "status")}`;
  if (record.kind === "harness.event") return String(data.kind ?? "harness event").replaceAll("_", " ");
  return record.kind.replaceAll(".", " ");
}

function activityDetail(record: LogRecord) {
  const data = record.data && typeof record.data === "object" ? record.data as Record<string, unknown> : {};
  const nested = data.data && typeof data.data === "object" ? data.data as Record<string, unknown> : {};
  if (isTelemetryStatus(record)) {
    return [nested.source, nested.detail].filter((value) => typeof value === "string" && value).join(" · ") || "telemetry status";
  }
  const name = data.name ?? nested.command ?? nested.path ?? data.outcome ?? data.error ?? record.message;
  const status = data.status ?? record.status;
  return [name ? String(name) : "", status ? String(status) : ""].filter(Boolean).join(" · ") || "observed";
}

function activityTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString();
}
