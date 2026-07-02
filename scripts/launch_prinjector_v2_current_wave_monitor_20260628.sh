#!/usr/bin/env bash
set -u

ROOT="/Users/harmin/Desktop/pr-injector-main"
LOG_DIR="$ROOT/experiments/rq2_500/logs"
LOG_FILE="$LOG_DIR/prinjector_v2_current_wave_monitor_20260628.log"

RUN_DIRS=(
  "$ROOT/experiments/rq2_500/v2_gap267_fresh_20260628"
  "$ROOT/experiments/rq2_500/v2_second_chance_pro_verified_20260628"
  "$ROOT/experiments/rq2_500/v2_multivariant_pilot_runs_20260628/l1_clean_revert"
  "$ROOT/experiments/rq2_500/v2_multivariant_pilot_runs_20260628/l2_ast_surgery"
  "$ROOT/experiments/rq2_500/v2_multivariant_pilot_runs_20260628/l3_semantic"
  "$ROOT/experiments/rq2_500/v2_multivariant_pilot_runs_20260628/l3_expanded_p2p"
)

SCREENS=(
  "pri_v2_gap267_fresh_20260628"
  "pri_v2_second_chance_pro_verified_20260628"
  "pri_v2_multivariant_pilot_20260628"
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
    date -u +"[%Y-%m-%dT%H:%M:%SZ] collecting current wave observations"
    for run_dir in "${RUN_DIRS[@]}"; do
      if [[ -d "$run_dir" ]]; then
        .venv/bin/python scripts/collect_prinjector_v2_observations.py \
          --run-dir "$run_dir" \
          --output-dir "$run_dir/observations"
      fi
    done
  } >> "$LOG_FILE" 2>&1

  if ! any_screen_alive; then
    date -u +"[%Y-%m-%dT%H:%M:%SZ] all current wave screens ended; monitor exiting" >> "$LOG_FILE" 2>&1
    break
  fi

  sleep 300
done
