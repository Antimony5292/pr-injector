#!/usr/bin/env bash
set -u

ROOT="/Users/harmin/Desktop/pr-injector-main"
RUN_DIR="$ROOT/experiments/rq2_500/v2_network_recovery_20260626"
OUT_DIR="$RUN_DIR/observations"
LOG_DIR="$ROOT/experiments/rq2_500/logs"
LOG_FILE="$LOG_DIR/prinjector_v2_recovery_observation_monitor_20260626.log"
RECOVERY_SCREEN="pri_v2_network_recovery_20260626"

mkdir -p "$OUT_DIR" "$LOG_DIR"
cd "$ROOT" || exit 1

while true; do
  {
    date -u +"[%Y-%m-%dT%H:%M:%SZ] collecting recovery observations"
    .venv/bin/python scripts/collect_prinjector_v2_observations.py \
      --run-dir "$RUN_DIR" \
      --output-dir "$OUT_DIR"
  } >> "$LOG_FILE" 2>&1

  if ! screen -ls | grep -q "$RECOVERY_SCREEN"; then
    date -u +"[%Y-%m-%dT%H:%M:%SZ] recovery screen ended; monitor exiting" >> "$LOG_FILE" 2>&1
    break
  fi

  sleep 300
done
