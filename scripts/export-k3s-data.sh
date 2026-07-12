#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT/data}"
OUT="${1:-$ROOT/lipetsk-gas-data-$(date +%Y%m%d-%H%M%S).tar.gz}"
WITH_SNAPSHOTS="${WITH_SNAPSHOTS:-0}"

DB="$DATA_DIR/history.db"
if [[ ! -f "$DB" ]]; then
  echo "ERROR: $DB not found" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/data"
cp "$DB" "$TMP/data/history.db"
if [[ -f "$DATA_DIR/history.db-wal" ]]; then
  cp "$DATA_DIR/history.db-wal" "$TMP/data/"
fi
if [[ -f "$DATA_DIR/history.db-shm" ]]; then
  cp "$DATA_DIR/history.db-shm" "$TMP/data/"
fi

python3 - <<'PY' "$DB" > "$TMP/MANIFEST.json"
import json, sqlite3, sys
db = sys.argv[1]
conn = sqlite3.connect(db)
snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
observations = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
stations = conn.execute("SELECT COUNT(DISTINCT station_id) FROM observations").fetchone()[0]
min_at, max_at = conn.execute(
    "SELECT MIN(collected_at), MAX(collected_at) FROM observations"
).fetchone()
print(json.dumps({
    "snapshots": snapshots,
    "observations": observations,
    "stations": stations,
    "from": min_at,
    "to": max_at,
}, ensure_ascii=False, indent=2))
PY

if [[ "$WITH_SNAPSHOTS" == "1" ]]; then
  if [[ -d "$DATA_DIR/snapshots" ]]; then
    cp -a "$DATA_DIR/snapshots" "$TMP/data/"
  fi
fi

tar -C "$TMP" -czf "$OUT" data MANIFEST.json
echo "Created $OUT"
tar -tzf "$OUT" | head
python3 -c "import json; print(json.load(open('$TMP/MANIFEST.json')))"
