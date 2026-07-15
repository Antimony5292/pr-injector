#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_500/rq2_b_500_final_20260608"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_500/claude_bedrock_sonnet46_B500_fixed_harness_eval_20260608"
WORKTREES_DIR=".pri-workspace/rq2-sonnet46-b500-fixed-harness-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
LOG_DIR="experiments/rq2_500/logs"
LOG_FILE="${LOG_DIR}/rq2_sonnet46_b500_fixed_harness_20260608.log"
METRICS_FILE="experiments/rq2_500/rq3_metrics_sonnet46_b500_fixed_harness_20260608.json"
BUNDLED_PYTHON="/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
export RQ2_CLAUDE_WRAPPER_PYTHON="${RQ2_CLAUDE_WRAPPER_PYTHON:-${BUNDLED_PYTHON}}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

mkdir -p "${RUN_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start -----"
date
echo "[B500 fixed-harness] Pairing: ${PAIRING}"
echo "[B500 fixed-harness] Model: ${MODEL}"
echo "[B500 fixed-harness] Agent/eval output: ${RUN_DIR}"
echo "[B500 fixed-harness] B baseline mode: orphan"
echo "[B500 fixed-harness] PASS_TO_PASS: all runnable B_PASS_TO_PASS_CLEAN tests"

"${PYTHON_BIN}" scripts/collect_rq3_metrics.py \
  --final-dir "${FINAL_DIR}" \
  --b-run-dir "${RUN_DIR}" \
  --output "${METRICS_FILE}"

"${PYTHON_BIN}" scripts/run_rq2_claude_bedrock_eval.py \
  --pairing "${PAIRING}" \
  --output-dir "${RUN_DIR}" \
  --worktrees-dir "${WORKTREES_DIR}" \
  --repos-dir .pri-workspace/repos \
  --group B \
  --agent-timeout-s 1800 \
  --agent-infra-retries 3 \
  --test-timeout-s 300 \
  --max-pass-to-pass 0 \
  --b-baseline-mode orphan \
  --tools Bash,Edit,Read,Grep,Glob \
  --aws-region us-west-2 \
  --aws-profile default \
  --bedrock-model-id "${MODEL}" \
  --forbid-forbidden-edits \
  --require-pass-to-pass

"${PYTHON_BIN}" scripts/collect_rq3_metrics.py \
  --final-dir "${FINAL_DIR}" \
  --b-run-dir "${RUN_DIR}" \
  --output "${METRICS_FILE}"

date
echo "[B500 fixed-harness] done"
