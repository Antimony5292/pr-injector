# AWS Bedrock Agent Guidebook

This note captures the shortest correct way to use this AWS Bedrock account for two coding-agent setups:

1. `Claude Code` over AWS Bedrock with `Claude Sonnet 4.6`
2. `OpenHands` over AWS Bedrock with `DeepSeek V3.2`

## 1. Authentication model

Bedrock does **not** use an Anthropic or DeepSeek API key here.
It uses normal AWS credentials through an AWS profile or role.

Expected AWS account:

- `497589205881`

Recommended local profile:

- `AWS_PROFILE=default`

Recommended region:

- `us-west-2`

Quick verification:

```bash
aws sts get-caller-identity --profile default
```

The returned `Account` should be:

```text
497589205881
```

## 2. Shared environment setup

Use these defaults for both agent paths:

```bash
export AWS_PROFILE=default
export AWS_REGION=us-west-2
export AWS_DEFAULT_REGION=us-west-2
```

Also clear direct model-provider API keys so the run cannot silently fall back away from Bedrock:

```bash
unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY MOONSHOT_API_KEY \
  MINIMAX_API_KEY OPENROUTER_API_KEY CODEX_API_KEY GOOGLE_API_KEY
```

## 3. Claude Code on Bedrock with Claude Sonnet 4.6

### Bedrock model setting

For Claude Code in this project, Bedrock is activated through:

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ANTHROPIC_MODEL='arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6'
```

Important detail:

- `ANTHROPIC_MODEL` is carrying the **Bedrock inference profile ARN**
- it is **not** a direct Anthropic API model name

### Minimal direct usage

After the environment is set, use Claude Code normally:

```bash
claude
```

Or for a one-shot prompt:

```bash
claude -p "Reply with exactly OK and nothing else."
```

### Project runner usage

This repository already has a Bedrock-aware Claude wrapper. The relevant chain is:

- `BenchInject-file/scripts/run_claude_inject_bedrock.py`
- `BenchInject-file/scripts/run_claude_headless.py`

Minimal headless example:

```bash
python3 /Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_claude_inject_bedrock.py \
  --repo /path/to/repo \
  --system /path/to/system.txt \
  --task /path/to/task.txt \
  --out /path/to/out.json \
  --aws-region us-west-2 \
  --aws-profile default \
  --bedrock-model-id 'arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6'
```

### What the local wrapper does

The project wrapper sets:

- `CLAUDE_CODE_USE_BEDROCK=1`
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
- `AWS_REGION`
- `AWS_DEFAULT_REGION`
- `AWS_PROFILE`
- `ANTHROPIC_MODEL`

and then calls the local Claude headless runner.

## 4. OpenHands on Bedrock with DeepSeek V3.2

### Bedrock model setting

For OpenHands in this project, the model string should be:

```bash
bedrock/deepseek.v3.2
```

### Readiness check

Use the existing check script:

```bash
bash /Users/harmin/Desktop/BenchInject/BenchInject-file/tools/check_openhands_bedrock_ready.sh bedrock/deepseek.v3.2
```

This validates:

- AWS identity
- Bedrock model visibility
- OpenHands Python environment
- LiteLLM-to-Bedrock path

### Project runner usage

The repository wrapper is:

- `BenchInject-file/scripts/run_openhands_headless.py`

Minimal example:

```bash
python3 /Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_openhands_headless.py \
  --repo /path/to/repo \
  --system /path/to/system.txt \
  --task /path/to/task.txt \
  --out /path/to/out.json \
  --model bedrock/deepseek.v3.2 \
  --aws-region us-west-2 \
  --aws-profile default
```

### Relevant OpenHands defaults in this repository

The current local wrapper uses:

- `temperature = 0`
- `max_output_tokens = 4096`
- `max_iterations = 50`
- runtime default: `cli`

Those are the effective project defaults unless explicitly overridden.

## 5. Which value to use where

### Claude Code + Bedrock

- auth path: AWS credentials/profile
- env switch: `CLAUDE_CODE_USE_BEDROCK=1`
- model variable: `ANTHROPIC_MODEL`
- model value: Bedrock inference profile ARN for Sonnet 4.6

### OpenHands + Bedrock

- auth path: AWS credentials/profile
- model argument: `--model`
- model value: `bedrock/deepseek.v3.2`

## 6. Common failure modes

### Wrong auth path

If a run is trying to use a direct provider API key, clean those env vars first and use AWS profile auth only.

### Wrong Claude model value

For Claude Code in this setup, do not pass a direct Anthropic model slug.
Use the Bedrock inference profile ARN in `ANTHROPIC_MODEL`.

### Wrong OpenHands model prefix

For OpenHands in this setup, use:

```text
bedrock/deepseek.v3.2
```

not a direct provider-only name.

### Wrong region

Use:

```text
us-west-2
```

unless the environment is deliberately reconfigured.

## 7. Short copy-paste versions

### Claude Code Sonnet 4.6 over Bedrock

```bash
export AWS_PROFILE=default
export AWS_REGION=us-west-2
export AWS_DEFAULT_REGION=us-west-2
unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY MOONSHOT_API_KEY \
  MINIMAX_API_KEY OPENROUTER_API_KEY CODEX_API_KEY GOOGLE_API_KEY
export CLAUDE_CODE_USE_BEDROCK=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export ANTHROPIC_MODEL='arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6'
claude
```

### OpenHands DeepSeek V3.2 over Bedrock

```bash
export AWS_PROFILE=default
export AWS_REGION=us-west-2
export AWS_DEFAULT_REGION=us-west-2
unset ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY MOONSHOT_API_KEY \
  MINIMAX_API_KEY OPENROUTER_API_KEY CODEX_API_KEY GOOGLE_API_KEY
bash /Users/harmin/Desktop/BenchInject/BenchInject-file/tools/check_openhands_bedrock_ready.sh bedrock/deepseek.v3.2
python3 /Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_openhands_headless.py \
  --repo /path/to/repo \
  --system /path/to/system.txt \
  --task /path/to/task.txt \
  --out /path/to/out.json \
  --model bedrock/deepseek.v3.2 \
  --aws-region us-west-2 \
  --aws-profile default
```

## 8. File references used to build this guide

- `/Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_claude_inject_bedrock.py`
- `/Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_claude_headless.py`
- `/Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_openhands_headless.py`
- `/Users/harmin/Desktop/BenchInject/BenchInject-file/tools/check_openhands_bedrock_ready.sh`
