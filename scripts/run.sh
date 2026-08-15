#!/usr/bin/env bash
# Start the Turn UI server.
#
# The served app uses real harnesses only. Deterministic test modes are loaded
# by test registries, never by this process.
set -euo pipefail
cd "$(dirname "$0")/.."

export TURN_DATA_DIR="${TURN_DATA_DIR:-$(pwd)/turn}"
export TURN_PROJECTS_DIR="${TURN_PROJECTS_DIR:-$(pwd)/projects}"
export TURN_PLANNER="${TURN_PLANNER:-codex}"
export TURN_DEFAULT_EXECUTOR="${TURN_DEFAULT_EXECUTOR:-codex}"
export TURN_EXECUTION_BACKEND="${TURN_EXECUTION_BACKEND:-direct}"
export PYTHONPATH="${PYTHONPATH:-.}:$(pwd)"

PORT="${TURN_PORT:-8000}"
echo "Turn → http://127.0.0.1:${PORT}"
echo "  store:   ${TURN_DATA_DIR}"
echo "  projects:${TURN_PROJECTS_DIR}"
echo "  planner: ${TURN_PLANNER}"
echo "  executor:${TURN_DEFAULT_EXECUTOR}"
echo "  backend: ${TURN_EXECUTION_BACKEND}"

exec turn server --host 127.0.0.1 --port "$PORT"
