#!/usr/bin/env bash
# Starts the whole stack inside a Codespace. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")/.."
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
echo "▶ Building and starting FeatureLedger (first time: 3-6 minutes)…"
docker compose up --build -d
echo "▶ Waiting for the dashboard…"
for i in $(seq 1 120); do
  if curl -fs http://localhost:3000 >/dev/null 2>&1; then
    echo "✅ FeatureLedger is up: open the PORTS tab → port 3000 (dashboard), 8000/docs (API)"
    exit 0
  fi
  sleep 5
done
echo "⚠️  Not up yet. Check: docker compose ps && docker compose logs api web"
