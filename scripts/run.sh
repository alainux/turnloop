#!/usr/bin/env bash
# Start the Turn UI server.
#
# Defaults run fully offline: SQLite store, heuristic planner, echo leaves.
# Override with env vars (see .env.example). Examples:
#   TURN_PLANNER=codex ./scripts/run.sh        # Codex-backed planning + workers
#   TURN_DATABASE_URL=postgresql+asyncpg://... ./scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export TURN_DATABASE_URL="${TURN_DATABASE_URL:-sqlite+aiosqlite:///./turnloop.db}"
export TURN_PLANNER="${TURN_PLANNER:-heuristic}"
export TURN_DEFAULT_EXECUTOR="${TURN_DEFAULT_EXECUTOR:-echo}"
export TURN_EXECUTION_BACKEND="${TURN_EXECUTION_BACKEND:-direct}"
export PYTHONPATH="${PYTHONPATH:-.}:$(pwd)"

PORT="${TURN_PORT:-8000}"
echo "Turn → http://127.0.0.1:${PORT}"
echo "  store:   ${TURN_DATABASE_URL}"
echo "  planner: ${TURN_PLANNER}"
echo "  executor:${TURN_DEFAULT_EXECUTOR}"
echo "  backend: ${TURN_EXECUTION_BACKEND}"

exec uvicorn turn.server.app:app --host 127.0.0.1 --port "$PORT" --reload
