#!/usr/bin/env bash
set -euo pipefail

ROOT="/Users/harmin/Desktop/pr-injector-main"
RUN_TAG="${REPOLAUNCH_RUN_TAG:-20260701_full_bedrock_docker}"
BASE_DIR="${ROOT}/experiments/repolaunch_prinjector_coverage_20260628"
OUT_DIR="${BASE_DIR}/repolaunch_${RUN_TAG}"
WORKSPACE_ROOT="${OUT_DIR}/workspace"
LOG_DIR="${BASE_DIR}/logs"
LOG_FILE="${LOG_DIR}/repolaunch_${RUN_TAG}.log"
PY="${ROOT}/.venvs/repolaunch/bin/python"
LIMIT="${REPOLAUNCH_FULL_LIMIT:-1168}"

cd "${ROOT}"
mkdir -p "${OUT_DIR}" "${WORKSPACE_ROOT}" "${LOG_DIR}"
exec >>"${LOG_FILE}" 2>&1

echo
echo "----- start RepoLaunch x PR-INJECTOR full ${RUN_TAG} -----"
date
echo "root=${ROOT}"
echo "out_dir=${OUT_DIR}"
echo "workspace_root=${WORKSPACE_ROOT}"
echo "full_limit=${LIMIT}"

export AWS_REGION="${AWS_REGION:-us-west-2}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-${AWS_REGION}}"
export AWS_PROFILE="${AWS_PROFILE:-default}"

"${PY}" scripts/build_repolaunch_smoke_inputs.py \
  --matrix "${BASE_DIR}/repolaunch_prinjector_matrix.jsonl" \
  --output-dir "${OUT_DIR}" \
  --workspace-root "${WORKSPACE_ROOT}" \
  --limit "${LIMIT}" \
  --workers "${REPOLAUNCH_WORKERS:-4}" \
  --model "${REPOLAUNCH_MODEL:-bedrock/global.anthropic.claude-sonnet-4-6}" \
  --max-steps-setup "${REPOLAUNCH_MAX_STEPS_SETUP:-16}" \
  --max-steps-verify "${REPOLAUNCH_MAX_STEPS_VERIFY:-10}" \
  --cmd-timeout "${REPOLAUNCH_CMD_TIMEOUT:-30}" \
  --max-trials "${REPOLAUNCH_MAX_TRIALS:-1}"

"${PY}" -m launch.run --config-path "${OUT_DIR}/config.json"

"${PY}" scripts/merge_repolaunch_results.py \
  --matrix "${BASE_DIR}/repolaunch_prinjector_matrix.jsonl" \
  --workspace-root "${WORKSPACE_ROOT}" \
  --output-dir "${OUT_DIR}/merged"

echo "----- finished RepoLaunch x PR-INJECTOR full ${RUN_TAG} -----"
date
