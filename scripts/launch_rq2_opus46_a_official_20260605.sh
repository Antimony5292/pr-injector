#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_100/rq2_b_l1_l2_original_100_final_20260605"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_100/claude_bedrock_opus46_A100_agentonly_20260605"
OFFICIAL_INPUT_DIR="experiments/rq2_100/official_a_eval_inputs_opus46_20260605"
WORKTREES_DIR=".pri-workspace/rq2-opus46-a-agent-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-opus-4-6-v1"
LOG_DIR="experiments/rq2_100/logs"
LOG_FILE="${LOG_DIR}/rq2_opus46_a_official_20260605.log"

mkdir -p "${RUN_DIR}" "${OFFICIAL_INPUT_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >"${LOG_FILE}" 2>&1

date
echo "[A] Pairing: ${PAIRING}"
echo "[A] Model: ${MODEL}"
echo "[A] Agent output: ${RUN_DIR}"
echo "[A] Official input: ${OFFICIAL_INPUT_DIR}"

python3 scripts/run_rq2_claude_bedrock_eval.py \
  --pairing "${PAIRING}" \
  --output-dir "${RUN_DIR}" \
  --worktrees-dir "${WORKTREES_DIR}" \
  --repos-dir .pri-workspace/repos \
  --group A \
  --agent-only \
  --agent-timeout-s 1800 \
  --test-timeout-s 300 \
  --max-pass-to-pass 20 \
  --tools Bash,Edit,Read,Grep,Glob \
  --aws-region us-west-2 \
  --aws-profile default \
  --bedrock-model-id "${MODEL}" \
  --forbid-forbidden-edits \
  --require-pass-to-pass

python3 scripts/run_rq2_official_a_eval.py \
  --which both \
  --pairing "${PAIRING}" \
  --input-dir "${OFFICIAL_INPUT_DIR}" \
  --results-dir "${RUN_DIR}" \
  --run-id-prefix rq2_a_opus46_20260605 \
  --verified-workers 1 \
  --pro-workers 1 \
  --timeout 1800 \
  --model-name claude-bedrock-opus-4.6 \
  --include-invalid-as-empty \
  --redo-pro

date
echo "[A] done"
