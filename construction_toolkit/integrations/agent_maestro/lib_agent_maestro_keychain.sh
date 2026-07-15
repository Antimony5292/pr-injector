#!/usr/bin/env bash

# Load the Windows Agent Maestro proxy key without printing or persisting it.
ensure_agent_maestro_key() {
  if [[ -n "${AGENT_MAESTRO_API_KEY:-}" ]]; then
    if [[ ${#AGENT_MAESTRO_API_KEY} -eq 64 && "${AGENT_MAESTRO_API_KEY}" != *[^0-9a-f]* ]]; then
      return 0
    fi
    echo "AGENT_MAESTRO_API_KEY is present but invalid" >&2
    return 1
  fi

  local account="${AGENT_MAESTRO_KEYCHAIN_ACCOUNT:-${USER:-}}"
  local service="${AGENT_MAESTRO_KEYCHAIN_SERVICE:-agent-maestro}"
  local key
  key="$(/usr/bin/security find-generic-password -a "${account}" -s "${service}" -w)" || {
    echo "Agent Maestro key is unavailable in macOS Keychain" >&2
    return 1
  }
  if [[ ${#key} -ne 64 || "${key}" == *[^0-9a-f]* ]]; then
    unset key
    echo "Agent Maestro Keychain value is not a 64-character lowercase hex key" >&2
    return 1
  fi
  export AGENT_MAESTRO_API_KEY="${key}"
  unset key
}

check_agent_maestro_connection() {
  local base_url="${AGENT_MAESTRO_BASE_URL:-http://127.0.0.1:23334}"
  curl -fsS --max-time "${AGENT_MAESTRO_HEALTH_TIMEOUT:-15}" \
    -H "x-api-key: ${AGENT_MAESTRO_API_KEY}" \
    "${base_url%/}/api/v1/info" >/dev/null
}
