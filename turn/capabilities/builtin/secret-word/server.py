#!/usr/bin/env python3
"""Tiny stdio MCP server used by Turn's capability verification fixture."""
from __future__ import annotations

import json
import sys


def send(message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"))
    sys.stdout.write(payload + "\n")
    sys.stdout.flush()


def receive() -> dict | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


while True:
    request = receive()
    if request is None:
        break
    request_id = request.get("id")
    method = request.get("method")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "secret-word", "version": "1.0.0"},
            },
        })
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"tools": [{
                "name": "echo_secret_word",
                "description": "Return the MCP-side verification word.",
                "inputSchema": {"type": "object", "properties": {}},
            }]},
        })
    elif method == "tools/call":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": "amber"}]},
        })
    elif request_id is not None:
        send({"jsonrpc": "2.0", "id": request_id, "result": {}})
