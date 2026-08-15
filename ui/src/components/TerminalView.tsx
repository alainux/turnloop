import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import xtermCss from "@xterm/xterm/css/xterm.css?inline";
import type { GraphNode, Run } from "../domain";
import { Icon } from "./Icon";

interface Props {
  node: GraphNode;
  runs: Run[];
}

export function TerminalView({ node }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);
  const [idle, setIdle] = useState(false);
  const [reconnectKey, setReconnectKey] = useState(0);
  // A project owns one Herdr space. A node gets a durable pane only when an
  // agent runs or a user explicitly opens its terminal; the inspector only
  // attaches to that pane.
  const [connection, setConnection] = useState<"connecting" | "connected" | "disconnected">("connecting");
  const endpoint = "shell";

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
.mount .xterm-viewport{width:100%!important;height:100%!important;overflow-y:auto!important}`;
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
      // Herdr is the canonical terminal history. Keep a bounded local render
      // buffer for smooth browser scrolling, but refresh it from Herdr on
      // every attach instead of accumulating divergent redraws.
      scrollback: 5000,
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
    let terminalWriteQueue = Promise.resolve();
    const queueTerminalWrite = (data: string) => {
      // Do not wait for xterm's optional completion callback here. A native
      // full-screen program can leave a parser sequence pending while it is
      // repainting; waiting on that callback would block every later chunk
      // and make a perfectly live PTY appear blank. xterm.write itself keeps
      // the bytes in order, so the queue only needs to serialize calls.
      const write = terminalWriteQueue.then(() => {
        terminal.write(data);
        terminal.refresh(0, Math.max(0, terminal.rows - 1));
      });
      terminalWriteQueue = write.catch(() => undefined);
      return write;
    };
    const syncSize = () => {
      const rect = mountHost.getBoundingClientRect();
      if (rect.width < 80 || rect.height < 80) return;
      try {
        fit.fit();
      } catch {
        /* hidden panel */
      }
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(
          JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }),
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
    if (endpoint) {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(
        `${protocol}://${location.host}/api/nodes/${node.id}/${endpoint}`,
      );
      socket.onopen = () => {
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
        if (message.type === "snapshot") {
          activate(Boolean(message.active), Boolean(message.idle));
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
            terminal.scrollToBottom();
          }
        } else if (message.type === "output") {
          if (!sessionActive) activate(true);
          setIdle(false);
          await queueTerminalWrite(String(message.data ?? ""));
        } else if (message.type === "status") {
          const statusActive = Boolean(message.active);
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
        if (!disposed) {
          setConnection("disconnected");
          // The Herdr pane is durable. A dropped websocket should be visibly
          // retried, not briefly presented as a manual reconnect failure.
          window.setTimeout(() => {
            if (!disposed) setReconnectKey((value) => value + 1);
          }, 500);
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
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
      }
    });
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
      socket?.close();
      terminal.dispose();
    };
  }, [endpoint, node.id, reconnectKey]);

  return (
    <div
      className={`terminal-shell ${!active ? "terminal-shell-collapsed" : ""}`}
    >
      <div className="terminal-head">
        <Icon name="terminal" />
        <span className="terminal-title">
          {endpoint === "shell" ? "shell" : node.agent?.harness ?? node.executor} · {node.id.slice(0, 8)}
        </span>
        <span className="terminal-mode">
          {active ? (idle ? "LIVE · waiting" : "LIVE") : connection === "connecting" ? "CONNECTING" : "RECONNECTING"}
        </span>
      </div>
      {!active && (
        <div className="terminal-disconnected">
          <span>
            {connection === "connecting"
              ? "Connecting to this node’s persistent Herdr shell…"
              : "Connection interrupted; reconnecting to the persistent Herdr shell…"}
          </span>
        </div>
      )}
      <div
        ref={host}
        className={`terminal-shadow-host ${!active ? "is-collapsed" : ""}`}
        aria-label="Agent terminal"
      />
    </div>
  );
}
