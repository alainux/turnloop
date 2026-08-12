"""Entry point: `python -m turn` / `turn` starts the UI server."""
from __future__ import annotations

import argparse

import uvicorn

from turn.server.app import app


def main() -> None:
    parser = argparse.ArgumentParser(prog="turn", description="Turn workgraph server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
