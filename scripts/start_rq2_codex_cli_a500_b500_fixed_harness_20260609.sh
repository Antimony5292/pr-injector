#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

LOG_DIR="experiments/rq2_500/logs"
mkdir -p "${LOG_DIR}"

A_SCREEN="rq2_codex_cli_a500_20260609"
B_SCREEN="rq2_codex_cli_b500_20260609"

if screen -list | grep -q "[.]${A_SCREEN}[[:space:]]"; then
  echo "A500 Codex CLI already running in screen: ${A_SCREEN}"
else
  screen -dmS "${A_SCREEN}" bash "/scripts/launch_rq2_codex_cli_a500_fixed_harness_official_20260609.sh"
  echo "A500 Codex CLI started in screen: ${A_SCREEN}"
fi

if screen -list | grep -q "[.]${B_SCREEN}[[:space:]]"; then
  echo "B500 Codex CLI already running in screen: ${B_SCREEN}"
else
  screen -dmS "${B_SCREEN}" bash "/scripts/launch_rq2_codex_cli_b500_fixed_harness_20260609.sh"
  echo "B500 Codex CLI started in screen: ${B_SCREEN}"
fi

echo "A log: experiments/rq2_500/logs/rq2_codex_cli_a500_fixed_harness_official_20260609.log"
echo "B log: experiments/rq2_500/logs/rq2_codex_cli_b500_fixed_harness_20260609.log"
