import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import xtermCss from "@xterm/xterm/css/xterm.css?inline";
import type { Artifact, GraphNode, Run } from "../domain";
import { Icon } from "./Icon";

interface Props {
  node: GraphNode;
  artifacts: Artifact[];
  runs: Run[];
}
function history(artifacts: Artifact[], runs: Run[]): string {
  const transcript = [...artifacts]
    .reverse()
    .find((item) => item.name === "transcript");
  if (typeof transcript?.content === "string") return transcript.content;
  const run = runs.at(-1);
  return run?.logs || run?.summary || "No terminal output yet.";
}
export function TerminalView({ node, artifacts, runs }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const [active, setActive] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    const shadow =
      host.current.shadowRoot ?? host.current.attachShadow({ mode: "open" });
    shadow.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `${xtermCss}\n:host{display:block;height:100%;background:#090b0e}.mount{height:100%;padding:8px;box-sizing:border-box}.xterm{height:100%}`;
    const mount = document.createElement("div");
    mount.className = "mount";
    shadow.append(style, mount);
    const terminal = new Terminal({
      convertEol: true,
      disableStdin: true,
      cursorBlink: false,
      fontFamily: "ui-monospace,SFMono-Regular,Menlo,monospace",
      fontSize: 11,
      lineHeight: 1.35,
      scrollback: 5000,
      theme: {
        background: "#090b0e",
        foreground: "#c5ccd6",
        cursor: "#78a9ff",
        red: "#ef7373",
        green: "#65c98a",
        yellow: "#dfb15b",
        blue: "#78a9ff",
        magenta: "#b28cff",
        cyan: "#65c9c4",
      },
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(mount);
    const initial = history(artifacts, runs);
    if (initial) terminal.write(initial.replaceAll("\n", "\r\n"));
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(
      `${protocol}://${location.host}/api/nodes/${node.id}/terminal`,
    );
    let sessionActive = false;
    const activate = (value: boolean) => {
      sessionActive = value;
      setActive(value);
      terminal.options.disableStdin = !value;
      terminal.options.cursorBlink = value;
      const helper = shadow.querySelector<HTMLTextAreaElement>(
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
    };
    socket.onmessage = (event) => {
      let message: Record<string, unknown>;
      try {
        message = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      if (message.type === "snapshot") {
        activate(Boolean(message.active));
        if (message.output) {
          terminal.reset();
          terminal.write(String(message.output));
        }
      } else if (message.type === "output")
        terminal.write(String(message.data ?? ""));
      else if (message.type === "status") activate(Boolean(message.active));
    };
    socket.onclose = () => activate(false);
    terminal.onData((value) => {
      if (sessionActive && socket.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: "input", data: value }));
    });
    terminal.onResize(({ cols, rows }) => {
      if (socket.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: "resize", cols, rows }));
    });
    const observer = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* hidden panel */
      }
    });
    observer.observe(host.current);
    requestAnimationFrame(() => fit.fit());
    return () => {
      observer.disconnect();
      socket.close();
      terminal.dispose();
    };
  }, [node.id, artifacts, runs]);
  return (
    <div className="terminal-shell">
      <div className="terminal-head">
        <Icon name="terminal" />
        <span className="terminal-title">
          {node.agent?.harness ?? node.executor} · {node.id.slice(0, 8)}
        </span>
        <span className="terminal-mode">{active ? "LIVE" : "TRANSCRIPT"}</span>
      </div>
      <div
        ref={host}
        className="terminal-shadow-host"
        aria-label="Agent terminal"
      />
    </div>
  );
}
