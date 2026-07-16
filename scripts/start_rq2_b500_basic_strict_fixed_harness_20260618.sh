#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_500/rq2_b_500_basic_strict_final_20260618"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
RUN_TAG="${RQ2_RUN_TAG:-20260618_fixed_harness}"
LOG_DIR="experiments/rq2_500/logs"
JOB_DIR="experiments/rq2_500/run_scripts/${RUN_TAG}"
BUNDLED_PYTHON="/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
SONNET_MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
START_STAGGER_S="${RQ2_START_STAGGER_S:-20}"

mkdir -p "${LOG_DIR}" "${JOB_DIR}"

if [[ ! -s "${PAIRING}" ]]; then
  echo "missing pairing: ${PAIRING}" >&2
  exit 1
fi

write_a_job() {
  local slug="$1"
  local agent_kind="$2"
  local agent_name="$3"
  local agent_model="$4"
  local runner_extra="$5"
  local job="${JOB_DIR}/${slug}_a500_${RUN_TAG}.sh"
  local run_dir="experiments/rq2_500/${slug}_A500_basic_strict_agentonly_${RUN_TAG}"
  local input_dir="experiments/rq2_500/official_a_eval_inputs_${slug}_basic_strict_${RUN_TAG}"
  local pro_dir="experiments/rq2_500/official_a_eval_pro_${slug}_basic_strict_${RUN_TAG}"
  local worktrees=".pri-workspace/${slug}-a500-basic-strict-${RUN_TAG}-worktrees"
  local metrics="experiments/rq2_500/rq3_metrics_${slug}_a500_basic_strict_${RUN_TAG}.json"
  local log="${LOG_DIR}/${slug}_a500_basic_strict_${RUN_TAG}.log"
  cat >"${job}" <<JOB
#!/usr/bin/env bash
set -u
cd /Users/harmin/Desktop/pr-injector-main
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONFAULTHANDLER=1
export RQ2_CLAUDE_WRAPPER_PYTHON="${BUNDLED_PYTHON}"
export RQ2_AGENT_RUNNER_PYTHON="${BUNDLED_PYTHON}"
mkdir -p "${run_dir}" "${input_dir}" "${pro_dir}" "${worktrees}" "${LOG_DIR}"
exec >>"${log}" 2>&1
echo
echo "----- start A ${slug} ${RUN_TAG} -----"
date
echo "[A500 basic-strict] final_dir=${FINAL_DIR}"
echo "[A500 basic-strict] agent=${agent_name}"
echo "[A500 basic-strict] model=${agent_model}"
"${PYTHON_BIN}" scripts/collect_rq3_metrics.py --final-dir "${FINAL_DIR}" --a-run-dir "${run_dir}" --output "${metrics}" || true
"${PYTHON_BIN}" scripts/run_rq2_claude_bedrock_eval.py \\
  --pairing "${PAIRING}" \\
  --output-dir "${run_dir}" \\
  --worktrees-dir "${worktrees}" \\
  --repos-dir .pri-workspace/repos \\
  --group A \\
  --agent-only \\
  $(if [[ "${agent_kind}" != "claude" ]]; then printf '%s' "--agent-kind ${agent_kind} --agent-name \"${agent_name}\" --agent-model \"${agent_model}\" ${runner_extra}"; fi) \\
  --agent-timeout-s 1800 \\
  --agent-infra-retries 3 \\
  --test-timeout-s 300 \\
  --max-pass-to-pass 0 \\
  --tools Bash,Edit,Read,Grep,Glob \\
  $(if [[ "${agent_kind}" == "claude" ]]; then printf '%s' "--aws-region us-west-2 --aws-profile default --bedrock-model-id \"${SONNET_MODEL}\""; else printf '%s' "--aws-region us-west-2 --aws-profile default"; fi) \\
  --forbid-forbidden-edits \\
  --require-pass-to-pass
agent_rc=\$?
echo "[A500 basic-strict] agent generation exit=\${agent_rc}"
if [[ "\${agent_rc}" -eq 0 ]]; then
  "${PYTHON_BIN}" scripts/run_rq2_official_a_eval.py \\
    --which all \\
    --pairing "${PAIRING}" \\
    --input-dir "${input_dir}" \\
    --results-dir "${run_dir}" \\
    --run-id-prefix "rq2_a500_${slug}_basic_strict_${RUN_TAG}" \\
    --swebench-workers 2 \\
    --verified-workers 1 \\
    --pro-workers 1 \\
    --timeout 1800 \\
    --model-name "${slug}-basic-strict-a500-${RUN_TAG}" \\
    --pro-output-dir "${pro_dir}" \\
    --include-invalid-as-empty \\
    --redo-pro
  eval_rc=\$?
  echo "[A500 basic-strict] official eval exit=\${eval_rc}"
else
  eval_rc=99
fi
"${PYTHON_BIN}" scripts/collect_rq3_metrics.py --final-dir "${FINAL_DIR}" --a-run-dir "${run_dir}" --output "${metrics}" || true
date
echo "[A500 basic-strict] done agent_rc=\${agent_rc} eval_rc=\${eval_rc}"
exit "\${agent_rc}"
JOB
  chmod +x "${job}"
}

