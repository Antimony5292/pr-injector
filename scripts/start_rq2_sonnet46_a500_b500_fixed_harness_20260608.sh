#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

LOG_DIR="experiments/rq2_500/logs"
mkdir -p "${LOG_DIR}"

A_PID_FILE="${LOG_DIR}/rq2_sonnet46_a500_fixed_harness_20260608.pid"
B_PID_FILE="${LOG_DIR}/rq2_sonnet46_b500_fixed_harness_20260608.pid"
A_START_LOG="${LOG_DIR}/rq2_sonnet46_a500_fixed_harness_start_20260608.log"
B_START_LOG="${LOG_DIR}/rq2_sonnet46_b500_fixed_harness_start_20260608.log"

if [[ -s "${A_PID_FILE}" ]] && kill -0 "$(cat "${A_PID_FILE}")" 2>/dev/null; then
  echo "A500 already running: pid $(cat "${A_PID_FILE}")"
else
  nohup bash /Users/harmin/Desktop/pr-injector-main/scripts/launch_rq2_sonnet46_a500_fixed_harness_official_20260608.sh >"${A_START_LOG}" 2>&1 &
  echo "$!" >"${A_PID_FILE}"
  echo "A500 started: pid $(cat "${A_PID_FILE}")"
fi

if [[ -s "${B_PID_FILE}" ]] && kill -0 "$(cat "${B_PID_FILE}")" 2>/dev/null; then
  echo "B500 already running: pid $(cat "${B_PID_FILE}")"
else
  nohup bash /Users/harmin/Desktop/pr-injector-main/scripts/launch_rq2_sonnet46_b500_fixed_harness_20260608.sh >"${B_START_LOG}" 2>&1 &
  echo "$!" >"${B_PID_FILE}"
  echo "B500 started: pid $(cat "${B_PID_FILE}")"
fi

echo "A log: experiments/rq2_500/logs/rq2_sonnet46_a500_fixed_harness_official_20260608.log"
echo "B log: experiments/rq2_500/logs/rq2_sonnet46_b500_fixed_harness_20260608.log"
