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
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from pr_injector.ast_engine.hunk_surgeon import (
    old_changed_line_ranges_for_file,
    overlaps_any_range,
    reverse_patch_hunks_for_file,
)
from pr_injector.core.compatibility import check_source_compatibility, reports_to_dicts
try:
    from prinjector_v2_metrics import (
        FidelityGateConfig,
        build_fidelity_feedback_prompt,
        evaluate_patch_pair_fidelity,
        patch_profile,
    )
except ModuleNotFoundError:
    from .prinjector_v2_metrics import (
        FidelityGateConfig,
        build_fidelity_feedback_prompt,
        evaluate_patch_pair_fidelity,
        patch_profile,
    )

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
PREFLIGHT_CACHE_SCHEMA = "target-preflight-v3-20260713"


def _stable_id_suffix(*parts: str, length: int = 12) -> str:
    data = "::".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha1(data).hexdigest()[:length]


def _target_preflight_cache_path(
    worktree: Path,
    repo: str,
    target_tests: list[str],
    max_target_tests: int | None,
) -> Path:
    head = git_text("rev-parse", "HEAD", cwd=worktree).strip()
    return _target_preflight_cache_path_for_head(
        repo, head, target_tests, max_target_tests
    )


def _target_preflight_cache_path_for_head(
    repo: str,
    head: str,
    target_tests: list[str],
    max_target_tests: int | None,
) -> Path:
    payload = json.dumps(
        {
            "schema": os.environ.get(
                "PRI_TARGET_PREFLIGHT_CACHE_SCHEMA", PREFLIGHT_CACHE_SCHEMA
            ),
            "repo": repo,
            "head": head,
            "target_tests": target_tests,
            "max_target_tests": max_target_tests,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    root = Path(
        os.environ.get(
            "PRI_TARGET_PREFLIGHT_CACHE_DIR",
            ".pri-workspace/preflight_cache/healthy_target",
        )
    )
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[3] / root
    return root / f"{digest}.json"


def _target_preflight_cache_get(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return None
    cached = dict(payload)
    cached["cache_hit"] = True
    cached["cache_path"] = str(path)
    return cached


def _target_preflight_cache_put(path: Path, payload: dict) -> None:
    if payload.get("ok") is not True:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    cached = dict(payload)
    cached["cache_hit"] = False
    cached["cache_path"] = str(path)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(cached, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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


def is_usable_git_repo(path: Path) -> bool:
    if not (path / ".git").exists():
        return False
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(path),
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


def clone_repo_with_retries(repo: str, repo_dir: Path, attempts: int = 3) -> tuple[bool, str]:
    repo_url = f"https://github.com/{repo}.git"
    timeout = int(os.environ.get("PRI_GIT_CLONE_TIMEOUT", "300"))
    commands = [
        ["git", "clone", "--filter=blob:none", "--no-tags", repo_url, str(repo_dir)],
        ["git", "clone", "--depth=1", "--filter=blob:none", "--no-tags", repo_url, str(repo_dir)],
        ["git", "clone", repo_url, str(repo_dir)],
    ]
    last_error = ""
    for attempt in range(1, attempts + 1):
        if repo_dir.exists() and not is_usable_git_repo(repo_dir):
            shutil.rmtree(repo_dir, ignore_errors=True)
        cmd = commands[min(attempt - 1, len(commands) - 1)]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last_error = f"timeout after {timeout}s: {' '.join(cmd)}"
            shutil.rmtree(repo_dir, ignore_errors=True)
            continue
        if proc.returncode == 0 and is_usable_git_repo(repo_dir):
            return True, ""
        stderr = proc.stderr.decode(errors="replace")[-500:]
        last_error = f"exit={proc.returncode}: {stderr}"
        shutil.rmtree(repo_dir, ignore_errors=True)
    return False, last_error


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


def rev_parse_commit(repo_dir: Path, ref: str) -> str | None:
    if not ref:
        return None
    r = git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=str(repo_dir), timeout=60)
    if r.returncode != 0:
        return None
    return r.stdout.decode(errors="replace").strip()


def fetch_ref_best_effort(repo_dir: Path, ref: str) -> None:
    if not ref:
        return
    git("fetch", "origin", ref, cwd=str(repo_dir), timeout=120)


def origin_head_branch(repo_dir: Path) -> str | None:
    r = git("symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=str(repo_dir), timeout=60)
    if r.returncode != 0:
        return None
    ref = r.stdout.decode(errors="replace").strip()
    prefix = "refs/remotes/origin/"
    if not ref.startswith(prefix):
        return None
    branch = ref.removeprefix(prefix)
    return branch or None


def resolve_healthy_revision(repo_dir: Path, instance: dict, base_commit: str) -> tuple[str, str, str | None]:
    """Choose a checkoutable healthy revision without assuming origin/main exists."""

    explicit_refs = [
        ("healthy_head", str(instance.get("healthy_head") or "").strip()),
        ("target_commit", str(instance.get("target_commit") or "").strip()),
    ]
    for source, ref in explicit_refs:
        if not ref:
            continue
        fetch_ref_best_effort(repo_dir, ref)
        sha = rev_parse_commit(repo_dir, ref) or rev_parse_commit(repo_dir, f"origin/{ref}")
        if sha:
            return sha, ref, f"candidate_{source}"

    branch_candidates: list[str] = []
    head_branch = origin_head_branch(repo_dir)
    if head_branch:
        branch_candidates.append(head_branch)
    for branch in ("main", "master", "devel", "develop", "trunk"):
        if branch not in branch_candidates:
            branch_candidates.append(branch)

    for branch in branch_candidates:
        ref = f"origin/{branch}"
        sha = rev_parse_commit(repo_dir, ref)
        if sha:
            return sha, ref, f"remote_branch:{branch}"

    fetch_ref_best_effort(repo_dir, base_commit)
    sha = rev_parse_commit(repo_dir, base_commit)
    if sha:
        return sha, base_commit, "base_commit_fallback"

    raise RuntimeError(f"Could not resolve healthy revision for {instance.get('instance_id')}")


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
        if len(value) == 1 and isinstance(value[0], str):
            nested = coerce_list(value[0])
            if nested != value:
                return nested
        return [str(item) for item in value]
    if isinstance(value, str):
        current = value
        for _ in range(3):
            try:
                parsed = json.loads(current)
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(current)
                except (SyntaxError, ValueError):
                    return [current]
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
            if isinstance(parsed, str) and parsed != current:
                current = parsed
                continue
            return [current]
        return [current]
    return []


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _truncate_for_l3_context(content: str, limit: int) -> str:
    """Keep both file ends when a large current file must be shortened."""

    if limit <= 0 or len(content) <= limit:
        return content
    head = max(1000, limit // 2)
    tail = max(1000, limit - head)
    omitted = len(content) - head - tail
    return (
        content[:head]
        + f"\n... (truncated {omitted} characters from the middle; tail follows)\n"
        + content[-tail:]
    )


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
    cache_path = _target_preflight_cache_path(
        wt_path, repo, target_tests, max_target_tests
    )
    cached = _target_preflight_cache_get(cache_path)
    if cached is not None:
        print("  [preflight-cache] Reusing healthy target result")
        return cached
    try:
        from verify_swebench_pro import (
            _collectable_tests,
            _create_venv,
            _filter_passing_tests,
            _existing_nodeids,
            _install_project,
            _prune_parent_nodeids,
            _target_budget_allows,
            _read_requires_python,
            _retry_with_target_execution_fallback,
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
    tests_for_collect = existing_tests or target_tests
    precollect_target_count = len(tests_for_collect)
    precollect_truncated = False
    if max_target_tests is not None and len(tests_for_collect) > max_target_tests:
        candidate_limit = int(
            os.environ.get(
                "PRI_PREFLIGHT_TARGET_MINIMIZE_CANDIDATES",
                str(max(max_target_tests * 4, max_target_tests)),
            )
        )
        if len(tests_for_collect) > candidate_limit:
            tests_for_collect = tests_for_collect[:candidate_limit]
            precollect_truncated = True
            print(
                "  [preflight] Bounding target collection before environment setup "
                f"({precollect_target_count} raw -> {len(tests_for_collect)} candidates)"
            )

    print("  [preflight] Installing deps and checking target tests on healthy HEAD...")
    venv_python = _create_venv(str(wt_path), repo)
    if not venv_python:
        return {
            "ok": False,
            "reason": "python_version_unavailable",
            "requires_python": _read_requires_python(wt_path),
        }
    _install_project(str(wt_path), repo, timeout, python=venv_python, test_files=tests_for_collect)
    if not _test_runner_available(str(wt_path), repo, venv_python):
        return {"ok": False, "reason": "test_runner_unavailable"}
    collectable = _prune_parent_nodeids(
        _collectable_tests(str(wt_path), repo, tests_for_collect, venv_python)
    )
    if not collectable:
        return {
            "ok": False,
            "reason": "target_nodeids_not_collectable"
            if existing_tests
            else "target_nodeids_not_remappable",
            "raw_target_tests": target_tests,
            "existing_target_tests": existing_tests,
        }
    if not existing_tests:
        print(
            "  [preflight] Target nodeids remapped: "
            f"{len(target_tests)} raw → {len(collectable)} collectable"
        )
    original_collectable_count = len(collectable)
    minimized = False
    if not _target_budget_allows(collectable, max_target_tests):
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
    target_execution_fallback = None
    if not healthy_executed:
        fallback_tests, fallback_result = _retry_with_target_execution_fallback(
            str(wt_path),
            repo,
            collectable,
            venv_python,
            timeout,
            metrics=None,
            phase="preflight_target_execution_fallback",
        )
        if fallback_result:
            target_execution_fallback = {
                "from": collectable,
                "to": fallback_tests,
                "result": fallback_result,
            }
            fallback_executed = int(fallback_result.get("total") or 0) > 0
            if fallback_executed:
                collectable = fallback_tests
                healthy_result = fallback_result
                healthy_executed = True
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
            "target_execution_fallback": target_execution_fallback,
        }
    result = {
        "ok": True,
        "reason": "healthy_target_passed",
        "collectable_target_tests": collectable,
        "original_collectable_target_test_count": original_collectable_count,
        "precollect_target_test_count": precollect_target_count,
        "precollect_truncated": precollect_truncated,
        "minimized": minimized,
        "healthy_minimized_from_failure": healthy_minimized_from_failure,
        "healthy_failed_tests": healthy_failed_tests,
        "target_execution_fallback": target_execution_fallback,
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
    _target_preflight_cache_put(cache_path, result)
    return result


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
                "hunk_replacement_count": len(hunk_replacements),
                "function_replacement_count": len(function_replacements),
                "level2_simplification_risk": bool(function_replacements and not hunk_replacements),
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
                "hunk_replacement_count": len(hunk_replacements),
                "function_replacement_count": len(function_replacements),
                "level2_simplification_risk": bool(function_replacements and not hunk_replacements),
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
        "hunk_replacement_count": len(hunk_replacements),
        "function_replacement_count": len(function_replacements),
        "level2_simplification_risk": bool(function_replacements and not hunk_replacements),
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

def _target_test_context_for_l3(worktree: str, target_tests: list[str]) -> str:
    """Return compact target-test source context for semantic injection.

    P2F misses are often caused by a generated diff that is syntactically
    plausible but does not affect the current target test path. Supplying the
    target test source gives Level 3 enough behavioral signal while still
    keeping edits restricted to source files shown in the Current Codebase
    section.
    """

    if not target_tests:
        return "(No target tests provided.)"

    max_files = int(os.environ.get("PRI_L3_TARGET_TEST_CONTEXT_MAX_FILES", "4"))
    limit = int(os.environ.get("PRI_L3_TARGET_TEST_CONTEXT_LIMIT", "6000"))
    paths: list[str] = []
    for test in target_tests:
        path = ""
        if "::" in test:
            path = test.split("::", 1)[0]
        elif "." in test and "/" not in test:
            parts = test.split(".")
            if len(parts) >= 3:
                path = "tests/" + "/".join(parts[:-2]) + ".py"
        if path and path.endswith(".py") and path not in paths:
            paths.append(path)
        if len(paths) >= max_files:
            break

    sections = [
        "Target tests:",
        *[f"- {test}" for test in target_tests[: int(os.environ.get("PRI_L3_TARGET_TEST_LIST_LIMIT", "12"))]],
    ]
    for rel in paths:
        path = Path(worktree) / rel
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        content = _truncate_for_l3_context(content, limit)
        sections.append(f"\n### {rel}\n```python\n{content}\n```")

    return _truncate_for_l3_context(
        "\n".join(sections),
        int(os.environ.get("PRI_L3_TARGET_TEST_CONTEXT_TOTAL_LIMIT", "18000")),
    )


def _limited_test_list_for_l3(title: str, tests: list[str] | None, env_name: str, default: int) -> str:
    tests = [str(test) for test in (tests or []) if str(test).strip()]
    if not tests:
        return f"{title}: (none provided)"
    limit = int(os.environ.get(env_name, str(default)))
    shown = tests[:limit]
    suffix = "" if len(tests) <= limit else f"\n- ... {len(tests) - limit} more omitted"
    return "\n".join([f"{title}:"] + [f"- {test}" for test in shown]) + suffix


def _behavior_contract_for_l3(
    patch: str,
    target_tests: list[str] | None,
    a_pass_to_pass: list[str] | None,
    b_pass_to_pass: list[str] | None,
) -> str:
    """Build deterministic semantic and complexity constraints for L3.

    This is deliberately derived from benchmark metadata rather than another
    LLM call. The goal is to reduce L3's tendency to create a tiny local
    target-test trigger when the historical fix had a broader behavioral
    surface.
    """

    shape = _diff_shape_for_l3(patch)
    source_files = extract_source_files_from_patch(patch)
    test_files = extract_test_files_from_patch(patch)
    line_changes = int(shape.get("line_changes") or 0)
    hunks = int(shape.get("hunks") or 0)
    source_file_count = int(shape.get("source_files") or 0)
    high_complexity = (
        line_changes >= int(os.environ.get("PRI_L3_HIGH_COMPLEXITY_LINES", "31"))
        or hunks >= int(os.environ.get("PRI_L3_HIGH_COMPLEXITY_HUNKS", "4"))
        or source_file_count >= int(os.environ.get("PRI_L3_HIGH_COMPLEXITY_FILES", "2"))
    )
    min_lines = max(1, int(line_changes * float(os.environ.get("PRI_L3_CONTRACT_MIN_LINE_RATIO", "0.50"))))
    min_hunks = max(1, int(hunks * float(os.environ.get("PRI_L3_CONTRACT_MIN_HUNK_RATIO", "0.50"))))
    max_lines = max(min_lines, int(line_changes * float(os.environ.get("PRI_L3_CONTRACT_MAX_LINE_RATIO", "2.50"))))

    parts = [
        "## Semantic Contract For This Injection",
        "",
        "Recreate the historical defect as a behavior-level regression in the current code, not as a test-specific hack.",
        "The injected diff must make the target FAIL_TO_PASS behavior fail while preserving unrelated PASS_TO_PASS behavior.",
        "",
        "Historical patch footprint:",
        f"- source_files={source_file_count}, hunks={hunks}, line_changes={line_changes}",
        f"- source paths: {', '.join(source_files[:8]) or '(none parsed)'}",
        f"- test paths from historical test patch: {', '.join(test_files[:8]) or '(none parsed)'}",
        "",
        "Complexity preservation constraints:",
        f"- Try to keep B line changes in [{min_lines}, {max_lines}] unless current architecture makes that impossible.",
        f"- Try to keep B hunks >= {min_hunks}; do not collapse multi-hunk behavior into a one-line target trigger.",
        "- Preserve cross-file or cross-module contracts when the historical patch changed them.",
    ]
    if high_complexity:
        parts.extend([
            "",
            "High-complexity lane:",
            "- This A-side patch is medium/large enough that a tiny local B diff is unacceptable.",
            "- Prefer preserving API/call-chain effects, validation branches, cache/helper interactions, or cross-module behavior.",
            "- If exact files moved, adapt to the modern equivalent abstraction rather than editing only the final assertion path.",
        ])
    parts.extend([
        "",
        _limited_test_list_for_l3(
            "Target tests that should fail after injection",
            target_tests,
            "PRI_L3_CONTRACT_TARGET_TEST_LIMIT",
            10,
        ),
        "",
        _limited_test_list_for_l3(
            "PASS_TO_PASS / adjacent tests that should not be broken",
            b_pass_to_pass or a_pass_to_pass,
            "PRI_L3_CONTRACT_P2P_TEST_LIMIT",
            18,
        ),
    ])
    return "\n".join(parts)


def try_level3(
    worktree: str,
    repo_dir: str,
    base_commit: str,
    patch: str,
    problem_statement: str,
    target_tests: list[str] | None = None,
    fidelity_gate: bool = False,
    fidelity_gate_config: FidelityGateConfig | None = None,
    a_fail_to_pass: list[str] | None = None,
    b_fail_to_pass: list[str] | None = None,
    a_pass_to_pass: list[str] | None = None,
    b_pass_to_pass: list[str] | None = None,
    retry_feedback_seed: str = "",
) -> tuple[bool, str, dict]:
    """Attempt Level 3: LLM semantic reversion.

    Calls the configured Level-3 model provider to semantically re-introduce
    the bug when both textual and structural matching have failed.

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
    provider_preference = os.environ.get("PRI_L3_PROVIDER", "agent_maestro_anthropic").strip().lower()
    agent_maestro_model = os.environ.get("PRI_AGENT_MAESTRO_MODEL", "claude-opus-4.8")
    agent_maestro_base_url = os.environ.get("AGENT_MAESTRO_BASE_URL", "http://127.0.0.1:23334")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    litellm_model = os.environ.get("PRI_L3_MODEL", "anthropic/claude-opus-4.8")

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
            content = _truncate_for_l3_context(
                content,
                int(os.environ.get("PRI_L3_FILE_CONTEXT_LIMIT", "12000")),
            )
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
                        content = _truncate_for_l3_context(
                            content,
                            int(os.environ.get("PRI_L3_DISCOVERY_FILE_CONTEXT_LIMIT", "8000")),
                        )
                        current_files[rel] = content
                        if len(current_files) >= 5:
                            break

    if not current_files:
        return False, "Level 3: no source files exist on HEAD (architecture deprecated)", {
            "l3_reason": "all_files_deleted",
        }

    # Build prompt
    files_section = ""
    files_section_limit = int(os.environ.get("PRI_L3_FILES_CONTEXT_TOTAL_LIMIT", "48000"))
    for path, content in current_files.items():
        block = f"### {path}\n```python\n{content}\n```\n\n"
        remaining = files_section_limit - len(files_section)
        if remaining <= 0:
            break
        files_section += block[:remaining]
    target_tests = target_tests or []
    target_section = _target_test_context_for_l3(worktree, target_tests)
    behavior_contract = _behavior_contract_for_l3(
        patch,
        target_tests,
        a_pass_to_pass or [],
        b_pass_to_pass or [],
    )

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
        " historical files that are absent from the current codebase.\n"
        "9. Preserve the original fix's difficulty surface: if the original fix spans multiple"
        " files, hunks, branches, API contracts, or modules, the injected bug must preserve the"
        " equivalent cross-file or cross-module relationship in the modern code. Do not collapse"
        " a multi-hunk or multi-module historical bug into a tiny one-line local bug merely because"
        " that would trigger a target test.\n"
        "10. Treat listed PASS_TO_PASS/adjacent tests as protected behavior: the injected bug should"
        " not break them.\n"
        "11. Do not hard-code test names, special-case test fixtures, raise generic exceptions, or"
        " delete broad code paths merely to force a target failure.\n"
        "12. Every response line must be a legal unified-diff line. Never include analysis,"
        " caveats, prose, bullets, or sentences inside or after the diff."
    )

    user_prompt = f"""## Original Bug Context

### Issue Description
{problem_statement[:3000] if problem_statement else "(No description available)"}

### Original Fix (PR Diff)
```diff
{patch[:int(os.environ.get("PRI_L3_PATCH_CONTEXT_LIMIT", "12000"))]}
```

## Current Codebase (Latest Version)

{files_section}

## Target Test Context

{target_section}

{behavior_contract}

Allowed current files: {", ".join(current_files.keys())}

## Task

The original PR fixed a bug described above. The codebase has since evolved.
Your task: Create a unified diff that reintroduces the SAME logical bug into the CURRENT code.

The bug should:
- Cause the same category of failure as the original
- Specifically affect the behavior exercised by the target tests above
- Be in the equivalent code location (which may have moved or been refactored)
- Preserve the original fix's approximate scope: files, hunks, functions, and API contract surface
- Avoid over-localizing the failure into a single trivial statement when the original fix was broader
- Preserve the protected PASS_TO_PASS/adjacent behavior listed above
- Be subtle enough that it's not immediately obvious

Output ONLY the unified diff, starting with "diff --git"."""

    provider = _choose_l3_provider(
        provider_preference=provider_preference,
        azure_endpoint=endpoint,
        anthropic_key=anthropic_key,
    )
    model_label = {
        "azure": deployment,
        "agent_maestro_anthropic": agent_maestro_model,
        "litellm_anthropic": litellm_model,
        "codex_agent": os.environ.get("PRI_CODEX_MODEL", "default-codex"),
    }.get(provider, "")
    if provider == "none":
        return False, (
            "Level 3: no configured model provider. Set PRI_L3_PROVIDER=agent_maestro_anthropic "
            "and keep Agent Maestro running at AGENT_MAESTRO_BASE_URL, or explicitly configure "
            "azure/litellm."
        ), {}

    print(f"  [L3] Calling LLM ({provider}:{model_label})...")
    print(f"       Current files available: {list(current_files.keys())}")

    max_attempts = max(1, int(os.environ.get("PRI_L3_APPLY_ATTEMPTS", "2")))
    retry_feedback = retry_feedback_seed
    last_error = "Level 3: no attempt made"
    last_meta: dict = {}
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost_usd = 0.0
    configured_candidates = max(
        1, int(os.environ.get("PRI_L3_CANDIDATES_PER_ATTEMPT", "1"))
    )
    candidates_per_attempt = _adaptive_l3_candidate_count(
        provider, patch, configured_candidates
    )
    retry_candidates_per_attempt = max(
        1,
        int(os.environ.get("PRI_L3_RETRY_CANDIDATES_PER_ATTEMPT", "1")),
    )
    for attempt in range(1, max_attempts + 1):
        attempt_user_prompt = user_prompt
        if retry_feedback:
            attempt_user_prompt += f"""

## Feedback From Previous Attempt

The previous attempt did not pass construction validation:

```text
{retry_feedback[:2000]}
```

Regenerate the diff from the CURRENT code shown above. Keep modern function
signatures and modern imports. Do not use stale line numbers or historical
files. Match the original patch's approximate files, hunks, line-change surface,
and target-test semantics. Output ONLY a clean unified diff. If the previous
error mentioned a corrupt patch or unexpected line, remove all prose and ensure
every hunk body line starts with exactly one of: space, +, -, or \\.
"""
        try:
            ranked_candidates: list[tuple[float, str, dict]] = []
            attempt_candidate_count = (
                candidates_per_attempt if attempt == 1 else retry_candidates_per_attempt
            )
            for candidate_idx in range(1, attempt_candidate_count + 1):
                candidate_prompt = attempt_user_prompt
                if attempt_candidate_count > 1:
                    candidate_prompt += f"""

## Candidate Diversity Requirement

This is candidate {candidate_idx} of {attempt_candidate_count} for the same bug.
Produce an independently reasoned diff that still obeys the same semantic
contract, protected P2P behavior, file scope, and complexity-preservation
requirements. Do not merely reformat a previous patch.
"""
                if provider == "codex_agent":
                    content, meta = _call_codex_agent_l3(
                        worktree=worktree,
                        user_prompt=candidate_prompt,
                    )
                else:
                    content, meta = _call_l3_model(
                        provider=provider,
                        endpoint=endpoint,
                        deployment=deployment,
                        api_version=api_version,
                        agent_maestro_model=agent_maestro_model,
                        agent_maestro_base_url=agent_maestro_base_url,
                        litellm_model=litellm_model,
                        system_prompt=system_prompt,
                        user_prompt=candidate_prompt,
                    )
                meta["attempt"] = attempt
                meta["candidate_idx"] = candidate_idx
                meta["candidates_per_attempt"] = attempt_candidate_count
                meta["initial_candidates_per_attempt"] = candidates_per_attempt
                meta["retry_candidates_per_attempt"] = retry_candidates_per_attempt
                meta["max_attempts"] = max_attempts
                meta["retry_count"] = max(0, attempt - 1)
                total_prompt_tokens += int(meta.get("prompt_tokens") or 0)
                total_completion_tokens += int(meta.get("completion_tokens") or 0)
                total_cost_usd += float(meta.get("cost_usd") or meta.get("total_cost_usd") or 0.0)
                meta["total_prompt_tokens"] = total_prompt_tokens
                meta["total_completion_tokens"] = total_completion_tokens
                meta["total_tokens"] = total_prompt_tokens + total_completion_tokens
                meta["total_cost_usd"] = round(total_cost_usd, 8)
                last_meta = meta

                print(
                    f"       Response attempt {attempt}/{max_attempts} "
                    f"candidate {candidate_idx}/{attempt_candidate_count}: {len(content)} chars, "
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
                if provider == "codex_agent":
                    invalid_files = sorted(
                        path for path in diff_files if _codex_agent_forbidden_path(path)
                    )
                else:
                    invalid_files = sorted(diff_files - set(current_files))
                if invalid_files:
                    meta["invalid_files"] = invalid_files
                    scope = "forbidden test/harness/dependency" if provider == "codex_agent" else "outside current set"
                    last_error = f"Level 3: LLM diff touched {scope} files: {invalid_files}"
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

                # Actually apply for cheap scoring, then revert before ranking.
                subprocess.run(
                    apply_args,
                    cwd=worktree, input=diff.encode(),
                    capture_output=True, timeout=30,
                )
                applied_diff = git_text("diff", cwd=worktree)
                if applied_diff.strip():
                    meta["confidence"] = _estimate_confidence(applied_diff, patch)
                    meta["fidelity"] = _l3_fidelity_profile(applied_diff, patch)
                    candidate_score = float(meta.get("confidence") or 0.0)
                    if fidelity_gate:
                        v2_gate = evaluate_patch_pair_fidelity(
                            a_patch=patch,
                            b_patch=applied_diff,
                            a_fail_to_pass=a_fail_to_pass,
                            b_fail_to_pass=b_fail_to_pass or target_tests,
                            a_pass_to_pass=a_pass_to_pass,
                            b_pass_to_pass=b_pass_to_pass,
                            injection_level="Level_3_LLM_Semantic",
                            config=fidelity_gate_config,
                        )
                        v2_gate["stage"] = "l3_generation"
                        meta["v2_fidelity_gate"] = v2_gate
                        meta["v2_fidelity_feedback_prompt"] = build_fidelity_feedback_prompt(v2_gate)
                        candidate_score = float(v2_gate.get("score") or 0.0)
                        if (
                            not v2_gate.get("pass_gate")
                            and env_bool("PRI_L3_REJECT_V2_GATE", True)
                        ):
                            subprocess.run(
                                ["git", "apply", "-R"],
                                cwd=worktree,
                                input=applied_diff.encode(),
                                capture_output=True,
                                timeout=30,
                            )
                            last_error = "Level 3: generated diff failed v2 fidelity gate"
                            retry_feedback = meta["v2_fidelity_feedback_prompt"]
                            print(
                                "       Rejected v2-gate-failing Level 3 diff; "
                                f"score={v2_gate.get('score')} tags={v2_gate.get('tags')}"
                            )
                            continue
                    elif (
                        env_bool("PRI_L3_REJECT_OVERSIMPLIFIED", True)
                        and _complexity_mismatch(meta["fidelity"])
                    ):
                        subprocess.run(
                            ["git", "apply", "-R"],
                            cwd=worktree,
                            input=applied_diff.encode(),
                            capture_output=True,
                            timeout=30,
                        )
                        last_error = "Level 3: generated diff complexity does not match original patch"
                        retry_feedback = (
                            f"{last_error}. Fidelity profile: "
                            f"{json.dumps(meta['fidelity'], ensure_ascii=False)[:1200]}"
                        )
                        print("       Rejected complexity-mismatched Level 3 diff; retrying")
                        continue
                    subprocess.run(
                        ["git", "apply", "-R"],
                        cwd=worktree,
                        input=applied_diff.encode(),
                        capture_output=True,
                        timeout=30,
                    )
                    ranked_candidates.append((candidate_score, applied_diff, meta))
                    print(
                        "       Accepted Level 3 candidate for ranking; "
                        f"score={round(candidate_score, 4)} "
                        f"candidate={candidate_idx}/{attempt_candidate_count}"
                    )
                    if _should_early_accept_l3_candidate(
                        candidate_score,
                        meta,
                        candidate_idx,
                        attempt_candidate_count,
                    ):
                        meta["l3_early_accept"] = True
                        meta["l3_early_accept_threshold"] = float(
                            os.environ.get("PRI_L3_EARLY_ACCEPT_SCORE", "0.80")
                        )
                        print(
                            "       High-confidence candidate accepted; "
                            "skipping redundant full-agent candidate"
                        )
                        break
                    continue

                last_error = "Level 3: patch applied but produced no diff"
                retry_feedback = last_error

            if ranked_candidates:
                ranked_candidates.sort(
                    key=lambda item: (
                        item[0],
                        float((item[2].get("confidence") or 0.0)),
                        len(item[1]),
                    ),
                    reverse=True,
                )
                best_score, best_diff, best_meta = ranked_candidates[0]
                proc = subprocess.run(
                    ["git", "apply", "--recount"],
                    cwd=worktree,
                    input=best_diff.encode(),
                    capture_output=True,
                    timeout=30,
                )
                if proc.returncode != 0:
                    whitespace_proc = subprocess.run(
                        ["git", "apply", "--recount", "--ignore-space-change"],
                        cwd=worktree,
                        input=best_diff.encode(),
                        capture_output=True,
                        timeout=30,
                    )
                    if whitespace_proc.returncode != 0:
                        last_error = (
                            "Level 3: ranked best diff failed to reapply: "
                            + whitespace_proc.stderr.decode(errors="replace")[:300]
                        )
                        retry_feedback = last_error
                        print(
                            "       Ranked Level 3 diff failed to reapply; "
                            f"score={round(best_score, 4)}"
                        )
                        continue
                best_meta["l3_ranked_candidate_count"] = len(ranked_candidates)
                best_meta["l3_ranked_best_score"] = round(best_score, 6)
                return True, git_text("diff", cwd=worktree), best_meta

        except Exception as e:
            last_error = f"Level 3: LLM call failed: {str(e)[:200]}"
            last_meta = {
                "error": str(e)[:300],
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retry_count": max(0, attempt - 1),
                "total_prompt_tokens": total_prompt_tokens,
                "total_completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
                "total_cost_usd": round(total_cost_usd, 8),
            }
            retry_feedback = last_error
            continue

    last_meta.setdefault("max_attempts", max_attempts)
    last_meta.setdefault("retry_count", max(0, int(last_meta.get("attempt") or max_attempts) - 1))
    last_meta.setdefault("total_prompt_tokens", total_prompt_tokens)
    last_meta.setdefault("total_completion_tokens", total_completion_tokens)
    last_meta.setdefault("total_tokens", total_prompt_tokens + total_completion_tokens)
    last_meta.setdefault("total_cost_usd", round(total_cost_usd, 8))
    return False, last_error, last_meta


def _extract_diff(response: str) -> str | None:
    """Extract unified diff from LLM response."""
    # Prefer fenced code blocks so trailing explanations are not swallowed by a
    # greedy bare-diff regex.
    for m in re.finditer(r"```(?:diff)?\s*\n(.*?)```", response, re.DOTALL):
        block = m.group(1).strip()
        if "diff --git" in block or "@@" in block:
            trimmed = _trim_bare_diff(block)
            if trimmed:
                return trimmed
    # Try bare diff as a fallback.
    m = re.search(r"(diff --git\s+.*)", response, re.DOTALL)
    if m:
        trimmed = _trim_bare_diff(m.group(1).strip())
        return trimmed or None
    return None


def _write_l3_debug(worktree: str, meta: dict, response: str, diff: str) -> None:
    debug_root = Path(os.environ.get("PRI_L3_DEBUG_DIR", ".pri-workspace/l3-debug"))
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
    start = next((i for i, line in enumerate(lines) if line.startswith("diff --git ")), None)
    if start is None:
        return ""

    keep: list[str] = []
    in_hunk = False
    for line in lines[start:]:
        if line.startswith("diff --git "):
            in_hunk = False
            keep.append(line)
        elif line.startswith(
            (
                "index ",
                "--- ",
                "+++ ",
                "new file mode",
                "deleted file mode",
                "old mode",
                "new mode",
                "similarity index ",
                "rename from ",
                "rename to ",
            )
        ):
            in_hunk = False
            keep.append(line)
        elif line.startswith("@@ "):
            in_hunk = True
            keep.append(line)
        elif in_hunk and (
            line.startswith(("+", "-", " ", "\\ No newline at end of file"))
            or line == ""
        ):
            keep.append(line)
        elif line.strip() == "":
            if keep:
                keep.append(line)
        else:
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


def _choose_l3_provider(provider_preference: str, azure_endpoint: str, anthropic_key: str) -> str:
    """Resolve the Level-3 model provider without silently using legacy Bedrock paths."""
    preference = (provider_preference or "agent_maestro_anthropic").lower()
    aliases = {
        "maestro": "agent_maestro_anthropic",
        "agent_maestro": "agent_maestro_anthropic",
        "agent-maestro": "agent_maestro_anthropic",
        "anthropic_maestro": "agent_maestro_anthropic",
        "anthropic": "litellm_anthropic",
        "codex": "codex_agent",
        "codex-agent": "codex_agent",
        "full_agent": "codex_agent",
        "full-agent": "codex_agent",
    }
    preference = aliases.get(preference, preference)
    if preference in {"bedrock", "aws", "aws_bedrock", "amazon_bedrock"}:
        return "none"
    if preference in {"agent_maestro_anthropic", "azure", "litellm_anthropic", "codex_agent"}:
        if preference == "azure" and not azure_endpoint:
            return "none"
        if preference == "litellm_anthropic" and not anthropic_key:
            return "none"
        return preference
    if preference != "auto":
        return "none"

    if azure_endpoint:
        return "azure"
    if os.environ.get("AGENT_MAESTRO_DISABLE", "").lower() not in {"1", "true", "yes", "on"}:
        return "agent_maestro_anthropic"
    if anthropic_key:
        return "litellm_anthropic"
    return "none"


def _post_json(url: str, payload: dict, headers: dict[str, str], timeout_s: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    attempts = max(1, int(os.environ.get("PRI_AGENT_MAESTRO_HTTP_ATTEMPTS", "3")))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(
                f"HTTP {exc.code} from {url} (request_bytes={len(data)}): {detail[-1000:]}"
            )
            transient = exc.code in {408, 429, 500, 502, 503, 504} or "408" in detail
            if not transient or attempt >= attempts:
                raise last_error from exc
        except urllib.error.URLError as exc:
            last_error = RuntimeError(
                f"Could not reach Agent Maestro at {url} (request_bytes={len(data)}): {exc}"
            )
            if attempt >= attempts:
                raise last_error from exc
        time.sleep(min(2 ** (attempt - 1), 8))
    raise RuntimeError(str(last_error or "Agent Maestro request failed"))


def _call_agent_maestro_anthropic_l3(
    model: str,
    base_url: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict]:
    api_key = os.environ.get("AGENT_MAESTRO_API_KEY", "")
    endpoint = base_url.rstrip("/") + "/api/anthropic/v1/messages"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    payload = {
        "model": model,
        "max_tokens": int(os.environ.get("PRI_L3_MAX_TOKENS", "4096")),
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    response = _post_json(
        endpoint,
        payload,
        headers,
        int(os.environ.get("PRI_AGENT_MAESTRO_TIMEOUT", os.environ.get("PRI_L3_TIMEOUT", "300"))),
    )
    content = "".join(
        block.get("text", "")
        for block in response.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    usage = response.get("usage", {}) if isinstance(response, dict) else {}
    meta = {
        "provider": "agent_maestro_anthropic",
        "base_url": base_url.rstrip("/"),
        "model": model,
        "finish_reason": response.get("stop_reason") or response.get("stopReason"),
        "prompt_tokens": usage.get("input_tokens") or usage.get("inputTokens") or 0,
        "completion_tokens": usage.get("output_tokens") or usage.get("outputTokens") or 0,
        "response_length": len(content),
    }
    return content, meta


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


def _codex_agent_forbidden_path(path: str) -> bool:
    """Protect the benchmark harness while allowing modern source discovery."""
    normalized = path.replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    basename = normalized.rsplit("/", 1)[-1]
    if is_test_file(normalized):
        return True
    if normalized.startswith(
        (".github/", ".azure-pipelines/", ".circleci/", "docs/", "doc/", "ci/")
    ):
        return True
    if basename in {
        "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "pytest.ini",
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
        "poetry.lock", "pipfile", "pipfile.lock", "dockerfile",
    }:
        return True
    return basename.startswith("requirements") or basename.endswith((".lock", ".md", ".rst"))


def _codex_agent_task_prompt(user_prompt: str) -> str:
    """Remove redundant embedded source because Codex can inspect the worktree."""
    compact = re.sub(
        r"## Current Codebase \(Latest Version\)\s+.*?## Target Test Context",
        (
            "## Current Codebase (Latest Version)\n\n"
            "Inspect the complete working tree directly; follow modern call chains and moved abstractions.\n\n"
            "## Target Test Context"
        ),
        user_prompt,
        flags=re.DOTALL,
    )
    compact = re.sub(r"\nAllowed current files: .*?\n", "\n", compact)
    return compact.replace(
        'Output ONLY the unified diff, starting with "diff --git".',
        "Inspect the full current repository and implement the semantic regression directly in the working tree.",
    )


def _adaptive_l3_candidate_count(provider: str, patch: str, configured: int) -> int:
    """Reserve multi-candidate full-agent ranking for large historical fixes."""

    configured = max(1, configured)
    if provider != "codex_agent" or configured == 1:
        return configured
    minimum_lines = int(os.environ.get("PRI_L3_MULTI_CANDIDATE_MIN_LINES", "31"))
    return configured if patch_profile(patch).line_changes >= minimum_lines else 1


def _should_early_accept_l3_candidate(
    score: float,
    metadata: dict,
    candidate_idx: int,
    candidate_count: int,
) -> bool:
    if candidate_idx != 1 or candidate_count <= 1:
        return False
    gate = metadata.get("v2_fidelity_gate") or {}
    if gate and not gate.get("pass_gate"):
        return False
    threshold = float(os.environ.get("PRI_L3_EARLY_ACCEPT_SCORE", "0.80"))
    return score >= threshold


def _git_submodule_paths(worktree: Path) -> set[str]:
    gitmodules = worktree / ".gitmodules"
    if not gitmodules.exists():
        return set()
    return {
        match.group(1).strip().replace("\\", "/")
        for match in re.finditer(
            r"^\s*path\s*=\s*(.+?)\s*$",
            gitmodules.read_text(encoding="utf-8", errors="replace"),
            flags=re.MULTILINE,
        )
    }


def _codex_agent_safe_preflight_baseline(
    status: str,
    diff: str,
    submodule_paths: set[str] | None = None,
) -> bool:
    """Allow dirty submodule internals prepared by repo bootstrap, not root edits."""
    if diff.strip():
        return False
    allowed_submodules = submodule_paths or set()
    for line in status.splitlines():
        # Porcelain v1 uses a submodule worktree marker in column two. The
        # superproject has no changed blob in this state, so it cannot leak
        # into the captured injection diff.
        xy = line[:2]
        path = line[3:].split(" -> ")[-1].replace("\\", "/")
        if path in allowed_submodules and xy[:1] == " " and xy[1:] in {"M", "m", "?"}:
            continue
        return False
    return True


def _call_codex_agent_l3(worktree: str, user_prompt: str) -> tuple[str, dict]:
    """Let Codex inspect/edit the full modern repo, then return its source diff.

    The dedicated construction worktree is restored before returning so the
    normal PR-INJECTOR ranking and strict verification path remains authoritative.
    """
    worktree_path = Path(worktree).resolve()
    submodule_paths = _git_submodule_paths(worktree_path)
    pre_status = git_text("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree)
    pre_diff = git_text(
        "diff", "--binary", "--ignore-submodules=all", "HEAD", "--", cwd=worktree
    )
    if not _codex_agent_safe_preflight_baseline(pre_status, pre_diff, submodule_paths):
        raise RuntimeError(
            "Codex L3 requires no pre-existing superproject source edits; "
            f"status={pre_status[:500]!r}"
        )

    debug_root = Path(os.environ.get("PRI_L3_DEBUG_DIR", ".pri-workspace/l3-debug"))
    call_id = _stable_id_suffix(worktree, user_prompt, str(time.time_ns()))
    call_dir = debug_root / worktree_path.name / f"codex-agent-{call_id}"
    call_dir.mkdir(parents=True, exist_ok=True)
    system_path = call_dir / "system.txt"
    task_path = call_dir / "task.txt"
    result_path = call_dir / "runner.json"

    system_path.write_text(
        "You are the semantic transplantation engine inside PR-INJECTOR. Inspect the entire "
        "modern repository and edit it directly to reintroduce the historical bug described in "
        "the task. Follow the behavior and complexity contract, including protected adjacent "
        "behavior. Modify implementation source only. Never modify tests, snapshots, CI, docs, "
        "dependency manifests, or generated lock files. Do not add a test-specific branch, syntax "
        "error, generic exception, or broad deletion. Do not fix the bug: construct the requested "
        "feature-regressed/buggy state. Leave the intended source changes in the working tree; "
        "your final prose is ignored and the Git diff is the only scored artifact. Do not install "
        "dependencies or run a broad test suite. You may run only the provided narrow target tests "
        "once after editing; PR-INJECTOR performs authoritative verification afterward.\n",
        encoding="utf-8",
    )
    task_path.write_text(_codex_agent_task_prompt(user_prompt), encoding="utf-8")

    runner = (
        Path(__file__).resolve().parents[3]
        / "construction_toolkit"
        / "integrations"
        / "agent_maestro"
        / "run_codex_headless.py"
    )
    cmd = [
        sys.executable,
        str(runner),
        "--repo", str(worktree_path),
        "--system", str(system_path),
        "--task", str(task_path),
        "--out", str(result_path),
        "--timeout-s", os.environ.get("PRI_CODEX_AGENT_TIMEOUT", "1800"),
        "--reasoning-effort", os.environ.get("PRI_CODEX_REASONING_EFFORT", "high"),
        "--runner-note",
        (
            "PR-INJECTOR construction note: edit this dedicated worktree directly to recreate "
            "the historical defect. Do not wait for approval and do not edit tests or harness files."
        ),
    ]
    model = os.environ.get("PRI_CODEX_MODEL", "").strip()
    if model:
        cmd.extend(["--model", model])

    started = time.time()
    proc: subprocess.CompletedProcess | None = None
    diff = ""
    untracked: list[str] = []
    try:
        proc = subprocess.run(
            cmd,
            cwd=Path(__file__).resolve().parents[3],
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("PRI_CODEX_AGENT_TIMEOUT", "1800")) + 60,
        )
        diff = git_text(
            "diff", "--binary", "--ignore-submodules=all", "HEAD", "--", cwd=worktree
        )
        status = git_text("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree)
        untracked = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
    finally:
        git("checkout", "--", ".", cwd=worktree)
        git("clean", "-fd", cwd=worktree)

    runner_payload: dict = {}
    if result_path.exists():
        try:
            runner_payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            runner_payload = {}
    meta = {
        "provider": "codex_agent",
        "model": (
            (runner_payload.get("env") or {}).get("CODEX_MODEL_RESOLVED")
            or model
            or "default-codex"
        ),
        "finish_reason": "completed" if proc and proc.returncode == 0 else "runner_error",
        "returncode": proc.returncode if proc else None,
        "timed_out": bool(runner_payload.get("timed_out")),
        "codex_error": runner_payload.get("codex_error") or (proc.stderr[-1000:] if proc else ""),
        "elapsed_s": round(time.time() - started, 3),
        "response_length": len(diff),
        "changed_files": sorted(_diff_files(diff)),
        "untracked_files": untracked,
        "runner_result": str(result_path),
        "prompt_tokens": int((runner_payload.get("usage") or {}).get("input_tokens") or 0),
        "cached_prompt_tokens": int(
            (runner_payload.get("usage") or {}).get("cached_input_tokens") or 0
        ),
        "completion_tokens": int((runner_payload.get("usage") or {}).get("output_tokens") or 0),
        "reasoning_output_tokens": int(
            (runner_payload.get("usage") or {}).get("reasoning_output_tokens") or 0
        ),
        "reasoning_effort": os.environ.get("PRI_CODEX_REASONING_EFFORT", "high"),
    }
    if untracked:
        raise RuntimeError(f"Codex L3 created untracked files, which are not allowed: {untracked[:8]}")
    if not diff.strip():
        error = meta["codex_error"] or f"runner returncode={meta['returncode']}"
        raise RuntimeError(f"Codex L3 produced no source diff: {error}")
    return diff, meta


def _call_l3_model(
    provider: str,
    endpoint: str,
    deployment: str,
    api_version: str,
    agent_maestro_model: str,
    agent_maestro_base_url: str,
    litellm_model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict]:
    if provider == "agent_maestro_anthropic":
        return _call_agent_maestro_anthropic_l3(
            model=agent_maestro_model,
            base_url=agent_maestro_base_url,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    if provider == "azure":
        return _call_azure_l3(
            endpoint=endpoint,
            deployment=deployment,
            api_version=api_version,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    if provider == "litellm_anthropic":
        return _call_litellm_l3(
            model=litellm_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
    raise RuntimeError(f"Unsupported Level-3 provider: {provider}")


def _diff_shape_for_l3(diff: str) -> dict:
    files = sorted(_diff_files(diff))
    source_files = [path for path in files if not is_test_file(path)]
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return {
        "files": len(files),
        "source_files": len(set(source_files)),
        "modules": len(set(str(Path(path).parent) for path in source_files)),
        "hunks": sum(1 for line in diff.splitlines() if line.startswith("@@ ")),
        "line_changes": added + removed,
        "added": added,
        "removed": removed,
    }


def _l3_fidelity_profile(generated: str, original: str) -> dict:
    generated_shape = _diff_shape_for_l3(generated)
    original_shape = _diff_shape_for_l3(original)
    original_lines = max(int(original_shape["line_changes"] or 0), 1)
    original_hunks = max(int(original_shape["hunks"] or 0), 1)
    original_source_files = max(int(original_shape["source_files"] or 0), 1)
    line_ratio = generated_shape["line_changes"] / original_lines
    hunk_ratio = generated_shape["hunks"] / original_hunks
    source_file_ratio = generated_shape["source_files"] / original_source_files
    simplification_reasons: list[str] = []
    over_complexity_reasons: list[str] = []
    if original_shape["source_files"] >= 2 and generated_shape["source_files"] <= 1:
        simplification_reasons.append("source_file_count_collapsed")
    if original_shape["modules"] >= 2 and generated_shape["modules"] <= 1:
        simplification_reasons.append("module_count_collapsed")
    if original_shape["hunks"] >= 3 and hunk_ratio < float(os.environ.get("PRI_L3_MIN_HUNK_RATIO", "0.35")):
        simplification_reasons.append("hunk_count_collapsed")
    if original_shape["line_changes"] >= 20 and line_ratio < float(os.environ.get("PRI_L3_MIN_LINE_RATIO", "0.20")):
        simplification_reasons.append("line_change_count_collapsed")
    if generated_shape["line_changes"] <= int(os.environ.get("PRI_L3_MIN_LINE_CHANGES", "4")) and original_shape["line_changes"] >= 12:
        simplification_reasons.append("generated_diff_too_small")
    if (
        generated_shape["source_files"] - original_shape["source_files"] >= 2
        and source_file_ratio > float(os.environ.get("PRI_L3_MAX_SOURCE_FILE_RATIO", "2.0"))
    ):
        over_complexity_reasons.append("source_file_count_inflated")
    if (
        generated_shape["hunks"] - original_shape["hunks"] >= 2
        and hunk_ratio > float(os.environ.get("PRI_L3_MAX_HUNK_RATIO", "3.0"))
    ):
        over_complexity_reasons.append("hunk_count_inflated")
    if (
        generated_shape["line_changes"] - original_shape["line_changes"] >= 20
        and line_ratio > float(os.environ.get("PRI_L3_MAX_LINE_RATIO", "4.0"))
    ):
        over_complexity_reasons.append("line_change_count_inflated")
    fidelity_reasons = simplification_reasons + over_complexity_reasons
    return {
        "original": original_shape,
        "generated": generated_shape,
        "line_change_ratio": round(line_ratio, 4),
        "hunk_ratio": round(hunk_ratio, 4),
        "source_file_ratio": round(source_file_ratio, 4),
        "simplification_risk": bool(simplification_reasons),
        "simplification_reasons": simplification_reasons,
        "over_complexity_risk": bool(over_complexity_reasons),
        "over_complexity_reasons": over_complexity_reasons,
        "fidelity_risk": bool(fidelity_reasons),
        "fidelity_reasons": fidelity_reasons,
    }


def _complexity_mismatch(profile: dict) -> bool:
    reasons = set(profile.get("fidelity_reasons") or [])
    if not reasons:
        return False
    # Modern code often consolidates historical multi-file fixes into one
    # module. Do not reject those solely because the file/module count collapsed
    # when the generated diff still preserves a comparable hunk/line surface;
    # strict P2F/P2P/golden repair verification remains the final gate.
    collapse_only = reasons <= {"source_file_count_collapsed", "module_count_collapsed"}
    if collapse_only:
        min_hunk = float(os.environ.get("PRI_L3_COLLAPSE_ONLY_MIN_HUNK_RATIO", "0.50"))
        min_line = float(os.environ.get("PRI_L3_COLLAPSE_ONLY_MIN_LINE_RATIO", "0.35"))
        generated = profile.get("generated") or {}
        if (
            float(profile.get("hunk_ratio") or 0.0) >= min_hunk
            and float(profile.get("line_change_ratio") or 0.0) >= min_line
            and int(generated.get("line_changes") or 0) >= int(os.environ.get("PRI_L3_COLLAPSE_ONLY_MIN_LINES", "8"))
        ):
            profile["fidelity_relaxed"] = True
            profile["fidelity_relaxation_reason"] = "collapsed_file_or_module_count_but_preserved_patch_surface"
            return False
    return bool(
        profile.get("fidelity_risk")
        or profile.get("simplification_risk")
        or profile.get("over_complexity_risk")
    )


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
    v2_fidelity_gate: bool = False,
    v2_require_fidelity_gate: bool = False,
    v2_gate_config: FidelityGateConfig | None = None,
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
    pass_to_pass = coerce_list(instance.get("pass_to_pass", []))
    if v2_require_fidelity_gate:
        v2_fidelity_gate = True
    v2_gate_config = v2_gate_config or FidelityGateConfig()

    short_id = iid[:60]
    print(f"\n{'━' * 70}")
    print(f"  {short_id}")
    print(f"  repo={repo}  base_commit={base_commit[:12]}  tests={len(target_tests)}")
    print(f"{'━' * 70}")

    result = {
        "instance_id": iid,
        "repo": repo,
        "base_commit": base_commit,
        "source_dataset": instance.get("source_dataset"),
        "source_instance_id": instance.get("source_instance_id") or iid,
        "injection_level": None,
        "success": False,
        "failure_reason": None,
    }
    injection_metrics = {
        "level_runtime_seconds": {},
        "level_attempts": {},
        "level_retry_count": {},
        "l3_prompt_tokens": 0,
        "l3_completion_tokens": 0,
        "l3_total_tokens": 0,
        "l3_cost_usd": 0.0,
    }
    result["injection_metrics"] = injection_metrics

    # 1. Clone/update repo
    repo_dir = repos_dir / repo.replace("/", "__")
    if repo_dir.exists() and not is_usable_git_repo(repo_dir):
        print(f"  Removing incomplete cached repo: {repo_dir}")
        shutil.rmtree(repo_dir, ignore_errors=True)
    if not repo_dir.exists():
        print(f"  Cloning {repo}...")
        ok, clone_error = clone_repo_with_retries(repo, repo_dir)
        if not ok:
            result["failure_reason"] = f"clone_failed: {clone_error}"
            print(f"  [FAIL] Clone failed: {clone_error}")
            return result
    else:
        print(f"  Fetching latest...")
        git("fetch", "origin", cwd=str(repo_dir), timeout=120)

    # Enable long paths on Windows
    git("config", "core.longpaths", "true", cwd=str(repo_dir))

    # Ensure base_commit is available
    git("fetch", "origin", base_commit, cwd=str(repo_dir), timeout=120)

    # Resolve the healthy revision without assuming every repo has origin/main.
    try:
        head_sha, healthy_ref, healthy_ref_source = resolve_healthy_revision(repo_dir, instance, base_commit)
    except Exception as exc:
        result["failure_reason"] = f"healthy_ref_resolution_failed: {exc}"
        print(f"  [FAIL] Healthy revision resolution failed: {exc}")
        return result
    print(f"  Healthy revision (h): {head_sha[:12]} ({healthy_ref_source}: {healthy_ref})")
    if healthy_ref.startswith("origin/"):
        ensure_repo_head(repo_dir, healthy_ref.removeprefix("origin/"))

    # 2. Create worktree at healthy HEAD. Use a hash of the full instance id
    # because multiple benchmark instances can share the same base commit prefix.
    run_key = _stable_id_suffix(repo, iid, base_commit)
    wt_name = f"inject-{repo.replace('/', '-')}-{run_key}"
    wt_path = (worktrees_dir / wt_name).resolve()
    branch = f"inj-{run_key}"
    print(f"  Creating worktree at {wt_path}...")
    r = None
    max_worktree_attempts = int(os.environ.get("PRI_WORKTREE_ADD_ATTEMPTS", "5"))
    for attempt in range(1, max_worktree_attempts + 1):
        cleanup_worktree_branch(repo_dir, branch, wt_path)
        r = git("worktree", "add", "-b", branch, str(wt_path), healthy_ref,
                cwd=str(repo_dir), timeout=1200)
        if r.returncode == 0:
            break
        stderr_text = r.stderr.decode(errors="replace")
        retryable = any(
            marker in stderr_text
            for marker in (
                "could not lock config file",
                "File exists",
                "already exists",
                "is already checked out",
                "unable to write upstream branch",
            )
        )
        if not retryable or attempt == max_worktree_attempts:
            break
        sleep_s = min(20.0, 1.5 * attempt)
        print(
            f"  [worktree] transient add failure on attempt "
            f"{attempt}/{max_worktree_attempts}; retrying in {sleep_s:.1f}s"
        )
        time.sleep(sleep_s)

    if r is None or r.returncode != 0:
        err_msg = r.stderr.decode(errors="replace")[:500] if r is not None else "worktree add did not run"
        result["failure_reason"] = f"worktree_failed: {err_msg}"
        rc = r.returncode if r is not None else "?"
        print(f"  [FAIL] Worktree creation failed (rc={rc}): {err_msg[:200]}")
        return result

    print(f"  Worktree created successfully")

    result["healthy_head"] = head_sha
    result["healthy_head_ref"] = healthy_ref
    result["healthy_head_ref_source"] = healthy_ref_source

    try:
        if preflight_target_tests:
            if not target_tests_to_run:
                result["injection_level"] = "Preflight_Failed"
                result["failure_reason"] = "preflight_failed: no_target_tests"
                result["preflight"] = {"ok": False, "reason": "no_target_tests"}
                print("  [preflight] Failed: no target tests")
                return result
            preflight_start = time.monotonic()
            preflight = run_target_preflight(
                wt_path, repo, target_tests_to_run, test_timeout, max_target_tests
            )
            injection_metrics["level_runtime_seconds"]["preflight"] = round(
                time.monotonic() - preflight_start, 3
            )
            injection_metrics["level_attempts"]["preflight"] = 1
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

        def record_v2_gate(level: str, diff: str, stage: str) -> dict:
            gate = evaluate_patch_pair_fidelity(
                a_patch=patch,
                b_patch=diff,
                a_fail_to_pass=fail_to_pass,
                b_fail_to_pass=target_tests_to_run,
                a_pass_to_pass=pass_to_pass,
                b_pass_to_pass=pass_to_pass,
                injection_level=level,
                config=v2_gate_config,
            )
            gate["stage"] = stage
            result.setdefault("v2_fidelity_gate_by_level", {})[level] = gate
            result["v2_fidelity_gate"] = gate
            result["v2_fidelity_gate_pass"] = bool(gate.get("pass_gate"))
            result["v2_fidelity_feedback_prompt"] = build_fidelity_feedback_prompt(gate)
            return gate

        def attempt_level3(retry_feedback_seed: str = "") -> tuple[bool, str]:
            if not enable_l3:
                return False, "Level 3 disabled"
            retry_feedback_seed = retry_feedback_seed or str(
                instance.get("v2_retry_feedback_prompt") or ""
            )
            print(f"  [L3] Attempting LLM semantic injection...")
            l3_start = time.monotonic()
            problem = instance.get("problem_statement", "")
            if target_tests_to_run:
                problem += (
                    "\n\nTarget tests that must pass on healthy HEAD and fail "
                    "after the injected bug:\n"
                    + "\n".join(f"- {test}" for test in target_tests_to_run)
                )
            ok3, l3_result, l3_meta = try_level3(
                str(wt_path),
                str(repo_dir),
                base_commit,
                patch,
                problem,
                target_tests_to_run,
                fidelity_gate=v2_fidelity_gate,
                fidelity_gate_config=v2_gate_config,
                a_fail_to_pass=fail_to_pass,
                b_fail_to_pass=target_tests_to_run,
                a_pass_to_pass=pass_to_pass,
                b_pass_to_pass=pass_to_pass,
                retry_feedback_seed=retry_feedback_seed,
            )
            result["l3_metadata"] = l3_meta
            injection_metrics["level_runtime_seconds"]["L3"] = round(
                time.monotonic() - l3_start, 3
            )
            injection_metrics["level_attempts"]["L3"] = int(l3_meta.get("attempt") or 1)
            injection_metrics["level_retry_count"]["L3"] = int(l3_meta.get("retry_count") or 0)
            injection_metrics["l3_prompt_tokens"] = int(
                l3_meta.get("total_prompt_tokens")
                or l3_meta.get("prompt_tokens")
                or 0
            )
            injection_metrics["l3_completion_tokens"] = int(
                l3_meta.get("total_completion_tokens")
                or l3_meta.get("completion_tokens")
                or 0
            )
            injection_metrics["l3_total_tokens"] = (
                injection_metrics["l3_prompt_tokens"]
                + injection_metrics["l3_completion_tokens"]
            )
            injection_metrics["l3_cost_usd"] = float(l3_meta.get("total_cost_usd") or 0.0)
            if ok3:
                print(
                    f"  [L3] Success! Diff size: {len(l3_result)} chars, "
                    f"confidence: {l3_meta.get('confidence', '?')}"
                )
                result["injection_level"] = "Level_3_LLM_Semantic"
                result["injected_diff"] = l3_result
                if l3_meta.get("fidelity"):
                    result["fidelity"] = l3_meta["fidelity"]
                    result["complexity"] = l3_meta["fidelity"].get("generated")
                if v2_fidelity_gate:
                    gate = l3_meta.get("v2_fidelity_gate") or record_v2_gate(
                        "Level_3_LLM_Semantic", l3_result, "construction_pre_verification"
                    )
                    gate["stage"] = gate.get("stage") or "construction_pre_verification"
                    result.setdefault("v2_fidelity_gate_by_level", {})[
                        "Level_3_LLM_Semantic"
                    ] = gate
                    result["v2_fidelity_gate"] = gate
                    result["v2_fidelity_gate_pass"] = bool(gate.get("pass_gate"))
                    result["v2_fidelity_feedback_prompt"] = (
                        l3_meta.get("v2_fidelity_feedback_prompt")
                        or build_fidelity_feedback_prompt(gate)
                    )
                    if v2_require_fidelity_gate and not gate.get("pass_gate"):
                        result.setdefault("v2_fidelity_gate_failures", []).append({
                            "level": "Level_3_LLM_Semantic",
                            "gate": gate,
                        })
                        result["success"] = False
                        result["injection_level"] = "Level_3_V2_Gate_Rejected"
                        result["failure_reason"] = "v2_fidelity_gate_failed"
                        return False, "v2_fidelity_gate_failed"
                result["success"] = True
            else:
                print(f"  [L3] Failed: {l3_result[:150]}")
                result["failure_reason"] = l3_result
                result["injection_level"] = "Level_3_Failed"
                result["success"] = False
            return ok3, l3_result

        def record_complexity(level: str, diff: str) -> dict:
            profile = _l3_fidelity_profile(diff, patch)
            result.setdefault("fidelity_by_level", {})[level] = profile
            result["fidelity"] = profile
            result["complexity"] = profile.get("generated")
            return profile

        def accept_or_retry_complexity(level: str, diff: str) -> bool:
            profile = record_complexity(level, diff)
            if v2_fidelity_gate:
                gate = record_v2_gate(level, diff, "construction_pre_verification")
                if gate.get("pass_gate"):
                    return True
                result.setdefault("v2_fidelity_gate_failures", []).append({
                    "level": level,
                    "gate": gate,
                })
                result["v2_gate_rejected_level"] = level
                result["v2_gate_rejection_profile"] = gate
                print(
                    "  [v2-gate] Fidelity gate failed for "
                    f"{level}: score={gate.get('score')} tags={gate.get('tags')}; "
                    "retrying with L3 if enabled..."
                )
                if enable_l3 and env_bool("PRI_RETRY_V2_GATE_WITH_L3", True):
                    git("checkout", ".", cwd=str(wt_path))
                    git("clean", "-fd", cwd=str(wt_path))
                    ok3, _ = attempt_level3(result.get("v2_fidelity_feedback_prompt", ""))
                    if ok3:
                        result["v2_retry_from_level"] = level
                        return True
                    return False
                if v2_require_fidelity_gate:
                    result["success"] = False
                    result["injection_level"] = f"{level}_V2_Gate_Rejected"
                    result["failure_reason"] = "v2_fidelity_gate_failed"
                    return False
                return True
            if not _complexity_mismatch(profile):
                return True
            result["complexity_rejected_level"] = level
            result["complexity_rejection_profile"] = profile
            print(
                "  [fidelity] Complexity mismatch for "
                f"{level}: {profile.get('fidelity_reasons')}; "
                "retrying with L3 if enabled..."
            )
            if (
                enable_l3
                and os.environ.get("PRI_RETRY_COMPLEXITY_MISMATCH_WITH_L3", "1").lower()
                in {"1", "true", "yes", "on"}
            ):
                git("checkout", ".", cwd=str(wt_path))
                git("clean", "-fd", cwd=str(wt_path))
                ok3, _ = attempt_level3()
                if ok3:
                    result["complexity_retry_from_level"] = level
                    return True
                return False
            if (
                os.environ.get("PRI_REJECT_COMPLEXITY_MISMATCH", "1").lower()
                in {"1", "true", "yes", "on"}
            ):
                result["success"] = False
                result["injection_level"] = f"{level}_Complexity_Rejected"
                result["failure_reason"] = "complexity_mismatch_with_original_patch"
                return False
            return True

        if os.environ.get("PRI_FORCE_L3_SEMANTIC") == "1":
            print("  [L3] Forced semantic retry mode enabled; skipping L1/L2.")
            attempt_level3()
            status = "PASS" if result["success"] else "FAIL"
            level = result["injection_level"] or "none"
            print(f"\n  [{status}] {short_id} ({level})")
            return result

        # 3. Try Level 1
        skip_l1 = env_bool("PRI_SKIP_L1", False)
        if skip_l1:
            print("  [L1] Skipped by PRI_SKIP_L1=1")
            ok, l1_result = False, "Level 1 skipped"
            injection_metrics["level_attempts"]["L1"] = 0
        else:
            print(f"  [L1] Attempting exact reverse apply...")
            l1_start = time.monotonic()
            ok, l1_result = try_level1(str(wt_path), patch)
            injection_metrics["level_runtime_seconds"]["L1"] = round(
                time.monotonic() - l1_start, 3
            )
            injection_metrics["level_attempts"]["L1"] = 1
        if ok:
            print(f"  [L1] Success! Diff size: {len(l1_result)} chars")
            result["injection_level"] = "Level_1_Clean_Revert"
            result["injected_diff"] = l1_result
            result["success"] = True
            accept_or_retry_complexity("Level_1_Clean_Revert", l1_result)
        else:
            print(f"  [L1] Failed: {l1_result[:100]}")

            # Reset worktree for L2
            git("checkout", ".", cwd=str(wt_path))
            git("clean", "-fd", cwd=str(wt_path))

            # 4. Try Level 2
            skip_l2 = env_bool("PRI_SKIP_L2", False)
            if skip_l2:
                print("  [L2] Skipped by PRI_SKIP_L2=1")
                ok, l2_result, l2_meta = False, "Level 2 skipped", {}
                injection_metrics["level_attempts"]["L2"] = 0
                result["l2_metadata"] = l2_meta
            else:
                print(f"  [L2] Attempting AST surgery...")
                l2_start = time.monotonic()
                ok, l2_result, l2_meta = try_level2(str(wt_path), str(repo_dir), base_commit, patch)
                injection_metrics["level_runtime_seconds"]["L2"] = round(
                    time.monotonic() - l2_start, 3
                )
                injection_metrics["level_attempts"]["L2"] = 1
                result["l2_metadata"] = l2_meta
            if ok:
                print(f"  [L2] Success! Diff size: {len(l2_result)} chars")
                result["injection_level"] = "Level_2_AST_Surgery"
                result["injected_diff"] = l2_result
                result["success"] = True
                accept_or_retry_complexity("Level_2_AST_Surgery", l2_result)
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
    parser.add_argument("--input", "-i", default="artifacts/bug-pool/candidate_pool.jsonl")
    parser.add_argument("--output", "-o", default="artifacts/bug-run/injection_results.jsonl")
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
    parser.add_argument("--v2-fidelity-gate", action="store_true",
                        help="Annotate each successful injection with the PR-INJECTOR v2 A/B complexity gate")
    parser.add_argument("--v2-require-fidelity-gate", action="store_true",
                        help="Reject successful injections that fail the v2 fidelity gate; retries with L3 when enabled")
    parser.add_argument("--v2-min-score", type=float, default=0.65)
    parser.add_argument("--v2-min-line-ratio", type=float, default=0.50)
    parser.add_argument("--v2-max-line-ratio", type=float, default=2.50)
    parser.add_argument("--v2-min-hunk-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-file-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-regression-ratio", type=float, default=0.25)
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
    v2_gate_config = FidelityGateConfig(
        min_score=args.v2_min_score,
        min_line_ratio=args.v2_min_line_ratio,
        max_line_ratio=args.v2_max_line_ratio,
        min_hunk_ratio=args.v2_min_hunk_ratio,
        min_file_ratio=args.v2_min_file_ratio,
        min_regression_ratio=args.v2_min_regression_ratio,
    )

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

        instance_start = time.monotonic()
        try:
            result = inject_instance(
                inst, repos_dir, worktrees_dir, args.timeout,
                enable_l3=args.enable_l3 and not args.no_l3,
                preflight_target_tests=args.preflight_target_tests,
                max_target_tests=args.max_target_tests,
                v2_fidelity_gate=args.v2_fidelity_gate or args.v2_require_fidelity_gate,
                v2_require_fidelity_gate=args.v2_require_fidelity_gate,
                v2_gate_config=v2_gate_config,
            )
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {
                "instance_id": inst["instance_id"],
                "repo": inst["repo"],
                "success": False,
                "failure_reason": f"exception: {str(e)[:200]}",
            }
        result["candidate_ordinal"] = i
        result["injection_duration_seconds"] = round(time.monotonic() - instance_start, 2)

        # Save diff to file, store relative path in result
        diff_content = result.pop("injected_diff", None)
        if diff_content and result.get("success"):
            diff_dir = Path(args.output).parent / "diffs"
            diff_dir.mkdir(parents=True, exist_ok=True)
            diff_path = diff_dir / f"{result['instance_id']}.diff"
            diff_path.write_text(diff_content, encoding="utf-8")
            # Store path relative to project root
            project_root = Path(__file__).resolve().parents[3]
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
             "l3_failed": 0, "l2_failed": 0, "errors": 0,
             "v2_gate_pass": 0, "v2_gate_fail": 0}
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
        if row.get("v2_fidelity_gate"):
            if row.get("v2_fidelity_gate_pass"):
                stats["v2_gate_pass"] += 1
            else:
                stats["v2_gate_fail"] += 1
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
    if args.v2_fidelity_gate or args.v2_require_fidelity_gate:
        print(f"  V2 gate pass        : {stats['v2_gate_pass']}")
        print(f"  V2 gate fail        : {stats['v2_gate_fail']}")
    print(f"  Injection rate      : {l_success/denom*100:.1f}%")
    print(f"\n  Results saved to: {args.output}")
    print(f"  Log saved to: {log_path}")


if __name__ == "__main__":
    main()
