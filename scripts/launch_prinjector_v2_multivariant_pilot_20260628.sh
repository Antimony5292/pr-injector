#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/harmin/Desktop/pr-injector-main"
RUN_TAG="${PRI_V2_VARIANT_RUN_TAG:-20260628}"
PILOT_DIR="experiments/rq2_500/v2_multivariant_pilot_20260628"
OUT_ROOT="experiments/rq2_500/v2_multivariant_pilot_runs_${RUN_TAG}"
LOG_DIR="experiments/rq2_500/logs"
LOG_FILE="${LOG_DIR}/prinjector_v2_multivariant_pilot_${RUN_TAG}.log"

cd "${ROOT}"
mkdir -p "${OUT_ROOT}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start PR-INJECTOR v2 multivariant pilot ${RUN_TAG} -----"
date

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
export AWS_PROFILE="${AWS_PROFILE:-default}"
export PRI_ALLOW_L3_MODEL_CALLS=1
export PRI_BEDROCK_MODEL="${PRI_BEDROCK_MODEL:-arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6}"
export PRI_L3_MAX_TOKENS="${PRI_L3_MAX_TOKENS:-8192}"
export PRI_L3_TEMPERATURE="${PRI_L3_TEMPERATURE:-0}"
export PRI_RETRY_V2_GATE_WITH_L3="${PRI_RETRY_V2_GATE_WITH_L3:-1}"
export PRI_L3_REJECT_V2_GATE="${PRI_L3_REJECT_V2_GATE:-1}"

PY="${ROOT}/.venv/bin/python"

run_variant() {
  local variant="$1"
  local candidate_file="$2"
  local output_dir="$3"
  local max_p2p="$4"
  local max_adjacent="$5"

  echo
  echo "----- variant ${variant} -----"
  date
  "${PY}" scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py \
    --output-root "${output_dir}" \
    --candidate-file "${candidate_file}" \
    --ignore-processed \
    --limit 12 \
    --shard-size 12 \
    --workers "${PRI_V2_VARIANT_WORKERS:-1}" \
    --timeout "${PRI_V2_TEST_TIMEOUT:-300}" \
    --max-target-tests 8 \
    --max-pass-to-pass "${max_p2p}" \
    --max-adjacent-pass-to-pass "${max_adjacent}" \
    --enable-l3 \
    --v2-fidelity-gate \
    --v2-require-fidelity-gate
}

(
  unset PRI_SKIP_L1 PRI_SKIP_L2 PRI_FORCE_L3_SEMANTIC
  export PRI_ALLOW_WHOLE_FUNCTION_LEVEL2=0
  run_variant \
    "l1_clean_revert" \
    "${PILOT_DIR}/candidate_pool_l1_clean_revert.jsonl" \
    "${OUT_ROOT}/l1_clean_revert" \
    50 25
)

(
  export PRI_SKIP_L1=1
  unset PRI_SKIP_L2 PRI_FORCE_L3_SEMANTIC
  export PRI_ALLOW_WHOLE_FUNCTION_LEVEL2=0
  run_variant \
    "l2_ast_surgery" \
    "${PILOT_DIR}/candidate_pool_l2_ast_surgery.jsonl" \
    "${OUT_ROOT}/l2_ast_surgery" \
    50 25
)

(
  export PRI_FORCE_L3_SEMANTIC=1
  unset PRI_SKIP_L1 PRI_SKIP_L2
  run_variant \
    "l3_semantic" \
    "${PILOT_DIR}/candidate_pool_l3_semantic.jsonl" \
    "${OUT_ROOT}/l3_semantic" \
    50 25
)

(
  export PRI_FORCE_L3_SEMANTIC=1
  unset PRI_SKIP_L1 PRI_SKIP_L2
  run_variant \
    "l3_expanded_p2p" \
    "${PILOT_DIR}/candidate_pool_l3_expanded_p2p.jsonl" \
    "${OUT_ROOT}/l3_expanded_p2p" \
    100 50
)

echo
echo "----- finished PR-INJECTOR v2 multivariant pilot ${RUN_TAG} -----"
date