write_b_job() {
  local slug="$1"
  local agent_kind="$2"
  local agent_name="$3"
  local agent_model="$4"
  local runner_extra="$5"
  local job="${JOB_DIR}/${slug}_b500_${RUN_TAG}.sh"
  local run_dir="experiments/rq2_500/${slug}_B500_basic_strict_eval_${RUN_TAG}"
  local worktrees=".pri-workspace/${slug}-b500-basic-strict-${RUN_TAG}-worktrees"
  local metrics="experiments/rq2_500/rq3_metrics_${slug}_b500_basic_strict_${RUN_TAG}.json"
  local log="${LOG_DIR}/${slug}_b500_basic_strict_${RUN_TAG}.log"
  cat >"${job}" <<JOB
#!/usr/bin/env bash
set -u
cd /Users/harmin/Desktop/pr-injector-main
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONFAULTHANDLER=1
export RQ2_CLAUDE_WRAPPER_PYTHON="${BUNDLED_PYTHON}"
export RQ2_AGENT_RUNNER_PYTHON="${BUNDLED_PYTHON}"
mkdir -p "${run_dir}" "${worktrees}" "${LOG_DIR}"
exec >>"${log}" 2>&1
echo
echo "----- start B ${slug} ${RUN_TAG} -----"
date
echo "[B500 basic-strict] final_dir=${FINAL_DIR}"
echo "[B500 basic-strict] agent=${agent_name}"
echo "[B500 basic-strict] model=${agent_model}"
echo "[B500 basic-strict] baseline mode=orphan"
"${PYTHON_BIN}" scripts/collect_rq3_metrics.py --final-dir "${FINAL_DIR}" --b-run-dir "${run_dir}" --output "${metrics}" || true
"${PYTHON_BIN}" scripts/run_rq2_claude_bedrock_eval.py \\
  --pairing "${PAIRING}" \\
  --output-dir "${run_dir}" \\
  --worktrees-dir "${worktrees}" \\
  --repos-dir .pri-workspace/repos \\
  --group B \\
  $(if [[ "${agent_kind}" != "claude" ]]; then printf '%s' "--agent-kind ${agent_kind} --agent-name \"${agent_name}\" --agent-model \"${agent_model}\" ${runner_extra}"; fi) \\
  --agent-timeout-s 1800 \\
  --agent-infra-retries 3 \\
  --test-timeout-s 300 \\
  --max-pass-to-pass 0 \\
  --b-baseline-mode orphan \\
  --tools Bash,Edit,Read,Grep,Glob \\
  $(if [[ "${agent_kind}" == "claude" ]]; then printf '%s' "--aws-region us-west-2 --aws-profile default --bedrock-model-id \"${SONNET_MODEL}\""; else printf '%s' "--aws-region us-west-2 --aws-profile default"; fi) \\
  --forbid-forbidden-edits \\
  --require-pass-to-pass
run_rc=\$?
echo "[B500 basic-strict] eval exit=\${run_rc}"
"${PYTHON_BIN}" scripts/collect_rq3_metrics.py --final-dir "${FINAL_DIR}" --b-run-dir "${run_dir}" --output "${metrics}" || true
date
echo "[B500 basic-strict] done run_rc=\${run_rc}"
exit "\${run_rc}"
JOB
  chmod +x "${job}"
}

