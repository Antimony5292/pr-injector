#!/usr/bin/env bash
set -euo pipefail

cd /Users/harmin/Desktop/pr-injector-main

FINAL_DIR="experiments/rq2_500/rq2_b_500_basic_strict_final_20260618"
PAIRING="${FINAL_DIR}/rq2_pairing_table.jsonl"
LOG_DIR="experiments/rq2_500/logs"
BUNDLED_PYTHON="/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
PYTHON_BIN="${PYTHON_BIN:-${BUNDLED_PYTHON}}"
SONNET_MODEL="arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"

mkdir -p "${LOG_DIR}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export RQ2_CLAUDE_WRAPPER_PYTHON="${RQ2_CLAUDE_WRAPPER_PYTHON:-${BUNDLED_PYTHON}}"

start_bg() {
  local name="$1"
  local cmd="$2"
  local pid_file="${LOG_DIR}/${name}.pid"
  local start_log="${LOG_DIR}/${name}.start.log"
  if [[ -s "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
    echo "${name} already running: pid $(cat "${pid_file}")"
    return
  fi
  nohup bash -lc "${cmd}" >"${start_log}" 2>&1 &
  echo "$!" >"${pid_file}"
  echo "${name} started: pid $(cat "${pid_file}")"
}

launch_a() {
  local slug="$1"
  local agent_kind="$2"
  local agent_name="$3"
  local agent_model="$4"
  local runner_extra="$5"
  local run_dir="experiments/rq2_500/${slug}_A500_basic_strict_agentonly_20260618"
  local input_dir="experiments/rq2_500/official_a_eval_inputs_${slug}_basic_strict_20260618"
  local pro_dir="experiments/rq2_500/official_a_eval_pro_${slug}_basic_strict_20260618"
  local worktrees=".pri-workspace/${slug}-a500-basic-strict-worktrees"
  local metrics="experiments/rq2_500/rq3_metrics_${slug}_a500_basic_strict_20260618.json"
  local log="${LOG_DIR}/${slug}_a500_basic_strict_official_20260618.log"
  local agent_args=""
  if [[ "${agent_kind}" != "claude" ]]; then
    agent_args="--agent-kind ${agent_kind} --agent-name \"${agent_name}\" --agent-model \"${agent_model}\" ${runner_extra}"
  fi
  local bedrock_args=""
  if [[ "${agent_kind}" == "claude" ]]; then
    bedrock_args="--aws-region us-west-2 --aws-profile default --bedrock-model-id \"${SONNET_MODEL}\""
  fi
  start_bg "${slug}_a500_basic_strict_20260618" "
    set -euo pipefail
    cd /Users/harmin/Desktop/pr-injector-main
    mkdir -p \"${run_dir}\" \"${input_dir}\" \"${pro_dir}\" \"${worktrees}\" \"${LOG_DIR}\"
    exec >>\"${log}\" 2>&1
    echo
    echo '----- start A -----'
    date
    echo '[A500 basic-strict] final_dir=${FINAL_DIR}'
    echo '[A500 basic-strict] agent=${agent_name} model=${agent_model}'
    \"${PYTHON_BIN}\" scripts/collect_rq3_metrics.py --final-dir \"${FINAL_DIR}\" --a-run-dir \"${run_dir}\" --output \"${metrics}\" || true
    \"${PYTHON_BIN}\" scripts/run_rq2_claude_bedrock_eval.py \
      --pairing \"${PAIRING}\" \
      --output-dir \"${run_dir}\" \
      --worktrees-dir \"${worktrees}\" \
      --repos-dir .pri-workspace/repos \
      --group A \
      --agent-only \
      ${agent_args} \
      --agent-timeout-s 1800 \
      --agent-infra-retries 3 \
      --test-timeout-s 300 \
      --max-pass-to-pass 0 \
      --tools Bash,Edit,Read,Grep,Glob \
      ${bedrock_args} \
      --forbid-forbidden-edits \
      --require-pass-to-pass
    \"${PYTHON_BIN}\" scripts/run_rq2_official_a_eval.py \
      --which all \
      --pairing \"${PAIRING}\" \
      --input-dir \"${input_dir}\" \
      --results-dir \"${run_dir}\" \
      --run-id-prefix \"rq2_a500_${slug}_basic_strict_20260618\" \
      --swebench-workers 2 \
      --verified-workers 1 \
      --pro-workers 1 \
      --timeout 1800 \
      --model-name \"${slug}-basic-strict-a500\" \
      --pro-output-dir \"${pro_dir}\" \
      --include-invalid-as-empty \
      --redo-pro
    \"${PYTHON_BIN}\" scripts/collect_rq3_metrics.py --final-dir \"${FINAL_DIR}\" --a-run-dir \"${run_dir}\" --output \"${metrics}\" || true
    date
    echo '[A500 basic-strict] done'
  "
}

launch_b() {
  local slug="$1"
  local agent_kind="$2"
  local agent_name="$3"
  local agent_model="$4"
  local runner_extra="$5"
  local run_dir="experiments/rq2_500/${slug}_B500_basic_strict_eval_20260618"
  local worktrees=".pri-workspace/${slug}-b500-basic-strict-worktrees"
  local metrics="experiments/rq2_500/rq3_metrics_${slug}_b500_basic_strict_20260618.json"
  local log="${LOG_DIR}/${slug}_b500_basic_strict_20260618.log"
  local agent_args=""
  if [[ "${agent_kind}" != "claude" ]]; then
    agent_args="--agent-kind ${agent_kind} --agent-name \"${agent_name}\" --agent-model \"${agent_model}\" ${runner_extra}"
  fi
  local bedrock_args=""
  if [[ "${agent_kind}" == "claude" ]]; then
    bedrock_args="--aws-region us-west-2 --aws-profile default --bedrock-model-id \"${SONNET_MODEL}\""
  fi
  start_bg "${slug}_b500_basic_strict_20260618" "
    set -euo pipefail
    cd /Users/harmin/Desktop/pr-injector-main
    mkdir -p \"${run_dir}\" \"${worktrees}\" \"${LOG_DIR}\"
    exec >>\"${log}\" 2>&1
    echo
    echo '----- start B -----'
    date
    echo '[B500 basic-strict] final_dir=${FINAL_DIR}'
    echo '[B500 basic-strict] agent=${agent_name} model=${agent_model}'
    echo '[B500 basic-strict] baseline mode=orphan'
    \"${PYTHON_BIN}\" scripts/collect_rq3_metrics.py --final-dir \"${FINAL_DIR}\" --b-run-dir \"${run_dir}\" --output \"${metrics}\" || true
    \"${PYTHON_BIN}\" scripts/run_rq2_claude_bedrock_eval.py \
      --pairing \"${PAIRING}\" \
      --output-dir \"${run_dir}\" \
      --worktrees-dir \"${worktrees}\" \
      --repos-dir .pri-workspace/repos \
      --group B \
      ${agent_args} \
      --agent-timeout-s 1800 \
      --agent-infra-retries 3 \
      --test-timeout-s 300 \
      --max-pass-to-pass 0 \
      --b-baseline-mode orphan \
      --tools Bash,Edit,Read,Grep,Glob \
      ${bedrock_args} \
      --forbid-forbidden-edits \
      --require-pass-to-pass
    \"${PYTHON_BIN}\" scripts/collect_rq3_metrics.py --final-dir \"${FINAL_DIR}\" --b-run-dir \"${run_dir}\" --output \"${metrics}\" || true
    date
    echo '[B500 basic-strict] done'
  "
}

if [[ ! -s "${PAIRING}" ]]; then
  echo "missing pairing: ${PAIRING}" >&2
  exit 1
fi

launch_a "sonnet46" "claude" "Claude Code Sonnet 4.6" "${SONNET_MODEL}" ""
launch_b "sonnet46" "claude" "Claude Code Sonnet 4.6" "${SONNET_MODEL}" ""

launch_a "opencode_qwen3coder" "opencode" "OpenCode Qwen3-Coder-480B-A35B" "amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0" ""
launch_b "opencode_qwen3coder" "opencode" "OpenCode Qwen3-Coder-480B-A35B" "amazon-bedrock/qwen.qwen3-coder-480b-a35b-v1:0" ""

launch_a "opencode_glm47" "opencode" "OpenCode ZAI GLM-4.7" "amazon-bedrock/zai.glm-4.7" ""
launch_b "opencode_glm47" "opencode" "OpenCode ZAI GLM-4.7" "amazon-bedrock/zai.glm-4.7" ""

launch_a "openhands_deepseek32" "openhands" "OpenHands DeepSeek V3.2" "bedrock/deepseek.v3.2" ""
launch_b "openhands_deepseek32" "openhands" "OpenHands DeepSeek V3.2" "bedrock/deepseek.v3.2" ""

echo "Logs:"
echo "  ${LOG_DIR}/sonnet46_a500_basic_strict_official_20260618.log"
echo "  ${LOG_DIR}/sonnet46_b500_basic_strict_20260618.log"
echo "  ${LOG_DIR}/opencode_qwen3coder_a500_basic_strict_official_20260618.log"
echo "  ${LOG_DIR}/opencode_qwen3coder_b500_basic_strict_20260618.log"
echo "  ${LOG_DIR}/opencode_glm47_a500_basic_strict_official_20260618.log"
echo "  ${LOG_DIR}/opencode_glm47_b500_basic_strict_20260618.log"
echo "  ${LOG_DIR}/openhands_deepseek32_a500_basic_strict_official_20260618.log"
echo "  ${LOG_DIR}/openhands_deepseek32_b500_basic_strict_20260618.log"
