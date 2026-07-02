#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_500/rq2_b_500_final_20260608"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_500/claude_bedrock_sonnet46_A500_fixed_harness_agentonly_20260608"
OFFICIAL_INPUT_DIR="experiments/rq2_500/official_a_eval_inputs_sonnet46_fixed_harness_20260608"
PRO_OUTPUT_DIR="experiments/rq2_500/official_a_eval_pro_sonnet46_fixed_harness_20260608"
WORKTREES_DIR=".pri-workspace/rq2-sonnet46-a500-fixed-harness-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
LOG_DIR="experiments/rq2_500/logs"
LOG_FILE="${LOG_DIR}/rq2_sonnet46_a500_fixed_harness_official_20260608.log"
METRICS_FILE="experiments/rq2_500/rq3_metrics_sonnet46_a500_fixed_harness_20260608.json"
BUNDLED_PYTHON="/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
export RQ2_CLAUDE_WRAPPER_PYTHON="${RQ2_CLAUDE_WRAPPER_PYTHON:-${BUNDLED_PYTHON}}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

mkdir -p "${RUN_DIR}" "${OFFICIAL_INPUT_DIR}" "${PRO_OUTPUT_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start -----"
date
echo "[A500 fixed-harness] Pairing: ${PAIRING}"
echo "[A500 fixed-harness] Model: ${MODEL}"
echo "[A500 fixed-harness] Agent output: ${RUN_DIR}"
echo "[A500 fixed-harness] Official input: ${OFFICIAL_INPUT_DIR}"
echo "[A500 fixed-harness] Pro official output: ${PRO_OUTPUT_DIR}"

"${PYTHON_BIN}" scripts/collect_rq3_metrics.py \
  --final-dir "${FINAL_DIR}" \
  --a-run-dir "${RUN_DIR}" \
  --output "${METRICS_FILE}"

"${PYTHON_BIN}" scripts/run_rq2_claude_bedrock_eval.py \
  --pairing "${PAIRING}" \
  --output-dir "${RUN_DIR}" \
  --worktrees-dir "${WORKTREES_DIR}" \
  --repos-dir .pri-workspace/repos \
  --group A \
  --agent-only \
  --agent-timeout-s 1800 \
  --agent-infra-retries 3 \
  --test-timeout-s 300 \
  --max-pass-to-pass 0 \
  --tools Bash,Edit,Read,Grep,Glob \
  --aws-region us-west-2 \
  --aws-profile default \
  --bedrock-model-id "${MODEL}" \
  --forbid-forbidden-edits \
  --require-pass-to-pass

"${PYTHON_BIN}" scripts/run_rq2_official_a_eval.py \
  --which all \
  --pairing "${PAIRING}" \
  --input-dir "${OFFICIAL_INPUT_DIR}" \
  --results-dir "${RUN_DIR}" \
  --run-id-prefix rq2_a500_sonnet46_fixed_harness_20260608 \
  --swebench-workers 1 \
  --verified-workers 1 \
  --pro-workers 1 \
  --timeout 1800 \
  --model-name claude-bedrock-sonnet-4.6-fixed-harness-a500 \
  --pro-output-dir "${PRO_OUTPUT_DIR}" \
  --include-invalid-as-empty \
  --redo-pro

"${PYTHON_BIN}" scripts/collect_rq3_metrics.py \
  --final-dir "${FINAL_DIR}" \
  --a-run-dir "${RUN_DIR}" \
  --output "${METRICS_FILE}"

date
echo "[A500 fixed-harness] done"