start_job() {
  local name="$1"
  local job="$2"
  local pid_file="${LOG_DIR}/${name}.pid"
  local start_log="${LOG_DIR}/${name}.start.log"
  local session="rq2_${name}"
  session="${session//[^A-Za-z0-9_.-]/_}"
  if screen -ls | grep -q "[.]${session}[[:space:]]"; then
    echo "${name} already running: screen ${session}"
    return
  fi
  screen -dmS "${session}" bash -lc "cd /Users/harmin/Desktop/pr-injector-main && bash '${job}' >'${start_log}' 2>&1"
  echo "${session}" >"${pid_file}"
  echo "${name} started: screen ${session} job=${job}"
  sleep "${START_STAGGER_S}"
}

write_a_job "sonnet46" "claude" "Claude Code Sonnet 4.6" "${SONNET_MODEL}" ""
write_b_job "sonnet46" "claude" "Claude Code Sonnet 4.6" "${SONNET_MODEL}" ""
write_a_job "opencode_qwen3coder" "opencode" "OpenCode Qwen3-Coder-480B-A35B" "amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0" ""
write_b_job "opencode_qwen3coder" "opencode" "OpenCode Qwen3-Coder-480B-A35B" "amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0" ""
write_a_job "opencode_glm47" "opencode" "OpenCode ZAI GLM-4.7" "amazon-bedrock/zai.glm-4.7" ""
write_b_job "opencode_glm47" "opencode" "OpenCode ZAI GLM-4.7" "amazon-bedrock/zai.glm-4.7" ""
write_a_job "openhands_deepseek32" "openhands" "OpenHands DeepSeek V3.2" "bedrock/deepseek.v3.2" ""
write_b_job "openhands_deepseek32" "openhands" "OpenHands DeepSeek V3.2" "bedrock/deepseek.v3.2" ""

start_job "sonnet46_a500_basic_strict_${RUN_TAG}" "${JOB_DIR}/sonnet46_a500_${RUN_TAG}.sh"
start_job "sonnet46_b500_basic_strict_${RUN_TAG}" "${JOB_DIR}/sonnet46_b500_${RUN_TAG}.sh"
start_job "opencode_qwen3coder_a500_basic_strict_${RUN_TAG}" "${JOB_DIR}/opencode_qwen3coder_a500_${RUN_TAG}.sh"
start_job "opencode_qwen3coder_b500_basic_strict_${RUN_TAG}" "${JOB_DIR}/opencode_qwen3coder_b500_${RUN_TAG}.sh"
start_job "opencode_glm47_a500_basic_strict_${RUN_TAG}" "${JOB_DIR}/opencode_glm47_a500_${RUN_TAG}.sh"
start_job "opencode_glm47_b500_basic_strict_${RUN_TAG}" "${JOB_DIR}/opencode_glm47_b500_${RUN_TAG}.sh"
start_job "openhands_deepseek32_a500_basic_strict_${RUN_TAG}" "${JOB_DIR}/openhands_deepseek32_a500_${RUN_TAG}.sh"
start_job "openhands_deepseek32_b500_basic_strict_${RUN_TAG}" "${JOB_DIR}/openhands_deepseek32_b500_${RUN_TAG}.sh"

echo "Started fixed-harness RQ2 B500/A500 runs with RUN_TAG=${RUN_TAG}"
echo "Logs are under ${LOG_DIR}"
