#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FINAL_DIR="experiments/rq2_100/rq2_b_l1_l2_original_100_final_20260605"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_100/claude_bedrock_sonnet46_B100_fixed_harness_eval_20260606"
WORKTREES_DIR=".pri-workspace/rq2-sonnet46-b-fixed-harness-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
LOG_DIR="experiments/rq2_100/logs"
LOG_FILE="${LOG_DIR}/rq2_sonnet46_b_fixed_harness_20260606.log"
BUNDLED_PYTHON="${BUNDLED_PYTHON:-python3}"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
export RQ2_CLAUDE_WRAPPER_PYTHON="${RQ2_CLAUDE_WRAPPER_PYTHON:-${BUNDLED_PYTHON}}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

mkdir -p "${RUN_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- restart -----"
date
echo "[B fixed-harness] Pairing: ${PAIRING}"
echo "[B fixed-harness] Model: ${MODEL}"
echo "[B fixed-harness] Agent/eval output: ${RUN_DIR}"
echo "[B fixed-harness] B baseline mode: orphan"
echo "[B fixed-harness] PASS_TO_PASS: all runnable B_PASS_TO_PASS_CLEAN tests"

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

date
echo "[B fixed-harness] done"
