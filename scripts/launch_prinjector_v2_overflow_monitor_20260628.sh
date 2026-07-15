#!/usr/bin/env bash
set -u

ROOT="/Users/harmin/Desktop/pr-injector-main"
LOG_DIR="$ROOT/experiments/rq2_500/logs"
LOG_FILE="$LOG_DIR/prinjector_v2_overflow_monitor_20260628.log"

RUN_DIRS=(
  "$ROOT/experiments/rq2_500/v2_overflow_verified_strict_20260628"
  "$ROOT/experiments/rq2_500/v2_overflow_pro_relaxed_20260628"
)

SCREENS=(
  "pri_v2_overflow_verified_strict_20260628"
  "pri_v2_overflow_pro_relaxed_20260628"
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
    date -u +"[%Y-%m-%dT%H:%M:%SZ] collecting overflow observations"
    for run_dir in "${RUN_DIRS[@]}"; do
      if [[ -d "$run_dir" ]]; then
        .venv/bin/python scripts/collect_prinjector_v2_observations.py \
          --run-dir "$run_dir" \
          --output-dir "$run_dir/observations"
      fi
    done
  } >> "$LOG_FILE" 2>&1

  if ! any_screen_alive; then
    date -u +"[%Y-%m-%dT%H:%M:%SZ] overflow screens ended; monitor exiting" >> "$LOG_FILE" 2>&1
    break
  fi

  sleep 300
done
