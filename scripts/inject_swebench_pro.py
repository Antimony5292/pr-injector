"""Batch injection experiment for SWE-bench Pro instances.

For each instance in the sampled JSONL:
  1. Clone/update the repo, checkout latest main (healthy revision h)
  2. Attempt Level 1: reverse-apply the patch via `git apply -R`
  3. If L1 fails, attempt Level 2: AST surgery (match functions from base_commit)
  4. Record results to output JSONL

Level 3 LLM semantic injection is present only as an explicit opt-in fallback.
The default experiment path is L1/L2-only and does not call any model.

Verification is handled separately by verify_swebench_pro.py.

Usage:
    uv run scripts/inject_swebench_pro.py [--input FILE] [--output FILE]
                                           [--timeout 300]
                                           [--filter INSTANCE_ID]
                                           [--max N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from pr_injector.ast_engine.hunk_surgeon import (
    old_changed_line_ranges_for_file,
    overlaps_any_range,
    reverse_patch_hunks_for_file,
)
from pr_injector.core.compatibility import check_source_compatibility, reports_to_dicts

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


class _Tee:
    """Write to multiple streams simultaneously."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


# ── Helpers ──────────────────────────────────────────────────────────────────

PYTHON = sys.executable


def _stable_id_suffix(*parts: str, length: int = 12) -> str:
    data = "::".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha1(data).hexdigest()[:length]


TEST_FILE_PATTERNS = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_"),
    re.compile(r"(^|/)test\."),
    re.compile(r"_test\.py$"),
]


@dataclass(frozen=True)
class FunctionSource:
    source: str
    start_line: int
    end_line: int


def is_test_file(path: str) -> bool:
    return any(p.search(path.lower()) for p in TEST_FILE_PATTERNS)


def git(*args: str, cwd: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, timeout=timeout, text=False,
    )


def git_text(*args: str, cwd: str, timeout: int = 600) -> str:
    r = git(*args, cwd=cwd, timeout=timeout)
    return r.stdout.decode(errors="replace")


def ensure_repo_head(repo_dir: Path, default_branch: str) -> None:
    """Repair local clone HEAD when it points at a stale or invalid branch."""
    head = git("rev-parse", "--verify", "HEAD", cwd=str(repo_dir), timeout=60)
    if head.returncode == 0:
        return
    remote_ref = f"origin/{default_branch}"
    resolved = git_text("rev-parse", remote_ref, cwd=str(repo_dir), timeout=60).strip()
    if not resolved:
        return
    local_ref = f"refs/heads/{default_branch}"
    git("update-ref", local_ref, resolved, cwd=str(repo_dir), timeout=60)
    git("symbolic-ref", "HEAD", local_ref, cwd=str(repo_dir), timeout=60)


def cleanup_worktree_branch(repo_dir: Path, branch: str, wt_path: Path | None = None) -> None:
    """Remove stale worktree/branch state left by interrupted injection runs."""
    git("worktree", "prune", cwd=str(repo_dir), timeout=120)
    if wt_path and wt_path.exists():
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir), timeout=120)
        shutil.rmtree(wt_path, ignore_errors=True)

    delete = git("branch", "-D", branch, cwd=str(repo_dir), timeout=120)
    if delete.returncode == 0:
        return

    # A branch can survive if Git still believes it is checked out in a stale
    # worktree. Remove that worktree by reading the porcelain metadata.
    listing = git_text("worktree", "list", "--porcelain", cwd=str(repo_dir), timeout=120)
    current_path: str | None = None
    branch_ref = f"refs/heads/{branch}"
    for line in listing.splitlines() + [""]:
        if line.startswith("worktree "):
            current_path = line.removeprefix("worktree ").strip()
        elif line == f"branch {branch_ref}" and current_path:
            git("worktree", "remove", "--force", current_path, cwd=str(repo_dir), timeout=120)
            shutil.rmtree(current_path, ignore_errors=True)
            current_path = None
        elif not line:
            current_path = None

    git("worktree", "prune", cwd=str(repo_dir), timeout=120)
    git("branch", "-D", branch, cwd=str(repo_dir), timeout=120)


