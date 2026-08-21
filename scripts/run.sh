#!/usr/bin/env bash
# CAUTION FOR AI OPERATORS: Herdr is an existing daemon. It cannot be launched
# inside subprocesses or from Herdr itself. Do not launch Herdr here; this
# script launches Turn only and requests the already-running Herdr client.
# Do not try to launch Herdr here.
# Start the Turn UI server.
#
# The served app uses real harnesses only. Deterministic test modes are loaded
# by test registries, never by this process.
set -euo pipefail
cd "$(dirname "$0")/.."

export TURN_DATA_DIR="${TURN_DATA_DIR:-$(pwd)/.turn}"
export TURN_PROJECTS_DIR="${TURN_PROJECTS_DIR:-$(pwd)/projects}"
export TURN_PLANNER="${TURN_PLANNER:-codex}"
export TURN_DEFAULT_EXECUTOR="${TURN_DEFAULT_EXECUTOR:-codex}"
export TURN_EXECUTION_BACKEND="${TURN_EXECUTION_BACKEND:-direct}"
# Resolve the checkout first so the daemon always serves the source tree the
# user launched it from, even when an older installed Turn package is present
# earlier on the process environment's import path.
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"

PORT="${TURN_PORT:-8000}"
echo "Turn → http://127.0.0.1:${PORT}"
echo "  store:   ${TURN_DATA_DIR}"
echo "  projects:${TURN_PROJECTS_DIR}"
echo "  planner: ${TURN_PLANNER}"
echo "  executor:${TURN_DEFAULT_EXECUTOR}"
echo "  backend: ${TURN_EXECUTION_BACKEND}"

exec turn server --host 127.0.0.1 --port "$PORT"
