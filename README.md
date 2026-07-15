# PR-Injector

> **Reusable construction release:** Start with [construction_toolkit/README.md](construction_toolkit/README.md). It separates the current bug-transplant and feature-addition case-construction workflows, strict gates, tests, and optional Agent Maestro adapters. Benchmark data and agent-evaluation results are intentionally excluded.

## Compatibility workflows

The reusable construction toolkit is additive. Existing repository-specific and
experiment-compatible entry points remain available under `scripts/` so current
integrations can migrate independently instead of being overwritten by the new
toolkit layout.

In particular, `scripts/experiment_ado.py` remains the stable Azure DevOps/C#
entry point used by ProdBench. The newer bug-transplant implementation lives
under `construction_toolkit/bug_transplant/`; neither workflow imports or
silently falls back to the other. Changes to either contract should be validated
against its own tests and consumers.

PR-Injector is a framework that transplants real historical bug-fix commits onto the latest healthy codebase via a multi-level reversion strategy, producing faithful benchmark instances without per-instance environments.

## Three-Stage Pipeline

Given a repository *R*, a healthy target revision *h*, and a historical bug-fix change *Δ*, PR-Injector constructs a buggy revision *h⁻* through a three-stage funnel:

```
  Input: (Repository R, Healthy Revision h, Historical Fix Δ)
                          │
                          ▼
          ┌───────────────────────────────┐
          │  Stage I: Pre-Screening       │
          │  Deprecation-aware filtering  │
          │  (Level 0)                    │
          └───────────────┬───────────────┘
                          │
                          ▼
          ┌───────────────────────────────┐
          │  Stage II: Multi-Level        │
          │  Historical Reversion         │
          │                               │
          │  Level 1: Exact Git Revert    │
          │       │ (fails on drift)      │
          │       ▼                       │
          │  Level 2: AST Surgery         │
          │       │ (fails on refactor)   │
          │       ▼                       │
          │  Level 3: LLM Revert          │
          └───────────────┬───────────────┘
                          │
                          ▼
          ┌───────────────────────────────┐
          │  Stage III: Behavioral        │
          │  Verification                 │
          │                               │
          │  • Pass-to-fail on target     │
          │    tests                      │
          │  • No-regression on unrelated │
          │    tests                      │
          └───────────────┬───────────────┘
                          │
                          ▼
            benchmark.jsonl (SWE-bench compatible)
```

### Stage I: Deprecation-Aware Pre-Screening

Checks whether the repaired functionality still has an executable counterpart on the target revision. If the relevant source files have been deleted, target tests have disappeared, or the repaired module has been replaced, the candidate is discarded at Level 0.

### Stage II: Multi-Level Historical Reversion

Three recovery levels applied in cascade to handle increasing degrees of cross-version drift:

**Level 1 — Exact Textual Reversion.** A direct `git revert --no-commit` of the historical fix on the target worktree. Succeeds when surrounding code is unchanged since the original fix.

**Level 2 — AST-Guided Structural Reversion.** Uses tree-sitter to parse both the target file and the historical pre-fix file into syntax trees, matches function nodes by symbol identity, and performs bounded body-level replacement. Immune to whitespace changes, import reordering, comment edits, and nearby code insertions.

**Level 3 — LLM Semantic Reversion.** Invokes a reasoning LLM with the current source code, the original fix diff, and optional PR context. The model infers the bug core and synthesizes an equivalent defect on the current architecture. Used only as a last resort.

### Stage III: Behavioral Verification

Every candidate injection must satisfy two conditions:
- **Pass-to-fail**: target tests must fail on the injected revision but pass on the healthy revision.
- **No-regression**: unrelated tests must continue to pass, constraining the blast radius of the injected bug.

## Installation

Requires Python 3.10+ and Git.

```bash
git clone https://github.com/antimony5292/pr-injector.git
cd pr-injector

# Install with uv (recommended)
uv sync

# Or with pip
pip install -e ".[dev]"
```

## Configuration

```bash
cp .env.example .env
```

Key settings (all prefixed with `PRI_`):

```bash
PRI_GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# LLM Provider: "azure" or "litellm"
PRI_LLM_PROVIDER=azure
PRI_AZURE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
PRI_AZURE_DEPLOYMENT=your-deployment-name

# Or use litellm
# PRI_LLM_PROVIDER=litellm
# PRI_LLM_MODEL=claude-sonnet-4-20250514
# PRI_LLM_API_KEY=sk-xxxxxxxxxxxx
```

See [.env.example](.env.example) for all available options.

## Usage

```bash
# Auto mode: Level 1 → Level 2 → Level 3 cascade
pr-injector run pallets/flask 5797

# Force a specific reversion level
pr-injector run pallets/flask 5799 --strategy llm

# Skip behavioral verification
pr-injector run pallets/flask 5797 --no-verify

# Batch processing
pr-injector mine pallets/flask --since 2024-01-01 --max-candidates 50
```

### Output Format

Results are written to `benchmark_dataset/benchmark.jsonl` in SWE-bench compatible format:

```json
{
  "instance_id": "pallets-flask-pr-5797",
  "repo": "pallets/flask",
  "base_commit": "a3f9b2c1...",
  "problem_statement": "Fix session context push ordering...",
  "injection_level": "Level_2_AST_Surgery",
  "golden_patch": "diff --git a/src/flask/testing.py ...",
  "test_patch": "",
  "created_at": "2026-03-11T..."
}
```

## Project Structure

```
pr-injector/
├── src/pr_injector/
│   ├── cli/                # CLI interface
│   ├── pipeline/
│   │   ├── orchestrator.py # Three-stage pipeline coordination
│   │   ├── reverter.py     # Stage II: Level 1 (git) + Level 2 (AST)
│   │   ├── resolver.py     # Stage II: Level 3 (LLM semantic revert)
│   │   └── verifier.py     # Stage III: Behavioral verification
│   ├── ast_engine/         # tree-sitter AST parsing & surgery
│   ├── llm/                # LLM client (Azure OpenAI / litellm)
│   ├── core/               # Models, config, git ops, diff parsing
│   └── output/             # SWE-bench compatible JSONL serialization
├── tests/
└── pyproject.toml
```

## Development

```bash
pytest                        # Run tests
pytest --cov=pr_injector      # Run with coverage
ruff check src/ tests/        # Lint
mypy src/                     # Type check
```

## Trademarks 

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft trademarks or logos is subject to and must follow Microsoft’s Trademark & Brand Guidelines. Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship. Any use of third-party trademarks or logos are subject to those third-party’s policies.

## License

MIT
