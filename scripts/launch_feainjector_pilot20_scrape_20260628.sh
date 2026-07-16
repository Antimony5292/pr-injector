#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/experiments/feainjector_pilot_20260628/scrape_pilot20"
LOG_DIR="$ROOT/experiments/feainjector_pilot_20260628/logs"
LOG="$LOG_DIR/scrape_pilot20.log"

mkdir -p "$OUT" "$LOG_DIR"

exec > >(tee -a "$LOG") 2>&1

echo "[feainjector] start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
cd "$ROOT"

"$ROOT/.venvs/feainjector/bin/python" "$ROOT/scripts/run_feainjector_pilot_scrape.py" \
  --pilot-ids "$ROOT/experiments/feainjector_pilot_20260628/instances_pilot50.json" \
  --output-dir "$OUT" \
  --limit 20

"$ROOT/.venvs/feainjector/bin/python" "$ROOT/scripts/profile_feainjector_candidates.py" \
  --input-jsonl "$OUT/FEA-INJECTOR-pilot-medium.jsonl" \
  --output-dir "$OUT/profiles"

echo "[feainjector] done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
