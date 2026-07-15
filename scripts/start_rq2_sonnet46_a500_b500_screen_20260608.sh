#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

LOG_DIR="experiments/rq2_500/logs"
mkdir -p "${LOG_DIR}"

A_SESSION="rq2_sonnet46_a500_20260608"
B_SESSION="rq2_sonnet46_b500_20260608"

if screen -list | grep -q "[.]${A_SESSION}[[:space:]]"; then
  echo "A500 screen already running: ${A_SESSION}"
else
  screen -dmS "${A_SESSION}" bash -lc 'cd /Users/harmin/Desktop/pr-injector-main && bash scripts/launch_rq2_sonnet46_a500_fixed_harness_official_20260608.sh'
  echo "A500 screen started: ${A_SESSION}"
fi

if screen -list | grep -q "[.]${B_SESSION}[[:space:]]"; then
  echo "B500 screen already running: ${B_SESSION}"
else
  screen -dmS "${B_SESSION}" bash -lc 'cd /Users/harmin/Desktop/pr-injector-main && bash scripts/launch_rq2_sonnet46_b500_fixed_harness_20260608.sh'
  echo "B500 screen started: ${B_SESSION}"
fi

screen -list || true
echo "A log: experiments/rq2_500/logs/rq2_sonnet46_a500_fixed_harness_official_20260608.log"
echo "B log: experiments/rq2_500/logs/rq2_sonnet46_b500_fixed_harness_20260608.log"
