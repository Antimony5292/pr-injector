#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/harmin/Desktop/pr-injector-main"
cd "${ROOT}"

OUT_DIR="experiments/rq1_sonnet46_20260611"
CANDIDATES="${OUT_DIR}/candidate_pool_rq1_20260611.jsonl"
INJECTION_RESULTS="${OUT_DIR}/injection_results.jsonl"
VERIFICATION_RESULTS="${OUT_DIR}/verification_results.jsonl"
SUMMARY="${OUT_DIR}/rq1_summary.json"
LOG_DIR="${OUT_DIR}/logs"
LOG_FILE="${LOG_DIR}/rq1_sonnet46_20260611.log"

mkdir -p "${LOG_DIR}" "${OUT_DIR}/l3_debug"
exec >> "${LOG_FILE}" 2>&1

echo "[RQ1] started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[RQ1] candidates=${CANDIDATES}"
echo "[RQ1] injection_results=${INJECTION_RESULTS}"
echo "[RQ1] verification_results=${VERIFICATION_RESULTS}"

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
export AWS_PROFILE="${AWS_PROFILE:-default}"

# Level 3 is an explicit fallback for semantic reversion during benchmark
# construction. It is not the RQ2 repair agent.
export PRI_ALLOW_L3_MODEL_CALLS=1
export PRI_BEDROCK_MODEL="${PRI_BEDROCK_MODEL:-arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6}"
export PRI_L3_APPLY_ATTEMPTS="${PRI_L3_APPLY_ATTEMPTS:-2}"
export PRI_L3_DEBUG_DIR="${OUT_DIR}/l3_debug"

# Use the conservative Level 2 configuration from the final B-set construction:
# prefer hunk-level reverse transformations, do not fall back to whole-function
# replacement, and reject diffs that introduce missing symbols/API drift.
export PRI_LEVEL2_MODE="${PRI_LEVEL2_MODE:-conservative_hunk_first}"
export PRI_ALLOW_WHOLE_FUNCTION_LEVEL2="${PRI_ALLOW_WHOLE_FUNCTION_LEVEL2:-0}"
export PRI_REJECT_COMPATIBILITY="${PRI_REJECT_COMPATIBILITY:-1}"

PY="${ROOT}/.venv/bin/python"
REPOS_DIR="${ROOT}/.pri-workspace/repos"
INJECT_WORKTREES="${ROOT}/.pri-workspace/rq1-sonnet46-inject-worktrees"
VERIFY_WORKTREES="${ROOT}/.pri-workspace/rq1-sonnet46-verify-worktrees"

"${PY}" scripts/inject_swebench_pro.py \
  --input "${CANDIDATES}" \
  --output "${INJECTION_RESULTS}" \
  --repos-dir "${REPOS_DIR}" \
  --worktrees-dir "${INJECT_WORKTREES}" \
  --timeout 300 \
  --enable-l3 \
  --preflight-target-tests \
  --max-target-tests 8

"${PY}" scripts/verify_swebench_pro.py \
  --injection-results "${INJECTION_RESULTS}" \
  --sampled-data "${CANDIDATES}" \
  --output "${VERIFICATION_RESULTS}" \
  --repos-dir "${REPOS_DIR}" \
  --worktrees-dir "${VERIFY_WORKTREES}" \
  --timeout 300 \
  --check-pass-to-pass \
  --clean-pass-to-pass \
  --require-clean-pass-to-pass \
  --max-pass-to-pass 50 \
  --check-golden-repair \
  --max-target-tests 8

"${PY}" scripts/analyze_rq1_experiment.py \
  --candidates "${CANDIDATES}" \
  --injection-results "${INJECTION_RESULTS}" \
  --verification-results "${VERIFICATION_RESULTS}" \
  --output "${SUMMARY}"

echo "[RQ1] finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[RQ1] summary=${SUMMARY}"