def extract_source_files_from_patch(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            path = m.group(2)
            if not is_test_file(path):
                files.append(path)
    return list(dict.fromkeys(files))  # deduplicate, preserve order


def extract_test_files_from_patch(patch: str) -> list[str]:
    files = []
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            path = m.group(2)
            if is_test_file(path):
                files.append(path)
    return list(dict.fromkeys(files))


def resolve_current_source_file(worktree: str | Path, filepath: str) -> str | None:
    """Resolve a historical source path to the current checkout.

    The common safe drift pattern is a project moving from flat package layout
    to ``src/`` layout, for example ``requests/models.py`` becoming
    ``src/requests/models.py``. Keep this deliberately conservative; broader
    semantic file discovery belongs in Level 3 prompt context, not in automatic
    patch application.
    """

    wt = Path(worktree)
    candidates = [filepath, str(Path("src") / filepath)]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        path = wt / candidate
        if path.exists() and path.is_file():
            return candidate.replace("\\", "/")
    return None


def coerce_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [value]
    return []


def reverse_patch(patch: str) -> str:
    """Reverse a unified diff."""
    lines = patch.split("\n")
    result = []
    for line in lines:
        if line.startswith("new file mode"):
            result.append(line.replace("new file mode", "deleted file mode"))
        elif line.startswith("deleted file mode"):
            result.append(line.replace("deleted file mode", "new file mode"))
        elif line.startswith("--- a/"):
            result.append(line.replace("--- a/", "+++ b/").replace("+++ b/", "+++ b/", 1))
        elif line.startswith("+++ b/"):
            result.append(line.replace("+++ b/", "--- a/").replace("--- a/", "--- a/", 1))
        elif line.startswith("--- /dev/null"):
            result.append("+++ /dev/null")
        elif line.startswith("+++ /dev/null"):
            result.append("--- /dev/null")
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+(?:,\d+)?) \+(\d+(?:,\d+)?) @@(.*)", line)
            if m:
                result.append(f"@@ -{m.group(2)} +{m.group(1)} @@{m.group(3)}")
            else:
                result.append(line)
        elif line.startswith("+") and not line.startswith("+++"):
            result.append("-" + line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            result.append("+" + line[1:])
        else:
            result.append(line)

    # Reorder: - before + in each group
    ordered = []
    plus_buf, minus_buf = [], []

    def flush():
        ordered.extend(minus_buf)
        ordered.extend(plus_buf)
        minus_buf.clear()
        plus_buf.clear()

    for line in result:
        if line.startswith("+") and not line.startswith("+++"):
            plus_buf.append(line)
        elif line.startswith("-") and not line.startswith("---"):
            minus_buf.append(line)
        else:
            flush()
            ordered.append(line)
    flush()
    return "\n".join(ordered)



# ── Level 1: Direct reverse apply ────────────────────────────────────────────

def try_level1(worktree: str, patch: str) -> tuple[bool, str]:
    """Attempt Level 1: reverse-apply the fix patch."""
    # Only reverse source-file parts (not test files, not new files like changelogs)
    reversed_patch = reverse_patch(patch)

    # Try git apply -R (which handles reverse natively)
    proc = subprocess.run(
        ["git", "apply", "--check", "-R"],
        cwd=worktree, input=patch.encode(),
        capture_output=True, timeout=30,
    )
    if proc.returncode == 0:
        # Actually apply
        proc = subprocess.run(
            ["git", "apply", "-R"],
            cwd=worktree, input=patch.encode(),
            capture_output=True, timeout=30,
        )
        if proc.returncode == 0:
            diff = git_text("diff", cwd=worktree)
            return True, diff

    # Fallback: try the manually reversed patch
    proc = subprocess.run(
        ["git", "apply", "--check"],
        cwd=worktree, input=reversed_patch.encode(),
        capture_output=True, timeout=30,
    )
    if proc.returncode == 0:
        proc = subprocess.run(
            ["git", "apply"],
            cwd=worktree, input=reversed_patch.encode(),
            capture_output=True, timeout=30,
        )
        if proc.returncode == 0:
            diff = git_text("diff", cwd=worktree)
            return True, diff

    err = proc.stderr.decode(errors="replace")[:300]
    return False, f"Level 1 failed: {err}"


def run_target_preflight(
    wt_path: Path,
    repo: str,
    target_tests: list[str],
    timeout: int,
    max_target_tests: int | None,
) -> dict:
    """Verify target tests are usable on modern healthy HEAD before injection."""
    try:
        from verify_swebench_pro import (
            _collectable_tests,
            _create_venv,
            _filter_passing_tests,
            _existing_nodeids,
            _install_project,
            _read_requires_python,
            _test_runner_available,
            run_repo_tests,
        )
    except Exception as exc:
        return {
            "ok": False,
            "reason": "preflight_import_failed",
            "error": str(exc)[:300],
        }

    existing_tests = _existing_nodeids(wt_path, target_tests, repo)
    if not existing_tests:
        return {"ok": False, "reason": "test_files_missing"}

    print("  [preflight] Installing deps and checking target tests on healthy HEAD...")
    venv_python = _create_venv(str(wt_path), repo)
    if not venv_python:
        return {
            "ok": False,
            "reason": "python_version_unavailable",
            "requires_python": _read_requires_python(wt_path),
        }
    _install_project(str(wt_path), repo, timeout, python=venv_python, test_files=existing_tests)
    if not _test_runner_available(str(wt_path), repo, venv_python):
        return {"ok": False, "reason": "test_runner_unavailable"}
    collectable = _collectable_tests(str(wt_path), repo, existing_tests, venv_python)
    if not collectable:
        return {"ok": False, "reason": "target_nodeids_not_collectable"}
    original_collectable_count = len(collectable)
    minimized = False
    if max_target_tests is not None and len(collectable) > max_target_tests:
        candidate_limit = int(
            os.environ.get(
                "PRI_PREFLIGHT_TARGET_MINIMIZE_CANDIDATES",
                str(max(max_target_tests * 4, max_target_tests)),
            )
        )
        target_candidates = collectable[:candidate_limit]
        print(
            "  [preflight] Minimizing target tests "
            f"({len(collectable)} > {max_target_tests}); "
            f"checking first {len(target_candidates)} candidates..."
        )
        passing, failing = _filter_passing_tests(
            str(wt_path), repo, target_candidates, venv_python, timeout
        )
        if not passing:
            return {
                "ok": False,
                "reason": "healthy_target_failed",
                "collectable_target_tests": collectable,
                "healthy_failed_tests": failing,
                "minimized": False,
                "minimize_candidate_limit": candidate_limit,
            }
        collectable = passing[:max_target_tests]
        minimized = True
    healthy_result = run_repo_tests(
        str(wt_path), repo, collectable, timeout=timeout, python=venv_python
    )
    healthy_executed = int(healthy_result.get("total") or 0) > 0
    healthy_minimized_from_failure = False
    healthy_failed_tests: list[str] = []
    if healthy_result["returncode"] != 0 and healthy_executed and len(collectable) > 1:
        print(
            "  [preflight] Target group failed on healthy HEAD; "
            "minimizing to individually passing target tests..."
        )
        passing, failing = _filter_passing_tests(
            str(wt_path), repo, collectable, venv_python, timeout
        )
        healthy_failed_tests = failing
        if passing:
            collectable = passing[:max_target_tests] if max_target_tests is not None else passing
            healthy_result = run_repo_tests(
                str(wt_path), repo, collectable, timeout=timeout, python=venv_python
            )
            healthy_executed = int(healthy_result.get("total") or 0) > 0
            healthy_minimized_from_failure = True
    if healthy_result["returncode"] != 0 or not healthy_executed:
        return {
            "ok": False,
            "reason": "healthy_target_failed" if healthy_executed else "healthy_target_not_executed",
            "collectable_target_tests": collectable,
            "healthy_result": healthy_result,
            "healthy_failed_tests": healthy_failed_tests,
        }
    return {
        "ok": True,
        "reason": "healthy_target_passed",
        "collectable_target_tests": collectable,
        "original_collectable_target_test_count": original_collectable_count,
        "minimized": minimized,
        "healthy_minimized_from_failure": healthy_minimized_from_failure,
        "healthy_failed_tests": healthy_failed_tests,
        "minimize_candidate_limit": (
            int(os.environ.get(
                "PRI_PREFLIGHT_TARGET_MINIMIZE_CANDIDATES",
                str(max(max_target_tests * 4, max_target_tests)),
            ))
            if max_target_tests is not None and minimized
            else None
        ),
        "max_target_tests": max_target_tests,
        "healthy_result": healthy_result,
    }


# ── Level 2: AST surgery ─────────────────────────────────────────────────────

def try_level2(
    worktree: str,
    repo_dir: str,
    base_commit: str,
    patch: str,
) -> tuple[bool, str, dict]:
    """Attempt Level 2: AST-guided structural reversion.

    For each source file in the patch, get the pre-fix version from base_commit,
    parse both with AST, match functions, and replace bodies.
    """
    import ast as pyast

    source_files = extract_source_files_from_patch(patch)
    if not source_files:
        return False, "No source files in patch", {"compatibility_reports": []}

    any_changed = False
    compatibility_reports = []
    flagged_files = []
    rejected_files = []
    hunk_replacements = []
    function_replacements = []
    skipped_whole_function_replacements = []
    level2_mode = os.environ.get("PRI_LEVEL2_MODE", "original_ast").lower()
    use_hunk_first = level2_mode in {"hunk_first", "conservative_hunk_first"}
    allow_whole_function = os.environ.get("PRI_ALLOW_WHOLE_FUNCTION_LEVEL2", "1").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    reject_on_compatibility = os.environ.get("PRI_REJECT_COMPATIBILITY", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    for filepath in source_files:
        current_filepath = resolve_current_source_file(worktree, filepath)
        if not current_filepath:
            continue
        target_path = Path(worktree) / current_filepath
        if not target_path.exists():
            continue

        # Get pre-fix version from base_commit
        try:
            prefix_content = git_text(
                "show", f"{base_commit}:{filepath}", cwd=repo_dir, timeout=30
            )
        except Exception:
            continue

        if not prefix_content.strip():
            continue

        current_content = target_path.read_text(encoding="utf-8", errors="replace")

        replaced = False
        if use_hunk_first:
            hunk_result = reverse_patch_hunks_for_file(filepath, current_content, patch)
            if hunk_result.changed:
                new_content = hunk_result.content
                hunk_replacements.extend(hunk_result.to_metadata())
                replaced = True

        if not replaced:
            if not allow_whole_function:
                skipped_whole_function_replacements.append(current_filepath)
                continue
            # Parse both versions
            try:
                prefix_tree = pyast.parse(prefix_content)
                current_tree = pyast.parse(current_content)
            except SyntaxError:
                continue

            # Extract top-level functions/methods
            prefix_funcs = _extract_function_sources(prefix_content, prefix_tree)
            changed_ranges = old_changed_line_ranges_for_file(filepath, patch)
            if changed_ranges:
                prefix_funcs = {
                    name: func
                    for name, func in prefix_funcs.items()
                    if overlaps_any_range(func.start_line, func.end_line, changed_ranges)
                }
            current_funcs = _extract_function_sources(current_content, current_tree)

            # Find functions that exist in both but differ
            new_content = current_content
            replaced = False
            replaced_current_sources: set[str] = set()
            for name, prefix_func in prefix_funcs.items():
                current_func = current_funcs.get(name)
                if (
                    current_func
                    and current_func.source != prefix_func.source
                    and current_func.source not in replaced_current_sources
                ):
                    new_content = new_content.replace(current_func.source, prefix_func.source, 1)
                    replaced_current_sources.add(current_func.source)
                    function_replacements.append({"file_path": current_filepath, "source_file_path": filepath, "symbol": name})
                    replaced = True

        if replaced:
            # Validate syntax
            try:
                pyast.parse(new_content)
            except SyntaxError:
                continue
            compatibility = check_source_compatibility(
                current_filepath, current_content, new_content
            )
            compatibility_reports.append(compatibility)
            if compatibility.checked and not compatibility.passed:
                flagged_files.append(current_filepath)
                if reject_on_compatibility:
                    rejected_files.append(current_filepath)
                    continue
            target_path.write_text(new_content, encoding="utf-8")
            any_changed = True

    if rejected_files:
        return False, "Level 2: compatibility rejection", {
                "compatibility_reports": reports_to_dicts(compatibility_reports),
                "compatibility_flagged_files": flagged_files,
                "compatibility_rejected_files": rejected_files,
                "hunk_replacements": hunk_replacements,
                "function_replacements": function_replacements,
                "skipped_whole_function_replacements": skipped_whole_function_replacements,
                "whole_function_level2_enabled": allow_whole_function,
                "level2_mode": level2_mode,
                "hunk_first_enabled": use_hunk_first,
                "reject_on_compatibility": reject_on_compatibility,
            }

    if any_changed:
        diff = git_text("diff", cwd=worktree)
        if diff.strip():
            return True, diff, {
                "compatibility_reports": reports_to_dicts(compatibility_reports),
                "compatibility_flagged_files": flagged_files,
                "compatibility_rejected_files": rejected_files,
                "hunk_replacements": hunk_replacements,
                "function_replacements": function_replacements,
                "skipped_whole_function_replacements": skipped_whole_function_replacements,
                "whole_function_level2_enabled": allow_whole_function,
                "level2_mode": level2_mode,
                "hunk_first_enabled": use_hunk_first,
                "reject_on_compatibility": reject_on_compatibility,
            }

    return False, "Level 2: no functions matched or replaced", {
        "compatibility_reports": reports_to_dicts(compatibility_reports),
        "compatibility_flagged_files": flagged_files,
        "compatibility_rejected_files": rejected_files,
        "hunk_replacements": hunk_replacements,
        "function_replacements": function_replacements,
        "skipped_whole_function_replacements": skipped_whole_function_replacements,
        "whole_function_level2_enabled": allow_whole_function,
        "level2_mode": level2_mode,
        "hunk_first_enabled": use_hunk_first,
        "reject_on_compatibility": reject_on_compatibility,
    }


def _extract_functions(source: str, tree) -> dict[str, str]:
    """Extract function/method source text keyed by qualified name."""
    return {
        name: function.source
        for name, function in _extract_function_sources(source, tree).items()
    }


def _extract_function_sources(source: str, tree) -> dict[str, FunctionSource]:
    """Extract function/method source text keyed by qualified name."""
    import ast as pyast
    lines = source.splitlines(keepends=True)

    class FunctionCollector(pyast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []
            self.funcs: dict[str, FunctionSource] = {}

        def visit_ClassDef(self, node):  # noqa: N802
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node):  # noqa: N802
            self._record(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node):  # noqa: N802
            self._record(node)
            self.generic_visit(node)

        def _record(self, node) -> None:
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
            qualname = ".".join([*self.class_stack, node.name])
            self.funcs[qualname] = FunctionSource(
                source="".join(lines[start:end]),
                start_line=node.lineno,
                end_line=end,
            )

    collector = FunctionCollector()
    collector.visit(tree)
    funcs = collector.funcs

    # Backward-compatible fallback for unique bare names only. This keeps older
    # single-function cases working without allowing ambiguous method overwrites.
    bare_counts: dict[str, int] = {}
    for qualname in funcs:
        bare = qualname.rsplit(".", 1)[-1]
        bare_counts[bare] = bare_counts.get(bare, 0) + 1
    for qualname, func_src in list(funcs.items()):
        bare = qualname.rsplit(".", 1)[-1]
        if bare_counts[bare] == 1:
            funcs.setdefault(bare, func_src)

    return funcs


# ── Level 3: LLM Semantic Injection ──────────────────────────────────────────

def try_level3(
    worktree: str,
    repo_dir: str,
    base_commit: str,
    patch: str,
    problem_statement: str,
) -> tuple[bool, str, dict]:
    """Attempt Level 3: LLM semantic reversion.

    Calls Azure OpenAI to semantically re-introduce the bug when both
    textual and structural matching have failed.

    Returns (success, diff_or_error, metadata).
    """
    if os.environ.get("PRI_ALLOW_L3_MODEL_CALLS", "").lower() not in {"1", "true", "yes", "on"}:
        return False, "Level 3 disabled: PRI_ALLOW_L3_MODEL_CALLS is not enabled", {
            "disabled": True,
            "reason": "l3_model_calls_disabled",
        }

    # Load config from .env
    import dotenv
    dotenv.load_dotenv()

    endpoint = os.environ.get("PRI_AZURE_ENDPOINT", "")
    deployment = os.environ.get("PRI_AZURE_DEPLOYMENT", "gpt-5")
    api_version = os.environ.get("PRI_AZURE_API_VERSION", "2024-12-01-preview")
    bedrock_model = os.environ.get("PRI_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-6")
    bedrock_region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-west-2"))
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    litellm_model = os.environ.get("PRI_L3_MODEL", "anthropic/claude-sonnet-4-20250514")

    # Collect current source files touched by the patch
    source_files = extract_source_files_from_patch(patch)
    current_files: dict[str, str] = {}
    for filepath in source_files:
        current_filepath = resolve_current_source_file(worktree, filepath)
        if not current_filepath:
            continue
        target_path = Path(worktree) / current_filepath
        if target_path.exists():
            content = target_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > 15000:
                content = content[:15000] + "\n... (truncated)"
            current_files[current_filepath] = content

    # If no source files exist on HEAD, check if the logic migrated elsewhere
    if not current_files:
        # Try to find related files by looking at the module path
        for filepath in source_files:
            parent = str(Path(filepath).parent)
            if parent and parent != ".":
                wt_parent = Path(worktree) / parent
                if wt_parent.exists():
                    for py_file in wt_parent.rglob("*.py"):
                        rel = str(py_file.relative_to(worktree)).replace("\\", "/")
                        content = py_file.read_text(encoding="utf-8", errors="replace")
                        if len(content) > 10000:
                            content = content[:10000] + "\n... (truncated)"
                        current_files[rel] = content
                        if len(current_files) >= 5:
                            break

    if not current_files:
        return False, "Level 3: no source files exist on HEAD (architecture deprecated)", {
            "l3_reason": "all_files_deleted",
        }

    # Build prompt
    files_section = ""
    for path, content in current_files.items():
        files_section += f"### {path}\n```python\n{content}\n```\n\n"

    system_prompt = (
        "You are an expert software engineer tasked with recreating a historical bug in a modern"
        " codebase. Your goal is to precisely reintroduce the same logical defect that was originally"
        " fixed, adapted to the current code structure.\n\nRules:\n1. Only modify the specific"
        " functions/methods that correspond to the original bug fix.\n2. The bug must be a LOGICAL"
        " defect, not a syntax error.\n3. Output ONLY a valid unified diff (no explanations, no"
        " markdown code fences).\n4. The diff must apply cleanly to the current source files.\n5. Do NOT introduce"
        " any new imports or dependencies.\n6. Preserve all existing functionality EXCEPT for the"
        " specific bug being reintroduced.\n7. Start your response with 'diff --git'.\n"
        "8. Modify ONLY files shown in the Current Codebase section. Do not create diffs for"
        " historical files that are absent from the current codebase."
    )

    user_prompt = f"""## Original Bug Context

### Issue Description
{problem_statement[:3000] if problem_statement else "(No description available)"}

### Original Fix (PR Diff)
```diff
{patch[:8000]}
```

## Current Codebase (Latest Version)

{files_section}

Allowed current files: {", ".join(current_files.keys())}

## Task

The original PR fixed a bug described above. The codebase has since evolved.
Your task: Create a unified diff that reintroduces the SAME logical bug into the CURRENT code.

The bug should:
- Cause the same category of failure as the original
- Be in the equivalent code location (which may have moved or been refactored)
- Be subtle enough that it's not immediately obvious

Output ONLY the unified diff, starting with "diff --git"."""

    provider = (
        "azure" if endpoint
        else "bedrock" if _bedrock_available()
        else "litellm_anthropic" if anthropic_key
        else "none"
    )
    model_label = deployment if provider == "azure" else bedrock_model if provider == "bedrock" else litellm_model
    if provider == "none":
        return False, "Level 3: no Azure, Bedrock, or ANTHROPIC_API_KEY configured", {}

    print(f"  [L3] Calling LLM ({provider}:{model_label})...")
    print(f"       Current files available: {list(current_files.keys())}")

    max_attempts = max(1, int(os.environ.get("PRI_L3_APPLY_ATTEMPTS", "2")))
    retry_feedback = ""
    last_error = "Level 3: no attempt made"
    last_meta: dict = {}
    for attempt in range(1, max_attempts + 1):
        attempt_user_prompt = user_prompt
        if retry_feedback:
            attempt_user_prompt += f"""

## Previous diff failed to apply

The previous unified diff was rejected by `git apply --check --recount`:

```text
{retry_feedback[:2000]}
```

Regenerate the diff from the CURRENT code shown above. Keep modern function
signatures and modern imports. Do not use stale line numbers or historical
files. Output ONLY a clean unified diff.
"""
        try:
            if provider == "azure":
                content, meta = _call_azure_l3(
                    endpoint, deployment, api_version, system_prompt, attempt_user_prompt
                )
            elif provider == "bedrock":
                content, meta = _call_bedrock_l3(
                    bedrock_model, bedrock_region, system_prompt, attempt_user_prompt
                )
            else:
                content, meta = _call_litellm_l3(
                    litellm_model, system_prompt, attempt_user_prompt
                )
            meta["attempt"] = attempt
            last_meta = meta

            print(
                f"       Response attempt {attempt}/{max_attempts}: {len(content)} chars, "
                f"finish={meta.get('finish_reason')}, "
                f"tokens={meta.get('prompt_tokens', 0)}+{meta.get('completion_tokens', 0)}"
            )

            if not content.strip():
                last_error = (
                    "Level 3: LLM returned empty response "
                    f"(finish={meta.get('finish_reason')})"
                )
                retry_feedback = last_error
                continue

            # Extract diff from response
            diff = _extract_diff(content)
            if not diff:
                print(f"       Response preview: {content[:300]}")
                last_error = "Level 3: no valid diff in LLM response"
                retry_feedback = last_error
                continue
            diff = _normalize_hunk_headers(diff)
            _write_l3_debug(worktree, meta, content, diff)

            # Validate diff syntax
            if "@@" not in diff:
                last_error = "Level 3: LLM diff missing hunk headers"
                retry_feedback = last_error
                continue
            diff_files = _diff_files(diff)
            invalid_files = sorted(diff_files - set(current_files))
            if invalid_files:
                meta["invalid_files"] = invalid_files
                last_error = f"Level 3: LLM diff touched files outside current set: {invalid_files}"
                retry_feedback = last_error
                continue

            # Try to apply. The second mode only tolerates whitespace drift;
            # it still requires Git to cleanly validate the patch.
            apply_args = ["git", "apply", "--recount"]
            proc = subprocess.run(
                apply_args[:2] + ["--check"] + apply_args[2:],
                cwd=worktree, input=diff.encode(),
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                whitespace_args = ["git", "apply", "--recount", "--ignore-space-change"]
                whitespace_proc = subprocess.run(
                    whitespace_args[:2] + ["--check"] + whitespace_args[2:],
                    cwd=worktree, input=diff.encode(),
                    capture_output=True, timeout=30,
                )
                if whitespace_proc.returncode == 0:
                    apply_args = whitespace_args
                    proc = whitespace_proc
            if proc.returncode != 0:
                err = proc.stderr.decode(errors="replace")[:1000]
                print(f"       Patch check failed: {err[:200]}")
                last_error = f"Level 3: LLM diff doesn't apply cleanly: {err[:300]}"
                retry_feedback = err
                continue

            # Actually apply
            subprocess.run(
                apply_args,
                cwd=worktree, input=diff.encode(),
                capture_output=True, timeout=30,
            )
            applied_diff = git_text("diff", cwd=worktree)
            if applied_diff.strip():
                meta["confidence"] = _estimate_confidence(applied_diff, patch)
                return True, applied_diff, meta

            last_error = "Level 3: patch applied but produced no diff"
            retry_feedback = last_error

        except Exception as e:
            last_error = f"Level 3: LLM call failed: {str(e)[:200]}"
            last_meta = {"error": str(e)[:300], "attempt": attempt}
            retry_feedback = last_error
            continue

    return False, last_error, last_meta


def _extract_diff(response: str) -> str | None:
    """Extract unified diff from LLM response."""
    # Prefer fenced code blocks so trailing explanations are not swallowed by a
    # greedy bare-diff regex.
    m = re.search(r"```(?:diff)?\s*\n(.*?)```", response, re.DOTALL)
    if m:
        block = m.group(1).strip()
        if "diff --git" in block or "@@" in block:
            return block
    # Try bare diff as a fallback.
    m = re.search(r"(diff --git\s+.*)", response, re.DOTALL)
    if m:
        return _trim_bare_diff(m.group(1).strip())
    return None


def _write_l3_debug(worktree: str, meta: dict, response: str, diff: str) -> None:
    debug_root = Path(os.environ.get("PRI_L3_DEBUG_DIR", "experiments/rq2_100/l3_expansion/debug"))
    debug_dir = debug_root / Path(worktree).name
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "response.txt").write_text(response, encoding="utf-8")
        (debug_dir / "extracted.diff").write_text(diff, encoding="utf-8")
        meta["debug_dir"] = str(debug_dir)
    except OSError:
        pass


def _trim_bare_diff(diff: str) -> str:
    lines = diff.splitlines()
    keep: list[str] = []
    in_diff = False
    for line in lines:
        if line.startswith("diff --git "):
            in_diff = True
            keep.append(line)
        elif in_diff and (
            line.startswith(("index ", "--- ", "+++ ", "@@ ", "+", "-", " "))
            or line.startswith(("new file mode", "deleted file mode"))
        ):
            keep.append(line)
        elif in_diff and line.strip() == "":
            keep.append(line)
        elif in_diff:
            break
    return "\n".join(keep).strip()


def _diff_files(diff: str) -> set[str]:
    files: set[str] = set()
    for line in diff.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            files.add(m.group(2))
    return files


def _normalize_hunk_headers(diff: str) -> str:
    lines = diff.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("index "):
            i += 1
            continue
        hunk = re.match(
            r"^@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@(?P<section>.*)$",
            line,
        )
        if not hunk:
            out.append(line)
            i += 1
            continue

        body: list[str] = []
        i += 1
        while i < len(lines) and not lines[i].startswith(("@@ ", "diff --git ")):
            body.append(lines[i])
            i += 1

        old_count = 0
        new_count = 0
        for body_line in body:
            if body_line.startswith("+") and not body_line.startswith("+++"):
                new_count += 1
            elif body_line.startswith("-") and not body_line.startswith("---"):
                old_count += 1
            else:
                old_count += 1
                new_count += 1
        out.append(
            f"@@ -{hunk.group('old_start')},{old_count} "
            f"+{hunk.group('new_start')},{new_count} @@{hunk.group('section')}"
        )
        out.extend(body)
    return "\n".join(out).rstrip() + "\n"


def _call_azure_l3(
    endpoint: str,
    deployment: str,
    api_version: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict]:
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    client = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=4096,
    )
    content = response.choices[0].message.content or ""
    usage = response.usage
    meta = {
        "provider": "azure",
        "model": response.model,
        "finish_reason": response.choices[0].finish_reason,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "response_length": len(content),
    }
    return content, meta


def _call_litellm_l3(
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict]:
    import litellm

    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=float(os.environ.get("PRI_L3_TEMPERATURE", "0.2")),
        max_tokens=int(os.environ.get("PRI_L3_MAX_TOKENS", "4096")),
    )
    content = response.choices[0].message.content or ""
    usage = response.usage
    meta = {
        "provider": "litellm",
        "model": model,
        "finish_reason": response.choices[0].finish_reason,
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "response_length": len(content),
    }
    return content, meta


