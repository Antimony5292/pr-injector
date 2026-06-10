#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FINAL_DIR="experiments/rq2_500/rq2_b_500_final_20260608"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_500/opencode_qwen3coder_A500_fixed_harness_agentonly_20260609"
OFFICIAL_INPUT_DIR="experiments/rq2_500/official_a_eval_inputs_opencode_qwen3coder_fixed_harness_20260609"
PRO_OUTPUT_DIR="experiments/rq2_500/official_a_eval_pro_opencode_qwen3coder_fixed_harness_20260609"
WORKTREES_DIR=".pri-workspace/rq2-opencode-qwen3coder-a500-fixed-harness-worktrees"
MODEL="amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0"
LOG_DIR="experiments/rq2_500/logs"
LOG_FILE="${LOG_DIR}/rq2_opencode_qwen3coder_a500_fixed_harness_official_20260609.log"
METRICS_FILE="experiments/rq2_500/rq3_metrics_opencode_qwen3coder_a500_fixed_harness_20260609.json"
BUNDLED_PYTHON="${BUNDLED_PYTHON:-python3}"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

mkdir -p "${RUN_DIR}" "${OFFICIAL_INPUT_DIR}" "${PRO_OUTPUT_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start -----"
date
echo "[A500 OpenCode Qwen3 Coder fixed-harness] Pairing: ${PAIRING}"
echo "[A500 OpenCode Qwen3 Coder fixed-harness] Model: ${MODEL}"
echo "[A500 OpenCode Qwen3 Coder fixed-harness] Agent output: ${RUN_DIR}"
echo "[A500 OpenCode Qwen3 Coder fixed-harness] Official input: ${OFFICIAL_INPUT_DIR}"
echo "[A500 OpenCode Qwen3 Coder fixed-harness] Pro official output: ${PRO_OUTPUT_DIR}"

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
  --agent-kind opencode \
  --agent-name "OpenCode + Bedrock Qwen3 Coder 480B" \
  --agent-model "${MODEL}" \
  --agent-timeout-s 1800 \
  --agent-infra-retries 2 \
  --test-timeout-s 300 \
  --max-pass-to-pass 0 \
  --tools Bash,Edit,Read,Grep,Glob \
  --aws-region us-west-2 \
  --aws-profile default \
  --forbid-forbidden-edits \
  --require-pass-to-pass

"${PYTHON_BIN}" scripts/run_rq2_official_a_eval.py \
  --which all \
  --pairing "${PAIRING}" \
  --input-dir "${OFFICIAL_INPUT_DIR}" \
  --results-dir "${RUN_DIR}" \
  --run-id-prefix rq2_a500_opencode_qwen3coder_fixed_harness_20260609 \
  --swebench-workers 1 \
  --verified-workers 1 \
  --pro-workers 1 \
  --timeout 1800 \
  --model-name opencode-qwen3-coder-480b-fixed-harness-a500 \
  --pro-output-dir "${PRO_OUTPUT_DIR}" \
  --include-invalid-as-empty \
  --redo-pro

"${PYTHON_BIN}" scripts/collect_rq3_metrics.py \
  --final-dir "${FINAL_DIR}" \
  --a-run-dir "${RUN_DIR}" \
  --output "${METRICS_FILE}"

date
echo "[A500 OpenCode Qwen3 Coder fixed-harness] done"
