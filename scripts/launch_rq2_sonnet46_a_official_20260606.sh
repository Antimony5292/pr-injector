#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_100/rq2_b_l1_l2_original_100_final_20260605"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_100/claude_bedrock_sonnet46_A100_agentonly_20260606"
OFFICIAL_INPUT_DIR="experiments/rq2_100/official_a_eval_inputs_sonnet46_20260606"
PRO_OUTPUT_DIR="experiments/rq2_100/official_a_eval_pro_sonnet46_20260606"
WORKTREES_DIR=".pri-workspace/rq2-sonnet46-a-agent-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
LOG_DIR="experiments/rq2_100/logs"
LOG_FILE="${LOG_DIR}/rq2_sonnet46_a_official_20260606.log"

mkdir -p "${RUN_DIR}" "${OFFICIAL_INPUT_DIR}" "${PRO_OUTPUT_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >"${LOG_FILE}" 2>&1

date
echo "[A] Pairing: ${PAIRING}"
echo "[A] Model: ${MODEL}"
echo "[A] Agent output: ${RUN_DIR}"
echo "[A] Official input: ${OFFICIAL_INPUT_DIR}"
echo "[A] Pro official output: ${PRO_OUTPUT_DIR}"

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
  --run-id-prefix rq2_a_sonnet46_20260606 \
  --verified-workers 1 \
  --pro-workers 1 \
  --timeout 1800 \
  --model-name claude-bedrock-sonnet-4.6 \
  --pro-output-dir "${PRO_OUTPUT_DIR}" \
  --include-invalid-as-empty \
  --redo-pro

date
echo "[A] done"
