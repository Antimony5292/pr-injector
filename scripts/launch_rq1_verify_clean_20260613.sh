#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/harmin/Desktop/pr-injector-main"
cd "${ROOT}"

OUT_DIR="experiments/rq1_sonnet46_20260611"
CANDIDATES="${OUT_DIR}/candidate_pool_rq1_20260611.jsonl"
INJECTION_RESULTS="${OUT_DIR}/injection_results.jsonl"
VERIFICATION_RESULTS="${OUT_DIR}/verification_results_clean_20260613.jsonl"
SUMMARY="${OUT_DIR}/rq1_summary_clean_20260613.json"
LOG_DIR="${OUT_DIR}/logs"
LOG_FILE="${LOG_DIR}/rq1_verify_clean_20260613.launch.log"

mkdir -p "${LOG_DIR}"
exec >> "${LOG_FILE}" 2>&1

echo "[RQ1-clean-verify] started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[RQ1-clean-verify] candidates=${CANDIDATES}"
echo "[RQ1-clean-verify] injection_results=${INJECTION_RESULTS}"
echo "[RQ1-clean-verify] verification_results=${VERIFICATION_RESULTS}"

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
export AWS_PROFILE="${AWS_PROFILE:-default}"

# Verification itself should be deterministic and non-agentic. Shared venvs keep
# repeated modern-HEAD test environments from being rebuilt for every case.
export PRI_SHARED_REPO_VENV=1
export PRI_SHARED_REPO_VENV_TAG="rq1_clean_20260613"

PY="${ROOT}/.venv/bin/python"
REPOS_DIR="${ROOT}/.pri-workspace/repos"
VERIFY_WORKTREES="${ROOT}/.pri-workspace/rq1-sonnet46-verify-clean-20260613-worktrees"

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
  --max-target-tests 8 \
  --force

"${PY}" scripts/analyze_rq1_experiment.py \
  --candidates "${CANDIDATES}" \
  --injection-results "${INJECTION_RESULTS}" \
  --verification-results "${VERIFICATION_RESULTS}" \
  --output "${SUMMARY}"

echo "[RQ1-clean-verify] finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "[RQ1-clean-verify] summary=${SUMMARY}"