def _bedrock_available() -> bool:
    return shutil.which("aws") is not None


def _call_bedrock_l3(
    model_id: str,
    region: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict]:
    messages = [
        {
            "role": "user",
            "content": [{"text": user_prompt}],
        }
    ]
    system = [{"text": system_prompt}]
    inference_config = {
        "maxTokens": int(os.environ.get("PRI_L3_MAX_TOKENS", "4096")),
    }
    temperature = os.environ.get("PRI_L3_TEMPERATURE")
    if temperature not in (None, ""):
        inference_config["temperature"] = float(temperature)

    with (
        NamedTemporaryFile("w", suffix=".messages.json", delete=False) as messages_file,
        NamedTemporaryFile("w", suffix=".system.json", delete=False) as system_file,
        NamedTemporaryFile("w", suffix=".inference.json", delete=False) as inference_file,
    ):
        json.dump(messages, messages_file)
        json.dump(system, system_file)
        json.dump(inference_config, inference_file)
        messages_path = messages_file.name
        system_path = system_file.name
        inference_path = inference_file.name

    try:
        proc = subprocess.run(
            [
                "aws", "bedrock-runtime", "converse",
                "--region", region,
                "--model-id", model_id,
                "--messages", f"file://{messages_path}",
                "--system", f"file://{system_path}",
                "--inference-config", f"file://{inference_path}",
                "--output", "json",
            ],
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("PRI_L3_TIMEOUT", "300")),
        )
    finally:
        for path in (messages_path, system_path, inference_path):
            try:
                Path(path).unlink()
            except OSError:
                pass

    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:])

    response = json.loads(proc.stdout)
    content_blocks = response.get("output", {}).get("message", {}).get("content", [])
    content = "".join(block.get("text", "") for block in content_blocks)
    usage = response.get("usage", {})
    meta = {
        "provider": "bedrock",
        "model": model_id,
        "finish_reason": response.get("stopReason"),
        "prompt_tokens": usage.get("inputTokens", 0),
        "completion_tokens": usage.get("outputTokens", 0),
        "response_length": len(content),
    }
    return content, meta


