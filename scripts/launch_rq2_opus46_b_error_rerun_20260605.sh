#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

FINAL_DIR="experiments/rq2_100/rq2_b_l1_l2_original_100_final_20260605"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_DIR="experiments/rq2_100/claude_bedrock_opus46_B100_eval_20260605"
WORKTREES_DIR=".pri-workspace/rq2-opus46-b-eval-worktrees"
MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-opus-4-6-v1"
LOG_DIR="experiments/rq2_100/logs"
LOG_FILE="${LOG_DIR}/rq2_opus46_b_error_rerun_20260605.log"

mkdir -p "${RUN_DIR}" "${WORKTREES_DIR}" "${LOG_DIR}"
exec >"${LOG_FILE}" 2>&1

date
echo "[B-rerun] Pairing: ${PAIRING}"
echo "[B-rerun] Model: ${MODEL}"
echo "[B-rerun] Output: ${RUN_DIR}"

CASE_IDS=()
while IFS= read -r case_id; do
  CASE_IDS+=("${case_id}")
done < <(
python3 - <<'PY'
import json
from pathlib import Path
root = Path("experiments/rq2_100/claude_bedrock_opus46_B100_eval_20260605")
case_ids = []
for p in sorted(root.glob("RQ2_*/*/result.json")):
    row = json.loads(p.read_text())
    if (row.get("evaluation") or {}).get("status") == "error":
        case_ids.append(row["case_id"])
for case_id in case_ids:
    print(case_id)
PY
)

echo "[B-rerun] Error cases: ${#CASE_IDS[@]}"
if [[ "${#CASE_IDS[@]}" -eq 0 ]]; then
  echo "[B-rerun] No error cases to rerun."
  exit 0
fi

CASE_ARGS=()
for case_id in "${CASE_IDS[@]}"; do
  CASE_ARGS+=(--case-id "${case_id}")
done

python3 scripts/run_rq2_claude_bedrock_eval.py \
  --pairing "${PAIRING}" \
  --output-dir "${RUN_DIR}" \
  --worktrees-dir "${WORKTREES_DIR}" \
  --repos-dir .pri-workspace/repos \
  --group B \
  "${CASE_ARGS[@]}" \
  --force \
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
echo "[B-rerun] done"
