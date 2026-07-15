#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/harmin/Desktop/pr-injector-main"
RUN_TAG="${PRI_V2_RECOVERY_RUN_TAG:-20260626}"
OUT_DIR="experiments/rq2_500/v2_network_recovery_${RUN_TAG}"
CANDIDATES="experiments/rq2_500/v2_recovery_pool_20260626/recovery_candidates.jsonl"
LOG_DIR="experiments/rq2_500/logs"
LOG_FILE="${LOG_DIR}/prinjector_v2_network_recovery_${RUN_TAG}.log"

cd "${ROOT}"
mkdir -p "${OUT_DIR}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start PR-INJECTOR v2 network recovery ${RUN_TAG} -----"
date
echo "root=${ROOT}"
echo "out_dir=${OUT_DIR}"
echo "candidates=${CANDIDATES}"

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
export AWS_PROFILE="${AWS_PROFILE:-default}"

export PRI_ALLOW_L3_MODEL_CALLS=1
export PRI_BEDROCK_MODEL="${PRI_BEDROCK_MODEL:-arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6}"
export PRI_L3_APPLY_ATTEMPTS="${PRI_L3_APPLY_ATTEMPTS:-3}"
export PRI_L3_MAX_TOKENS="${PRI_L3_MAX_TOKENS:-8192}"
export PRI_L3_TEMPERATURE="${PRI_L3_TEMPERATURE:-0}"
export PRI_L3_DEBUG_DIR="${OUT_DIR}/l3_debug"

export PRI_LEVEL2_MODE="${PRI_LEVEL2_MODE:-conservative_hunk_first}"
export PRI_ALLOW_WHOLE_FUNCTION_LEVEL2="${PRI_ALLOW_WHOLE_FUNCTION_LEVEL2:-0}"
export PRI_REJECT_COMPATIBILITY="${PRI_REJECT_COMPATIBILITY:-1}"
export PRI_RETRY_V2_GATE_WITH_L3="${PRI_RETRY_V2_GATE_WITH_L3:-1}"
export PRI_L3_REJECT_V2_GATE="${PRI_L3_REJECT_V2_GATE:-1}"

PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "missing executable python: ${PY}" >&2
  exit 1
fi

"${PY}" scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py \
  --output-root "${OUT_DIR}" \
  --candidate-file "${CANDIDATES}" \
  --ignore-processed \
  --limit "${PRI_V2_RECOVERY_LIMIT:-171}" \
  --shard-size "${PRI_V2_RECOVERY_SHARD_SIZE:-30}" \
  --workers "${PRI_V2_RECOVERY_WORKERS:-2}" \
  --timeout "${PRI_V2_TEST_TIMEOUT:-300}" \
  --max-target-tests 8 \
  --max-pass-to-pass 50 \
  --max-adjacent-pass-to-pass 25 \
  --enable-l3 \
  --v2-fidelity-gate \
  --v2-require-fidelity-gate

rc=$?
echo "----- finished PR-INJECTOR v2 network recovery ${RUN_TAG}: rc=${rc} -----"
date
exit "${rc}"
