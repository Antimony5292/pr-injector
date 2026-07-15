from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.inject_swebench_pro import (  # noqa: E402
    _adaptive_l3_candidate_count,
    _should_early_accept_l3_candidate,
    _choose_l3_provider,
    _codex_agent_task_prompt,
    _codex_agent_forbidden_path,
    _codex_agent_safe_preflight_baseline,
    _extract_diff,
)
from construction_toolkit.integrations.agent_maestro.run_codex_headless import (  # noqa: E402
    collect_codex_usage,
    resolve_codex_model,
)


def test_fenced_diff_trims_explanatory_text_inside_block() -> None:
    response = """```diff
diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,2 +1,2 @@
-old = True
+old = False
Actually, the important part is that this remains multi-hunk.
```
"""

    diff = _extract_diff(response)

    assert diff is not None
    assert "Actually" not in diff
    assert "+old = False" in diff


def test_bare_diff_trims_trailing_explanation() -> None:
    response = """diff --git a/pkg/a.py b/pkg/a.py
--- a/pkg/a.py
+++ b/pkg/a.py
@@ -1,2 +1,2 @@
-old = True
+old = False

This is why the patch works.
"""

    diff = _extract_diff(response)

    assert diff is not None
    assert "This is why" not in diff
    assert diff.startswith("diff --git")


def test_codex_agent_provider_aliases_do_not_require_cloud_credentials() -> None:
    assert _choose_l3_provider("codex", "", "") == "codex_agent"
    assert _choose_l3_provider("full-agent", "", "") == "codex_agent"


def test_codex_agent_scope_protects_harness_but_allows_moved_source() -> None:
    assert _codex_agent_forbidden_path("tests/test_behavior.py")
    assert _codex_agent_forbidden_path(".github/workflows/test.yml")
    assert _codex_agent_forbidden_path(".azure-pipelines/azure-pipelines.yml")
    assert _codex_agent_forbidden_path("requirements-dev.txt")
    assert not _codex_agent_forbidden_path("modern/new/module/behavior.py")
    assert not _codex_agent_forbidden_path("openlibrary/templates/books/view.html")


def test_codex_agent_prompt_uses_repo_instead_of_embedding_source() -> None:
    prompt = """## Current Codebase (Latest Version)

### huge.py
very large embedded source

## Target Test Context
target test

Allowed current files: huge.py

Output ONLY the unified diff, starting with "diff --git".
"""
    compact = _codex_agent_task_prompt(prompt)
    assert "very large embedded source" not in compact
    assert "Allowed current files" not in compact
    assert "target test" in compact
    assert "working tree" in compact


def test_codex_agent_baseline_allows_bootstrapped_dirty_submodule_only() -> None:
    submodules = {"vendor/infogami"}
    assert _codex_agent_safe_preflight_baseline(
        " ? vendor/infogami\n", "", submodules
    )
    assert _codex_agent_safe_preflight_baseline(
        " M vendor/infogami\n", "", submodules
    )
    assert not _codex_agent_safe_preflight_baseline(
        " M source.py\n", "", submodules
    )
    assert not _codex_agent_safe_preflight_baseline(
        " M vendor/not-a-submodule\n", "", submodules
    )
    assert not _codex_agent_safe_preflight_baseline("", "diff --git a/source.py b/source.py\n")


def test_codex_usage_is_collected_from_completed_turns() -> None:
    stdout = "\n".join([
        '{"type":"thread.started","thread_id":"x"}',
        '{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":20,"reasoning_output_tokens":5}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
    ])
    assert collect_codex_usage(stdout) == {
        "input_tokens": 110,
        "cached_input_tokens": 80,
        "output_tokens": 22,
        "reasoning_output_tokens": 5,
    }


def test_codex_multi_candidate_ranking_is_reserved_for_large_patches() -> None:
    small = "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    large = (
        "diff --git a/a.py b/a.py\n@@ -1,16 +1,16 @@\n"
        + "".join(f"-old{i}\n+new{i}\n" for i in range(16))
    )
    assert _adaptive_l3_candidate_count("codex_agent", small, 2) == 1
    assert _adaptive_l3_candidate_count("codex_agent", large, 2) == 2
    assert _adaptive_l3_candidate_count("litellm_anthropic", small, 2) == 2


def test_codex_model_is_resolved_from_local_config(tmp_path) -> None:
    (tmp_path / "config.toml").write_text('model = "gpt-test"\n', encoding="utf-8")
    assert resolve_codex_model("", {"CODEX_HOME": str(tmp_path)}) == "gpt-test"
    assert resolve_codex_model("explicit-model", {"CODEX_HOME": str(tmp_path)}) == (
        "explicit-model"
    )


def test_l3_early_accept_requires_a_high_scoring_gate_pass() -> None:
    assert _should_early_accept_l3_candidate(
        0.85, {"v2_fidelity_gate": {"pass_gate": True}}, 1, 2
    )
    assert not _should_early_accept_l3_candidate(
        0.79, {"v2_fidelity_gate": {"pass_gate": True}}, 1, 2
    )
    assert not _should_early_accept_l3_candidate(
        0.90, {"v2_fidelity_gate": {"pass_gate": False}}, 1, 2
    )
    assert not _should_early_accept_l3_candidate(
        0.90, {"v2_fidelity_gate": {"pass_gate": True}}, 2, 2
    )