def _estimate_confidence(generated: str, original: str) -> float:
    """Quick confidence score."""
    gen_files = set(re.findall(r"diff --git a/(\S+)", generated))
    orig_files = set(re.findall(r"diff --git a/(\S+)", original))
    score = 0.5
    if gen_files and orig_files:
        overlap = len(gen_files & orig_files) / max(len(gen_files), len(orig_files))
        score += 0.3 * overlap
    gen_size = generated.count("\n+") + generated.count("\n-")
    orig_size = original.count("\n+") + original.count("\n-")
    if orig_size > 0:
        ratio = min(gen_size, orig_size) / max(gen_size, orig_size)
        score += 0.2 * ratio
    return min(1.0, score)


# ── Main injection logic ─────────────────────────────────────────────────────

def inject_instance(
    instance: dict,
    repos_dir: Path,
    worktrees_dir: Path,
    test_timeout: int,
    enable_l3: bool = False,
    preflight_target_tests: bool = False,
    max_target_tests: int | None = None,
) -> dict:
    """Attempt injection for a single SWE-bench Pro instance."""

    iid = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    patch = instance["patch"]
    test_patch = instance.get("test_patch", "")

    # Determine target test files
    fail_to_pass = coerce_list(instance.get("fail_to_pass", []))
    target_tests = coerce_list(instance.get("selected_test_files_to_run", []))
    if not target_tests:
        target_tests = extract_test_files_from_patch(test_patch)
    target_tests_to_run = fail_to_pass or target_tests

    short_id = iid[:60]
    print(f"\n{'━' * 70}")
    print(f"  {short_id}")
    print(f"  repo={repo}  base_commit={base_commit[:12]}  tests={len(target_tests)}")
    print(f"{'━' * 70}")

    result = {
        "instance_id": iid,
        "repo": repo,
        "base_commit": base_commit,
        "injection_level": None,
        "success": False,
        "failure_reason": None,
    }

    # 1. Clone/update repo
    repo_dir = repos_dir / repo.replace("/", "__")
    if not repo_dir.exists():
        print(f"  Cloning {repo}...")
        r = subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", str(repo_dir)],
            capture_output=True, timeout=600,
        )
        if r.returncode != 0:
            result["failure_reason"] = "clone_failed"
            print(f"  [FAIL] Clone failed")
            return result
    else:
        print(f"  Fetching latest...")
        git("fetch", "origin", cwd=str(repo_dir), timeout=120)

    # Enable long paths on Windows
    git("config", "core.longpaths", "true", cwd=str(repo_dir))

    # Ensure base_commit is available
    git("fetch", "origin", base_commit, cwd=str(repo_dir), timeout=120)

    # Get latest main HEAD (healthy revision)
    default_branch = "main"
    for branch_name in ("main", "master", "devel"):
        r = git("rev-parse", "--verify", f"origin/{branch_name}", cwd=str(repo_dir))
        if r.returncode == 0:
            default_branch = branch_name
            break

    head_sha = git_text("rev-parse", f"origin/{default_branch}", cwd=str(repo_dir)).strip()
    print(f"  Healthy revision (h): {head_sha[:12]} ({default_branch})")
    ensure_repo_head(repo_dir, default_branch)

    # 2. Create worktree at healthy HEAD. Use a hash of the full instance id
    # because multiple benchmark instances can share the same base commit prefix.
    run_key = _stable_id_suffix(repo, iid, base_commit)
    wt_name = f"inject-{repo.replace('/', '-')}-{run_key}"
    wt_path = (worktrees_dir / wt_name).resolve()
    branch = f"inj-{run_key}"
    cleanup_worktree_branch(repo_dir, branch, wt_path)
    print(f"  Creating worktree at {wt_path}...")
    r = git("worktree", "add", "-b", branch, str(wt_path), f"origin/{default_branch}",
            cwd=str(repo_dir), timeout=1200)
    if r.returncode != 0:
        # Check if it's just a stale branch issue
        stderr_text = r.stderr.decode(errors="replace")
        if "already exists" in stderr_text:
            cleanup_worktree_branch(repo_dir, branch, wt_path)
            r = git("worktree", "add", "-b", branch, str(wt_path), f"origin/{default_branch}",
                    cwd=str(repo_dir), timeout=1200)

    if r.returncode != 0:
        err_msg = r.stderr.decode(errors="replace")[:500]
        result["failure_reason"] = f"worktree_failed: {err_msg}"
        print(f"  [FAIL] Worktree creation failed (rc={r.returncode}): {err_msg[:200]}")
        return result

    print(f"  Worktree created successfully")

    result["healthy_head"] = head_sha

    try:
        if preflight_target_tests:
            if not target_tests_to_run:
                result["injection_level"] = "Preflight_Failed"
                result["failure_reason"] = "preflight_failed: no_target_tests"
                result["preflight"] = {"ok": False, "reason": "no_target_tests"}
                print("  [preflight] Failed: no target tests")
                return result
            preflight = run_target_preflight(
                wt_path, repo, target_tests_to_run, test_timeout, max_target_tests
            )
            result["preflight"] = preflight
            if not preflight.get("ok"):
                result["injection_level"] = "Preflight_Failed"
                result["failure_reason"] = f"preflight_failed: {preflight.get('reason')}"
                print(f"  [preflight] Failed: {preflight.get('reason')}")
                return result
            print(
                "  [preflight] Passed: "
                f"{len(preflight.get('collectable_target_tests', []))} target tests"
            )
            target_tests_to_run = preflight.get("collectable_target_tests") or target_tests_to_run
            result["fail_to_pass"] = target_tests_to_run
            git("checkout", ".", cwd=str(wt_path))
            git("clean", "-fd", cwd=str(wt_path))

        def attempt_level3() -> tuple[bool, str]:
            if not enable_l3:
                return False, "Level 3 disabled"
            print(f"  [L3] Attempting LLM semantic injection...")
            problem = instance.get("problem_statement", "")
            if target_tests_to_run:
                problem += (
                    "\n\nTarget tests that must pass on healthy HEAD and fail "
                    "after the injected bug:\n"
                    + "\n".join(f"- {test}" for test in target_tests_to_run)
                )
            ok3, l3_result, l3_meta = try_level3(
                str(wt_path), str(repo_dir), base_commit, patch, problem
            )
            result["l3_metadata"] = l3_meta
            if ok3:
                print(
                    f"  [L3] Success! Diff size: {len(l3_result)} chars, "
                    f"confidence: {l3_meta.get('confidence', '?')}"
                )
                result["injection_level"] = "Level_3_LLM_Semantic"
                result["injected_diff"] = l3_result
                result["success"] = True
            else:
                print(f"  [L3] Failed: {l3_result[:150]}")
                result["failure_reason"] = l3_result
                result["injection_level"] = "Level_3_Failed"
            return ok3, l3_result

        if os.environ.get("PRI_FORCE_L3_SEMANTIC") == "1":
            print("  [L3] Forced semantic retry mode enabled; skipping L1/L2.")
            attempt_level3()
            status = "PASS" if result["success"] else "FAIL"
            level = result["injection_level"] or "none"
            print(f"\n  [{status}] {short_id} ({level})")
            return result

        # 3. Try Level 1
        print(f"  [L1] Attempting exact reverse apply...")
        ok, l1_result = try_level1(str(wt_path), patch)
        if ok:
            print(f"  [L1] Success! Diff size: {len(l1_result)} chars")
            result["injection_level"] = "Level_1_Clean_Revert"
            result["injected_diff"] = l1_result
            result["success"] = True
        else:
            print(f"  [L1] Failed: {l1_result[:100]}")

            # Reset worktree for L2
            git("checkout", ".", cwd=str(wt_path))
            git("clean", "-fd", cwd=str(wt_path))

            # 4. Try Level 2
            print(f"  [L2] Attempting AST surgery...")
            ok, l2_result, l2_meta = try_level2(str(wt_path), str(repo_dir), base_commit, patch)
            result["l2_metadata"] = l2_meta
            if ok:
                print(f"  [L2] Success! Diff size: {len(l2_result)} chars")
                result["injection_level"] = "Level_2_AST_Surgery"
                result["injected_diff"] = l2_result
                result["success"] = True
            else:
                print(f"  [L2] Failed: {l2_result[:100]}")
                if l2_meta.get("compatibility_rejected_files"):
                    print(
                        "       Compatibility rejected files: "
                        f"{l2_meta['compatibility_rejected_files']}"
                    )

                # Reset worktree for L3
                git("checkout", ".", cwd=str(wt_path))
                git("clean", "-fd", cwd=str(wt_path))

                # 5. Try Level 3 (LLM)
                if enable_l3:
                    attempt_level3()
                else:
                    result["failure_reason"] = l2_result
                    result["injection_level"] = "Level_2_Failed"

        status = "PASS" if result["success"] else "FAIL"
        level = result["injection_level"] or "none"
        print(f"\n  [{status}] {short_id} ({level})")
        return result

    finally:
        # Clean up worktree
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir))
        git("branch", "-D", branch, cwd=str(repo_dir))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Inject SWE-bench Pro instances")
    parser.add_argument("--input", "-i", default="experiments/swebench_pro/sampled_35.jsonl")
    parser.add_argument("--output", "-o", default="experiments/swebench_pro/injection_results.jsonl")
    parser.add_argument("--timeout", "-t", type=int, default=300)
    parser.add_argument("--enable-l3", action="store_true", default=False,
                        help="Enable Level 3 LLM injection for L1+L2 failures (default: disabled)")
    parser.add_argument("--no-l3", action="store_true",
                        help="Keep Level 3 disabled; retained for explicit L1/L2-only experiment scripts")
    parser.add_argument("--filter", "-f", type=str, default=None)
    parser.add_argument("--max", "-n", type=int, default=None)
    parser.add_argument("--repos-dir", default=".pri-workspace/repos")
    parser.add_argument("--worktrees-dir", default=".pri-workspace/worktrees")
    parser.add_argument("--preflight-target-tests", action="store_true",
                        help="Before injection, require target tests to exist, collect, and pass on healthy HEAD")
    parser.add_argument("--max-target-tests", type=int, default=None,
                        help="With preflight, skip instances with more target nodeids than this")
    parser.add_argument("--force", action="store_true",
                        help="Re-run instances even if an output row already exists")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    # Setup log file (tee stdout to file)
    output_path = Path(args.output)
    log_path = output_path.parent / (output_path.stem.replace("_results", "") + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_file)

    repos_dir = Path(args.repos_dir)
    worktrees_dir = Path(args.worktrees_dir)
    repos_dir.mkdir(parents=True, exist_ok=True)
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    # Load instances
    instances = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                instances.append(json.loads(line))

    if args.filter:
        instances = [i for i in instances if args.filter in i["instance_id"]]
    if args.max:
        instances = instances[:args.max]

    print(f"Instances to process: {len(instances)}")
    print(f"Output: {args.output}")

    # Run injection. Keep this resumable because large construction sweeps can
    # take many hours and may be interrupted by dependency/network failures.
    results_by_id: dict[str, dict] = {}
    if output_path.exists() and not args.force:
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("instance_id"):
                        results_by_id[row["instance_id"]] = row

    for i, inst in enumerate(instances, 1):
        print(f"\n[{i}/{len(instances)}]", end="")
        if inst["instance_id"] in results_by_id and not args.force:
            existing = results_by_id[inst["instance_id"]]
            print(
                f"  [SKIP existing] {inst['instance_id'][:60]} "
                f"({existing.get('injection_level')})",
                flush=True,
            )
            continue

        try:
            result = inject_instance(
                inst, repos_dir, worktrees_dir, args.timeout,
                enable_l3=args.enable_l3 and not args.no_l3,
                preflight_target_tests=args.preflight_target_tests,
                max_target_tests=args.max_target_tests,
            )
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {
                "instance_id": inst["instance_id"],
                "repo": inst["repo"],
                "success": False,
                "failure_reason": f"exception: {str(e)[:200]}",
            }

        # Save diff to file, store relative path in result
        diff_content = result.pop("injected_diff", None)
        if diff_content and result.get("success"):
            diff_dir = Path(args.output).parent / "diffs"
            diff_dir.mkdir(parents=True, exist_ok=True)
            diff_path = diff_dir / f"{result['instance_id']}.diff"
            diff_path.write_text(diff_content, encoding="utf-8")
            # Store path relative to project root
            project_root = Path(__file__).resolve().parent.parent
            resolved_diff_path = diff_path.resolve()
            try:
                result["injected_diff"] = str(resolved_diff_path.relative_to(project_root))
            except ValueError:
                result["injected_diff"] = str(resolved_diff_path)
        results_by_id[result["instance_id"]] = result

        # Incremental save
        with open(args.output, "w", encoding="utf-8") as f:
            for inst_out in instances:
                r = results_by_id.get(inst_out["instance_id"])
                if r is not None:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Print summary
    final_rows = [
        results_by_id[inst["instance_id"]]
        for inst in instances
        if inst["instance_id"] in results_by_id
    ]
    stats = {"total": len(final_rows), "l1_success": 0, "l2_success": 0, "l3_success": 0,
             "l3_failed": 0, "l2_failed": 0, "errors": 0}
    for row in final_rows:
        level = row.get("injection_level")
        if level == "Level_1_Clean_Revert":
            stats["l1_success"] += 1
        elif level == "Level_2_AST_Surgery":
            stats["l2_success"] += 1
        elif level == "Level_3_LLM_Semantic":
            stats["l3_success"] += 1
        elif level == "Level_3_Failed":
            stats["l3_failed"] += 1
        elif level == "Level_2_Failed":
            stats["l2_failed"] += 1
        elif str(row.get("failure_reason", "")).startswith("exception:"):
            stats["errors"] += 1
    t = stats["total"]
    l_success = stats["l1_success"] + stats["l2_success"] + stats["l3_success"]
    print(f"\n\n{'=' * 70}")
    print(f"  INJECTION EXPERIMENT SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total instances     : {t}")
    denom = t or 1
    print(f"  Level 1 success     : {stats['l1_success']} ({stats['l1_success']/denom*100:.1f}%)")
    print(f"  Level 2 success     : {stats['l2_success']} ({stats['l2_success']/denom*100:.1f}%)")
    print(f"  Level 3 success     : {stats['l3_success']} ({stats['l3_success']/denom*100:.1f}%)")
    print(f"  Level 3 failed      : {stats['l3_failed']}")
    print(f"  Level 2 failed      : {stats['l2_failed']}")
    print(f"  Errors              : {stats['errors']}")
    print(f"  Injection rate      : {l_success/denom*100:.1f}%")
    print(f"\n  Results saved to: {args.output}")
    print(f"  Log saved to: {log_path}")


if __name__ == "__main__":
    main()
