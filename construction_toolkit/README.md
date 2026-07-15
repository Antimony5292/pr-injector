# PR-INJECTOR Construction Toolkit

This directory is the reusable, construction-only release of PR-INJECTOR. It contains no benchmark instances, experiment results, evaluation trajectories, repository worktrees, or credentials.

## Workflows

| Workflow | Input | Constructed task | Strict acceptance condition |
| --- | --- | --- | --- |
| [Bug transplantation](bug_transplant/README.md) | Historical bug-fix task plus a healthy modern revision of the same project | Modern revision with an equivalent historical bug reintroduced | Healthy target tests pass; injected target tests fail; adjacent/P2P tests remain safe; the golden repair restores both; complexity/fidelity remains comparable to the source task |
| [Feature addition](feature_addition/README.md) | Historical feature PR/task plus an executable modern revision | Modern `feature-missing` revision and a gold feature-restore patch | Feature tests pass before removal, fail after removal, and pass after restoration; P2P tests remain safe; source/modern feature intent passes the fidelity gate |

Both workflows implement the same high-level contract:

```text
historical change + target revision
    -> source/target preflight
    -> deterministic transformation (when possible)
    -> semantic agent transformation (only when needed)
    -> complexity and intent fidelity gates
    -> target behavior gate
    -> adjacent/P2P regression gate
    -> gold restoration gate
    -> auditable task artifact
```

The task-specific transformation and tests differ. Bug transplantation uses the L1/L2/L3 reversion cascade. Feature addition performs semantic feature removal and feature-intent pairing; it does not pretend that bug-specific L1/L2/L3 labels apply unchanged.

## Installation

```bash
git clone https://github.com/Antimony5292/pr-injector.git
cd pr-injector
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
export PYTHONPATH="$PWD/src:$PWD"
```

Python 3.10+, Git, and an executable test environment for every target repository are required. Docker is optional but recommended when repository dependencies conflict.

## Model and agent integration

L1/L2 and all strict gates are deterministic. A model is used only for semantic reconstruction/removal or explicit rescue lanes. The included [Agent Maestro integration](integrations/agent_maestro/README.md) supports:

- Anthropic-compatible raw requests for L3 bug reconstruction;
- OpenAI Responses-compatible coding agents;
- Claude Code and Codex headless runners;
- environment-variable or local secret-store injection without committed keys.

Agent Maestro is optional. The bug workflow also retains provider hooks for LiteLLM/Azure-compatible deployments. Never commit a real API key, GitHub token, Copilot token, VS Code secret, or model response containing private source code.

## Repository layout

```text
construction_toolkit/
  bug_transplant/        # B-style bug task construction
  feature_addition/      # feature-missing task construction
  integrations/          # optional model/agent adapters
  tests/                 # construction and gate tests only
src/pr_injector/         # shared AST, patch, pipeline, and output library
```

Generated artifacts should be written under `.pri-workspace/`, `artifacts/`, or another ignored directory. They are intentionally absent from this repository.

## Validation

```bash
pytest -q construction_toolkit/tests/bug_transplant
pytest -q construction_toolkit/tests/feature_addition
ruff check --select E9,F63,F7,F82 src construction_toolkit
```

Run the workflow-specific `--help` commands before a large batch. Start with a small candidate set, inspect all rejection buckets, and only then increase workers.
