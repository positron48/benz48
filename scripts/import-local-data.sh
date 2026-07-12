#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="${1:-}"

if [[ -z "$SOURCE" ]]; then
  echo "Usage: $0 <history.db|history.db.gz>" >&2
  exit 1
fi

if [[ ! -f "$SOURCE" ]]; then
  echo "ERROR: file not found: $SOURCE" >&2
  exit 1
fi

cd "$ROOT"
echo "Stopping containers to avoid SQLite corruption..."
docker compose stop collector web

mkdir -p data
TMP="$(mktemp "${TMPDIR:-/tmp}/history.XXXXXX.db")"
cleanup() { rm -f "$TMP"; }
trap cleanup EXIT

if [[ "$SOURCE" == *.gz ]]; then
  gunzip -c "$SOURCE" > "$TMP"
else
  cp "$SOURCE" "$TMP"
fi

python3 - <<PY
import sqlite3, sys
conn = sqlite3.connect("$TMP")
integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
conn.close()
if integrity != "ok":
    raise SystemExit(f"integrity_check failed: {integrity}")
print(f"OK: {count} snapshots")
PY

rm -f data/history.db data/history.db-wal data/history.db-shm
cp "$TMP" data/history.db

echo "Starting containers..."
docker compose up -d web collector
docker compose restart web collector
echo "Done. Open http://localhost:${PORT:-18743}/"
