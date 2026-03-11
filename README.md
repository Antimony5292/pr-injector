# PR-Injector

**Dynamic Bug Injection via Historical PR Reversion for AI Coding Agent Evaluation**

PR-Injector is an automated defect injection framework designed for evaluating AI coding agents and code LLMs. Unlike traditional benchmarks that rely on stale historical snapshots, PR-Injector takes real historical bug-fix PRs and intelligently re-injects the original defects into the **latest, healthy `main` branch** — producing accurate golden patches and SWE-bench compatible evaluation instances.

## Motivation

Evaluating AI coding agents on real-world bug fixing is hard. Existing approaches have fundamental trade-offs:

| | Static Snapshots (SWE-bench) | LLM Synthesis (SWE-smith) | **PR-Injector** |
|---|---|---|---|
| Runtime Environment | Stale dependencies, broken toolchains | Current codebase | **Current `main` branch** |
| Bug Realism | High (real historical bugs) | Low (random mutations) | **High (real historical bugs)** |
| Golden Patch | Yes | Often missing | **Yes (original PR diff)** |
| Core Challenge | "Dependency hell" | Code that doesn't compile | **Cross-version context drift** |

PR-Injector proposes a third paradigm: **cross-temporal intelligent reversion** — bringing real business-logic defects from historical PRs back into modern code through multi-level fallback strategies.

## Multi-Level Injection Strategy

When reverting a months-old PR onto the latest code, the main challenge is **context drift** — surrounding code has changed since the original fix. PR-Injector handles this with a 4-level fallback:

### Level 1: Clean Git Revert
The original fix context is unchanged. A simple `git revert` applies cleanly, producing a high-fidelity golden patch.

### Level 2: AST Surgery
Surrounding code has drifted (whitespace, renames, new statements), causing git conflicts. PR-Injector uses **tree-sitter** to parse both the current and pre-fix versions of the code, matches functions by name, and performs precise byte-level replacement — immune to line number shifts and formatting changes.

### Level 3: LLM Semantic Injection
The code has been significantly refactored. Physical matching fails entirely, but the core business logic still exists. A reasoning LLM (e.g., GPT-5, Claude) is given the original issue description, the original fix diff, and the current source code, then asked to re-introduce the same logical defect in the current architecture.

### Level 4: Architecture Deprecated
The underlying module has been removed or replaced entirely. PR-Injector detects this via file existence checks and discards the candidate — avoiding generation of meaningless "ghost code" bugs.

## Pipeline Architecture

```
GitHub Repository
       |
       v
+-- Stage 1: Miner --------+    Filter PRs by time decay, test presence,
|   GitHub API -> Candidates |    patch size, and change frequency
+---------------------------+
       |
       v
+-- Stage 2: Reverter ------+    Level 1 (git revert) -> Level 2 (AST surgery)
|   Git + tree-sitter        |    Isolated git worktrees for parallel safety
+---------------------------+
       |
       v
+-- Stage 3: Resolver ------+    Level 4 detection -> Level 3 (LLM injection)
|   File checks + LLM call   |    Only invoked when Level 1 & 2 both fail
+---------------------------+
       |
       v
+-- Stage 4: Verifier ------+    Blast radius control:
|   Test runner + validation  |    Target tests MUST fail, unrelated tests MUST pass
+---------------------------+
       |
       v
   benchmark.jsonl (SWE-bench compatible)
```

## Installation

Requires Python 3.10+ and Git.

```bash
# Clone the repository
git clone https://github.com/xqgao23/pr-injector.git
cd pr-injector

# Install with uv (recommended)
uv pip install -e ".[languages,dev]"

# Or with pip
pip install -e ".[languages,dev]"
```

The `languages` extra installs tree-sitter grammars for Python, JavaScript, TypeScript, Java, Go, and Rust.

## Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Key settings (all prefixed with `PRI_`):

```bash
# Required: GitHub token for API access
PRI_GITHUB_TOKEN=ghp_xxxxxxxxxxxx

# LLM Provider: "azure" or "litellm"
PRI_LLM_PROVIDER=azure

# Azure OpenAI (requires `az login` for Azure AD auth)
PRI_AZURE_ENDPOINT=https://your-resource.cognitiveservices.azure.com/
PRI_AZURE_DEPLOYMENT=your-deployment-name

# Or use litellm for other providers
# PRI_LLM_PROVIDER=litellm
# PRI_LLM_MODEL=claude-sonnet-4-20250514
# PRI_LLM_API_KEY=sk-xxxxxxxxxxxx
```

See [.env.example](.env.example) for all available options.

## Usage

### Single PR Injection

Inject a specific historical PR into the latest codebase:

```bash
# Auto mode: tries Level 1 -> 2 -> 3 with fallback
pr-injector run pallets/flask 5797

# Force a specific strategy
pr-injector run pallets/flask 5799 --strategy llm

# Skip verification (faster, for debugging)
pr-injector run pallets/flask 5797 --no-verify
```

### Batch Mining

Discover and process multiple candidate PRs from a repository:

```bash
pr-injector mine pallets/flask \
  --since 2024-01-01 \
  --require-tests \
  --max-prs 50
```

### Output Format

Results are written to `benchmark_dataset/benchmark.jsonl` in SWE-bench compatible format:

```json
{
  "instance_id": "pallets-flask-pr-5797",
  "repo": "pallets/flask",
  "base_commit": "a3f9b2c1...",
  "problem_statement": "Fix session context push ordering in redirects...",
  "injection_level": "Level_2_AST_Surgery",
  "golden_patch": "diff --git a/src/flask/testing.py b/src/flask/testing.py\n...",
  "test_patch": "",
  "hints_text": "",
  "created_at": "2026-03-11T..."
}
```

## Project Structure

```
pr-injector/
├── src/pr_injector/
│   ├── cli/                # Typer CLI (run, mine commands)
│   ├── pipeline/           # 4-stage funnel pipeline
│   │   ├── miner.py        # Stage 1: PR discovery & filtering
│   │   ├── reverter.py     # Stage 2: Level 1 (git) + Level 2 (AST)
│   │   ├── resolver.py     # Stage 3: Level 4 detection + Level 3 (LLM)
│   │   └── verifier.py     # Stage 4: Blast radius control
│   ├── ast_engine/         # tree-sitter multi-language AST engine
│   ├── llm/                # LLM client (Azure OpenAI / litellm)
│   ├── core/               # Models, config, git ops, diff parsing
│   └── output/             # SWE-bench compatible JSONL writer
├── tests/                  # Unit and integration tests
├── docs/design.md          # Technical design document (Chinese)
├── pyproject.toml          # Project metadata and dependencies
└── .env.example            # Environment variable template
```

## Supported Languages (AST Surgery)

Level 2 AST Surgery supports the following languages via tree-sitter:

- Python
- JavaScript / TypeScript
- Java
- Go
- Rust

## Development

```bash
# Run tests
pytest

# Run with coverage
pytest --cov=pr_injector

# Lint
ruff check src/ tests/

# Type check
mypy src/
```

## License

MIT
