#!/usr/bin/env bash
set -u

ROOT="/Users/harmin/Desktop/pr-injector-main"
LOG_DIR="$ROOT/experiments/rq2_500/logs"
LOG_FILE="$LOG_DIR/prinjector_v2_all_observation_monitor_20260626.log"

RUN_DIRS=(
  "$ROOT/experiments/rq2_500/v2_construction_20260625"
  "$ROOT/experiments/rq2_500/v2_network_recovery_20260626"
  "$ROOT/experiments/rq2_500/v2_l3_recovery_20260626"
  "$ROOT/experiments/rq2_500/v2_supplemental_relaxed_20260626"
)

SCREENS=(
  "pri_v2_b500_construct_20260625"
  "pri_v2_network_recovery_20260626"
  "pri_v2_l3_recovery_20260626"
  "pri_v2_supplemental_relaxed_20260626"
)

mkdir -p "$LOG_DIR"
cd "$ROOT" || exit 1

any_screen_alive() {
  local listing
  listing="$(screen -ls 2>/dev/null || true)"
  local name
  for name in "${SCREENS[@]}"; do
    if grep -q "$name" <<<"$listing"; then
      return 0
    fi
  done
  return 1
}

while true; do
  {
    date -u +"[%Y-%m-%dT%H:%M:%SZ] collecting all v2 construction observations"
    for run_dir in "${RUN_DIRS[@]}"; do
      if [[ -d "$run_dir" ]]; then
        .venv/bin/python scripts/collect_prinjector_v2_observations.py \
          --run-dir "$run_dir" \
          --output-dir "$run_dir/observations"
      fi
    done
  } >> "$LOG_FILE" 2>&1

  if ! any_screen_alive; then
    date -u +"[%Y-%m-%dT%H:%M:%SZ] all tracked screens ended; monitor exiting" >> "$LOG_FILE" 2>&1
    break
  fi

  sleep 300
done
