#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FINAL_DIR="experiments/rq2_100/rq2_b_l1_l2_original_100_final_20260605"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_100/claude_bedrock_sonnet46_B100_eval_20260606"
WORKTREES_DIR=".pri-workspace/rq2-sonnet46-b-eval-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
LOG_DIR="experiments/rq2_100/logs"
LOG_FILE="${LOG_DIR}/rq2_sonnet46_b_eval_20260606.log"

mkdir -p "${RUN_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >"${LOG_FILE}" 2>&1

date
echo "[B] Pairing: ${PAIRING}"
echo "[B] Model: ${MODEL}"
echo "[B] Agent/eval output: ${RUN_DIR}"

python3 scripts/run_rq2_claude_bedrock_eval.py \
  --pairing "${PAIRING}" \
  --output-dir "${RUN_DIR}" \
  --worktrees-dir "${WORKTREES_DIR}" \
  --repos-dir .pri-workspace/repos \
  --group B \
  --agent-timeout-s 1800 \
  --test-timeout-s 300 \
  --max-pass-to-pass 20 \
  --tools Bash,Edit,Read,Grep,Glob \
  --aws-region us-west-2 \
  --aws-profile default \
  --bedrock-model-id "${MODEL}" \
  --forbid-forbidden-edits \
  --require-pass-to-pass

date
echo "[B] done"
