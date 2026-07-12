#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <archive.tar.gz> <namespace>" >&2
  echo "Example: $0 lipetsk-gas-data-20260712.tar.gz lipetsk-gas-monitor" >&2
  exit 1
fi

ARCHIVE="$1"
NAMESPACE="$2"
DEPLOYMENT="${DEPLOYMENT:-lipetsk-gas-monitor}"

if [[ ! -f "$ARCHIVE" ]]; then
  echo "ERROR: archive not found: $ARCHIVE" >&2
  exit 1
fi

POD="$(kubectl -n "$NAMESPACE" get pods -l app=lipetsk-gas-monitor -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "$POD" ]]; then
  echo "ERROR: pod not found in namespace $NAMESPACE" >&2
  exit 1
fi

echo "Scaling down collector side effects: importing into pod $POD"
kubectl -n "$NAMESPACE" cp "$ARCHIVE" "$POD:/tmp/import.tar.gz" -c web
kubectl -n "$NAMESPACE" exec "$POD" -c web -- sh -c '
  set -e
  cd /tmp
  rm -rf import-stage
  mkdir import-stage
  tar -xzf import.tar.gz -C import-stage
  test -f import-stage/data/history.db
  mkdir -p /app/data
  cp import-stage/data/history.db /app/data/history.db
  rm -f /app/data/history.db-wal /app/data/history.db-shm
  if [ -d import-stage/data/snapshots ]; then
    rm -rf /app/data/snapshots
    cp -a import-stage/data/snapshots /app/data/
  fi
  cat import-stage/MANIFEST.json
'

echo "Restarting deployment to reopen SQLite cleanly..."
kubectl -n "$NAMESPACE" rollout restart "deployment/$DEPLOYMENT"
kubectl -n "$NAMESPACE" rollout status "deployment/$DEPLOYMENT" --timeout=120s
echo "Done. Check https://gas.qantrix.ru/api/meta"
