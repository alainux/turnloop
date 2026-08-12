#!/usr/bin/env bash
# Start a local Postgres (Postgres.app on macOS, or system/docker elsewhere),
# create a `turn` role + database, and print the DATABASE_URL to use.
set -euo pipefail

PG_DIR="./.pgdata"
PORT="${PGPORT:-5432}"
ROLE="${PGROLE:-turn}"
PASS="${PGPASS:-turn}"
DB="${PGDB:-turn}"

# Prefer Postgres.app binaries if present.
if [ -d "/Applications/Postgres.app/Contents/Versions/latest/bin" ]; then
  export PATH="/Applications/Postgres.app/Contents/Versions/latest/bin:$PATH"
fi

if ! command -v pg_ctl >/dev/null 2>&1; then
  echo "pg_ctl not found. Install Postgres.app, or run 'docker run -e POSTGRES_PASSWORD=$PASS -p $PORT:5432 postgres' and skip this script."
  exit 1
fi

if [ ! -d "$PG_DIR" ]; then
  echo "Initializing Postgres data dir at $PG_DIR ..."
  initdb -D "$PG_DIR" -U postgres --auth=trust >/dev/null
fi

if ! pg_ctl -D "$PG_DIR" status >/dev/null 2>&1; then
  echo "Starting Postgres on port $PORT ..."
  pg_ctl -D "$PG_DIR" -o "-p $PORT" -l "$PG_DIR/postgres.log" start
  sleep 3
fi

echo "Creating role '$ROLE' and database '$DB' ..."
psql -h localhost -p "$PORT" -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname='$ROLE'" | grep -q 1 \
  || psql -h localhost -p "$PORT" -U postgres -c "CREATE ROLE $ROLE LOGIN PASSWORD '$PASS' SUPERUSER;"
psql -h localhost -p "$PORT" -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='$DB'" | grep -q 1 \
  || psql -h localhost -p "$PORT" -U postgres -c "CREATE DATABASE $DB OWNER $ROLE;"

URL="postgresql+asyncpg://$ROLE:$PASS@localhost:$PORT/$DB"
echo
echo "Postgres is ready. Use:"
echo "    export TURN_DATABASE_URL=\"$URL\""
