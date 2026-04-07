"""Batch injection experiment for SWE-bench Pro instances.

For each instance in the sampled JSONL:
  1. Clone/update the repo, checkout latest main (healthy revision h)
  2. Attempt Level 1: reverse-apply the patch via `git apply -R`
  3. If L1 fails, attempt Level 2: AST surgery (match functions from base_commit)
  4. If L2 fails, attempt Level 3: LLM semantic injection
  5. If injection succeeds, verify: target tests should FAIL, unrelated tests should PASS
  6. Record results to output JSONL

Usage:
    python scripts/inject_swebench_pro.py [--input FILE] [--output FILE]
                                           [--timeout 300] [--skip-verify]
                                           [--filter INSTANCE_ID]
                                           [--max N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ── Helpers ──────────────────────────────────────────────────────────────────

PYTHON = sys.executable

TEST_FILE_PATTERNS = [
    re.compile(r"test[_/]"),
    re.compile(r"[_/]test\."),
    re.compile(r"tests[_/]"),
    re.compile(r"_test\.py$"),
]


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


def run_pytest(cwd: str, test_files: list[str], timeout: int = 300) -> dict:
    """Run pytest on specific test files."""
    cmd = [PYTHON, "-m", "pytest", "-x", "--tb=short", "-q"] + test_files
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout)
        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        output = stdout + "\n" + stderr

        passed, failed, errors = 0, 0, 0
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m:
            errors = int(m.group(1))

        return {
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed + errors,
            "total": passed + failed + errors,
            "output_tail": output[-1500:],
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "output_tail": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "output_tail": str(e)}


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


# ── Level 2: AST surgery ─────────────────────────────────────────────────────

def try_level2(worktree: str, repo_dir: str, base_commit: str, patch: str) -> tuple[bool, str]:
    """Attempt Level 2: AST-guided structural reversion.

    For each source file in the patch, get the pre-fix version from base_commit,
    parse both with AST, match functions, and replace bodies.
    """
    import ast as pyast

    source_files = extract_source_files_from_patch(patch)
    if not source_files:
        return False, "No source files in patch"

    any_changed = False
    for filepath in source_files:
        target_path = Path(worktree) / filepath
        if not target_path.exists():
            continue

        # Get pre-fix version from base_commit
        try:
            prefix_content = git_text("show", f"{base_commit}:{filepath}", cwd=repo_dir, timeout=30)
        except Exception:
            continue

        if not prefix_content.strip():
            continue

        current_content = target_path.read_text(encoding="utf-8", errors="replace")

        # Parse both versions
        try:
            prefix_tree = pyast.parse(prefix_content)
            current_tree = pyast.parse(current_content)
        except SyntaxError:
            continue

        # Extract top-level functions/methods
        prefix_funcs = _extract_functions(prefix_content, prefix_tree)
        current_funcs = _extract_functions(current_content, current_tree)

        # Find functions that exist in both but differ
        new_content = current_content
        replaced = False
        for name, prefix_src in prefix_funcs.items():
            if name in current_funcs and current_funcs[name] != prefix_src:
                # Replace current function body with pre-fix version
                new_content = new_content.replace(current_funcs[name], prefix_src)
                replaced = True

        if replaced:
            # Validate syntax
            try:
                pyast.parse(new_content)
            except SyntaxError:
                continue
            target_path.write_text(new_content, encoding="utf-8")
            any_changed = True

    if any_changed:
        diff = git_text("diff", cwd=worktree)
        if diff.strip():
            return True, diff

    return False, "Level 2: no functions matched or replaced"


def _extract_functions(source: str, tree) -> dict[str, str]:
    """Extract function/method source text keyed by qualified name."""
    import ast as pyast
    lines = source.splitlines(keepends=True)
    funcs = {}

    for node in pyast.walk(tree):
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            name = node.name
            # Get parent class name if it's a method
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
            func_src = "".join(lines[start:end])
            funcs[name] = func_src

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
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    # Load config from .env
    import dotenv
    dotenv.load_dotenv()

    endpoint = os.environ.get("PRI_AZURE_ENDPOINT", "")
    deployment = os.environ.get("PRI_AZURE_DEPLOYMENT", "gpt-5")
    api_version = os.environ.get("PRI_AZURE_API_VERSION", "2024-12-01-preview")

    if not endpoint:
        return False, "Level 3: no Azure endpoint configured", {}

    # Build token provider
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    client = AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )

    # Collect current source files touched by the patch
    source_files = extract_source_files_from_patch(patch)
    current_files: dict[str, str] = {}
    for filepath in source_files:
        target_path = Path(worktree) / filepath
        if target_path.exists():
            content = target_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > 15000:
                content = content[:15000] + "\n... (truncated)"
            current_files[filepath] = content

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
        " specific bug being reintroduced.\n7. Start your response with 'diff --git'."
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

## Task

The original PR fixed a bug described above. The codebase has since evolved.
Your task: Create a unified diff that reintroduces the SAME logical bug into the CURRENT code.

The bug should:
- Cause the same category of failure as the original
- Be in the equivalent code location (which may have moved or been refactored)
- Be subtle enough that it's not immediately obvious

Output ONLY the unified diff, starting with "diff --git"."""

    print(f"  [L3] Calling LLM ({deployment})...")
    print(f"       Current files available: {list(current_files.keys())}")

    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=4096,
        )

        content = response.choices[0].message.content or ""
        finish_reason = response.choices[0].finish_reason
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0

        meta = {
            "model": response.model,
            "finish_reason": finish_reason,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "response_length": len(content),
        }

        print(f"       Response: {len(content)} chars, finish={finish_reason}, "
              f"tokens={prompt_tokens}+{completion_tokens}")

        if not content.strip():
            return False, f"Level 3: LLM returned empty response (finish={finish_reason})", meta

        # Extract diff from response
        diff = _extract_diff(content)
        if not diff:
            print(f"       Response preview: {content[:300]}")
            return False, "Level 3: no valid diff in LLM response", meta

        # Validate diff syntax
        if "@@" not in diff:
            return False, "Level 3: LLM diff missing hunk headers", meta

        # Try to apply
        proc = subprocess.run(
            ["git", "apply", "--check"],
            cwd=worktree, input=diff.encode(),
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace")[:200]
            print(f"       Patch check failed: {err}")
            return False, f"Level 3: LLM diff doesn't apply cleanly: {err}", meta

        # Actually apply
        subprocess.run(
            ["git", "apply"],
            cwd=worktree, input=diff.encode(),
            capture_output=True, timeout=30,
        )
        applied_diff = git_text("diff", cwd=worktree)
        if applied_diff.strip():
            meta["confidence"] = _estimate_confidence(applied_diff, patch)
            return True, applied_diff, meta

        return False, "Level 3: patch applied but produced no diff", meta

    except Exception as e:
        return False, f"Level 3: LLM call failed: {str(e)[:200]}", {"error": str(e)[:300]}


def _extract_diff(response: str) -> str | None:
    """Extract unified diff from LLM response."""
    # Try bare diff
    m = re.search(r"(diff --git\s+.*)", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Try code block
    m = re.search(r"```(?:diff)?\s*\n(.*?)```", response, re.DOTALL)
    if m:
        block = m.group(1).strip()
        if "diff --git" in block or "@@" in block:
            return block
    return None


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
    skip_verify: bool,
    enable_l3: bool = False,
) -> dict:
    """Attempt injection for a single SWE-bench Pro instance."""

    iid = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    patch = instance["patch"]
    test_patch = instance.get("test_patch", "")

    # Determine target test files
    target_tests = instance.get("selected_test_files_to_run", [])
    if isinstance(target_tests, str):
        target_tests = json.loads(target_tests) if target_tests.startswith("[") else [target_tests]
    if not target_tests:
        target_tests = extract_test_files_from_patch(test_patch)

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
        "verification": None,
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

    # 2. Create worktree at healthy HEAD
    wt_name = f"inject-{repo.replace('/', '-')}-{base_commit[:8]}"
    wt_path = (worktrees_dir / wt_name).resolve()
    if wt_path.exists():
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir))
        shutil.rmtree(wt_path, ignore_errors=True)

    branch = f"inj-{base_commit[:8]}"
    git("branch", "-D", branch, cwd=str(repo_dir))
    print(f"  Creating worktree at {wt_path}...")
    r = git("worktree", "add", "-b", branch, str(wt_path), f"origin/{default_branch}",
            cwd=str(repo_dir), timeout=1200)
    if r.returncode != 0:
        # Check if it's just a stale branch issue
        stderr_text = r.stderr.decode(errors="replace")
        if "already exists" in stderr_text:
            git("branch", "-D", branch, cwd=str(repo_dir))
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
            ok, l2_result = try_level2(str(wt_path), str(repo_dir), base_commit, patch)
            if ok:
                print(f"  [L2] Success! Diff size: {len(l2_result)} chars")
                result["injection_level"] = "Level_2_AST_Surgery"
                result["injected_diff"] = l2_result
                result["success"] = True
            else:
                print(f"  [L2] Failed: {l2_result[:100]}")

                # Reset worktree for L3
                git("checkout", ".", cwd=str(wt_path))
                git("clean", "-fd", cwd=str(wt_path))

                # 5. Try Level 3 (LLM)
                if enable_l3:
                    print(f"  [L3] Attempting LLM semantic injection...")
                    problem = instance.get("problem_statement", "")
                    ok, l3_result, l3_meta = try_level3(
                        str(wt_path), str(repo_dir), base_commit, patch, problem
                    )
                    result["l3_metadata"] = l3_meta
                    if ok:
                        print(f"  [L3] Success! Diff size: {len(l3_result)} chars, "
                              f"confidence: {l3_meta.get('confidence', '?')}")
                        result["injection_level"] = "Level_3_LLM_Semantic"
                        result["injected_diff"] = l3_result
                        result["success"] = True
                    else:
                        print(f"  [L3] Failed: {l3_result[:150]}")
                        result["failure_reason"] = l3_result
                        result["injection_level"] = "Level_3_Failed"
                else:
                    result["failure_reason"] = l2_result
                    result["injection_level"] = "Level_3_Needed"

        # 5. Verification
        if result["success"] and not skip_verify and target_tests:
            print(f"  [Verify] Running target tests ({target_tests})...")
            # Filter to existing test files
            existing_tests = [t for t in target_tests if (wt_path / t).exists()]
            if not existing_tests:
                print(f"  [Verify] No target test files exist on healthy revision")
                result["verification"] = {"skip_reason": "test_files_missing"}
            else:
                test_result = run_pytest(str(wt_path), existing_tests, timeout=test_timeout)
                target_failed = test_result["returncode"] != 0

                result["verification"] = {
                    "target_tests_failed": target_failed,
                    "target_passed": test_result["passed"],
                    "target_failures": test_result["failed"],
                    "target_total": test_result["total"],
                }

                if target_failed:
                    print(f"  [Verify] Target tests FAIL (good!) - "
                          f"passed={test_result['passed']} failed={test_result['failed']}")
                else:
                    print(f"  [Verify] Target tests PASS (bad - injection didn't break them)")
                    result["success"] = False
                    result["failure_reason"] = "verification_failed_tests_still_pass"

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
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--enable-l3", action="store_true",
                        help="Enable Level 3 LLM injection for L1+L2 failures")
    parser.add_argument("--filter", "-f", type=str, default=None)
    parser.add_argument("--max", "-n", type=int, default=None)
    parser.add_argument("--repos-dir", default=".pri-workspace/repos")
    parser.add_argument("--worktrees-dir", default=".pri-workspace/worktrees")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

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

    # Run injection
    results = []
    stats = {"total": 0, "l1_success": 0, "l2_success": 0, "l3_success": 0,
             "l3_failed": 0, "l3_needed": 0,
             "verified_ok": 0, "verified_fail": 0, "errors": 0}

    for i, inst in enumerate(instances, 1):
        print(f"\n[{i}/{len(instances)}]", end="")
        stats["total"] += 1

        try:
            result = inject_instance(
                inst, repos_dir, worktrees_dir, args.timeout, args.skip_verify,
                enable_l3=args.enable_l3,
            )
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {
                "instance_id": inst["instance_id"],
                "repo": inst["repo"],
                "success": False,
                "failure_reason": f"exception: {str(e)[:200]}",
            }
            stats["errors"] += 1

        # Update stats
        level = result.get("injection_level")
        if level == "Level_1_Clean_Revert":
            stats["l1_success"] += 1
        elif level == "Level_2_AST_Surgery":
            stats["l2_success"] += 1
        elif level == "Level_3_LLM_Semantic":
            stats["l3_success"] += 1
        elif level == "Level_3_Failed":
            stats["l3_failed"] += 1
        elif level == "Level_3_Needed":
            stats["l3_needed"] += 1

        v = result.get("verification", {})
        if v and v.get("target_tests_failed"):
            stats["verified_ok"] += 1
        elif v and not v.get("skip_reason"):
            stats["verified_fail"] += 1

        # Don't write large diffs to results
        result.pop("injected_diff", None)
        results.append(result)

        # Incremental save
        with open(args.output, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Print summary
    t = stats["total"]
    l_success = stats["l1_success"] + stats["l2_success"] + stats["l3_success"]
    print(f"\n\n{'=' * 70}")
    print(f"  INJECTION EXPERIMENT SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total instances     : {t}")
    print(f"  Level 1 success     : {stats['l1_success']} ({stats['l1_success']/t*100:.1f}%)")
    print(f"  Level 2 success     : {stats['l2_success']} ({stats['l2_success']/t*100:.1f}%)")
    print(f"  Level 3 success     : {stats['l3_success']} ({stats['l3_success']/t*100:.1f}%)")
    print(f"  Level 3 failed      : {stats['l3_failed']}")
    print(f"  Level 3 needed      : {stats['l3_needed']}")
    print(f"  Errors              : {stats['errors']}")
    print(f"  Injection rate      : {l_success/t*100:.1f}%")
    if stats["verified_ok"] + stats["verified_fail"] > 0:
        v_total = stats["verified_ok"] + stats["verified_fail"]
        print(f"  Verified OK         : {stats['verified_ok']}/{v_total} ({stats['verified_ok']/v_total*100:.1f}%)")
    print(f"\n  Results saved to: {args.output}")


if __name__ == "__main__":
    main()
