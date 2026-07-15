# Agent Maestro Integration

PR-INJECTOR does not require a model for deterministic transformations or verification. This adapter is for L3 semantic bug reconstruction, semantic feature removal, and explicit rescue lanes.

## Endpoint contract

Set the endpoint exposed by your own Agent Maestro deployment:

```bash
export AGENT_MAESTRO_BASE_URL='http://127.0.0.1:23333'
export AGENT_MAESTRO_API_KEY="$(your-approved-secret-store-command)"
```

Expected routes:

- Anthropic-compatible: `$AGENT_MAESTRO_BASE_URL/api/anthropic/v1/messages`
- OpenAI-compatible: `$AGENT_MAESTRO_BASE_URL/api/openai/v1`
- Health/catalog: `$AGENT_MAESTRO_BASE_URL/api/v1/info`

Do not copy GitHub/Copilot OAuth tokens out of VS Code. Agent Maestro's local proxy key must be generated and distributed through an approved secret store, never committed to this repository.

## Included runners

- `run_codex_headless.py`: Codex CLI with an OpenAI Responses-compatible provider profile.
- `run_claude_code_headless.py`: Claude Code pointed at an Anthropic-compatible endpoint.
- `run_copilot_headless.py`: compatibility runner for the Copilot/Agent Maestro path.
- `lib_agent_maestro_keychain.sh`: optional macOS Keychain loader; service/account names are configurable.
- `agent-maestro.config.example.toml`: example Codex provider profile that reads the key from the environment.

Example health check:

```bash
curl -fsS \
  -H "x-api-key: $AGENT_MAESTRO_API_KEY" \
  "$AGENT_MAESTRO_BASE_URL/api/v1/info"
```

Example Claude Code environment:

```bash
export ANTHROPIC_BASE_URL="$AGENT_MAESTRO_BASE_URL/api/anthropic"
export ANTHROPIC_API_KEY="$AGENT_MAESTRO_API_KEY"
export ANTHROPIC_AUTH_TOKEN="$AGENT_MAESTRO_API_KEY"
export ANTHROPIC_MODEL='claude-sonnet-5[1m]'
unset CLAUDE_CODE_USE_BEDROCK AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION
```

Model identifiers are deployment-specific. Query the catalog instead of silently substituting another model. Log the requested model, resolved model, endpoint type, retry count, latency, and usage metadata, but never log the key.
