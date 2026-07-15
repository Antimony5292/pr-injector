"""Post-hoc verification for benchmark instances.

For each instance in benchmark.jsonl, this script:
  1. Clones/updates the repo and checks out the healthy base_commit
  2. Creates an isolated worktree
  3. Applies the REVERSE of golden_patch (to recreate the buggy state)
  4. Runs target tests (extracted from test_patch) — expects FAILURE
  5. Runs full test suite — expects unrelated tests to PASS
  6. Records verification results back into a new JSONL file

Usage:
    python scripts/verify_benchmark.py [input.jsonl] [--output verified.jsonl]
                                        [--timeout 300] [--threshold 0.1]
                                        [--filter INSTANCE_ID]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_FILE_PATTERNS = [
    re.compile(r"test[_/]"),
    re.compile(r"[_/]test\."),
    re.compile(r"tests[_/]"),
    re.compile(r"_test\.py$"),
]


def is_test_file(path: str) -> bool:
    return any(p.search(path.lower()) for p in TEST_FILE_PATTERNS)


def extract_test_files_from_patch(test_patch: str) -> list[str]:
    """Extract test file paths from a unified diff."""
    files: list[str] = []
    for line in test_patch.splitlines():
        # Match 'diff --git a/path b/path' or '+++ b/path'
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            path = m.group(2)
            if is_test_file(path):
                files.append(path)
            continue
        m = re.match(r"^\+\+\+ b/(\S+)", line)
        if m:
            path = m.group(1)
            if is_test_file(path) and path not in files:
                files.append(path)
    return files


def extract_test_files_from_golden(golden_patch: str) -> list[str]:
    """Fallback: extract any test files mentioned in golden_patch."""
    files: list[str] = []
    for line in golden_patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            for p in (m.group(1), m.group(2)):
                if is_test_file(p) and p not in files:
                    files.append(p)
    return files


def reverse_patch(patch: str) -> str:
    """Reverse a unified diff: swap +/- lines and headers."""
    lines = patch.split("\n")
    result: list[str] = []
    for line in lines:
        if line.startswith("---"):
            result.append(line.replace("--- a/", "--- b/").replace("--- b/", "--- a/", 1))
        elif line.startswith("+++"):
            result.append(line.replace("+++ b/", "+++ a/").replace("+++ a/", "+++ b/", 1))
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

    # Reorder so - lines come before + lines within each group
    ordered: list[str] = []
    plus_buf: list[str] = []
    minus_buf: list[str] = []

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


def run_cmd(cmd: list[str], cwd: str, timeout: int = 300) -> dict:
    """Run a command and return parsed result dict."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout, text=False
        )
        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        output = stdout + "\n" + stderr

        # Parse pytest output
        total, failed, errors, passed = 0, 0, 0, 0
        m = re.search(r"(\d+) passed", output)
        if m:
            passed = int(m.group(1))
        m = re.search(r"(\d+) failed", output)
        if m:
            failed = int(m.group(1))
        m = re.search(r"(\d+) error", output)
        if m:
            errors = int(m.group(1))
        total = passed + failed + errors

        failed_tests = re.findall(r"FAILED\s+(\S+)", output)

        return {
            "returncode": proc.returncode,
            "total": total,
            "passed": passed,
            "failures": failed + errors,
            "failed_tests": failed_tests,
            "stdout": stdout[-2000:],  # Keep tail for debugging
            "stderr": stderr[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "total": 0, "passed": 0, "failures": 0,
                "failed_tests": [], "stdout": "", "stderr": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -1, "total": 0, "passed": 0, "failures": 0,
                "failed_tests": [], "stdout": "", "stderr": str(e)}


# ---------------------------------------------------------------------------
# Core verification logic
# ---------------------------------------------------------------------------

def verify_instance(
    instance: dict,
    repos_cache: Path,
    test_timeout: int,
    blast_threshold: float,
) -> dict:
    """Verify a single benchmark instance. Returns verification result dict."""

    iid = instance["instance_id"]
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    golden_patch = instance["golden_patch"]
    test_patch = instance.get("test_patch", "")

    print(f"\n{'─' * 60}")
    print(f"  Verifying: {iid}")
    print(f"  Repo: {repo}  Commit: {base_commit[:12]}")
    print(f"{'─' * 60}")

    # 1. Identify target test files
    target_tests = extract_test_files_from_patch(test_patch)
    if not target_tests:
        target_tests = extract_test_files_from_golden(golden_patch)
    if not target_tests:
        print(f"  [SKIP] No target test files found")
        return _make_result(iid, skip_reason="no_target_tests")

    print(f"  Target tests: {target_tests}")

    # 2. Clone / update repo
    repo_dir = repos_cache / repo.replace("/", "__")
    if not repo_dir.exists():
        print(f"  Cloning {repo}...")
        ret = subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", str(repo_dir)],
            capture_output=True, timeout=600,
        )
        if ret.returncode != 0:
            print(f"  [FAIL] Clone failed")
            return _make_result(iid, skip_reason="clone_failed")
    else:
        print(f"  Updating {repo}...")
        subprocess.run(
            ["git", "fetch", "origin"], cwd=str(repo_dir),
            capture_output=True, timeout=120,
        )

    # 3. Create a temporary worktree at base_commit
    worktree_dir = repos_cache / "worktrees" / f"verify-{iid}"
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)

    # Ensure base_commit exists
    subprocess.run(
        ["git", "fetch", "origin", base_commit],
        cwd=str(repo_dir), capture_output=True, timeout=120,
    )

    branch_name = f"verify-{iid}"
    # Clean up stale branch if exists
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=str(repo_dir), capture_output=True,
    )

    ret = subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(worktree_dir), base_commit],
        cwd=str(repo_dir), capture_output=True, timeout=60,
    )
    if ret.returncode != 0:
        print(f"  [FAIL] Worktree creation failed: {ret.stderr.decode(errors='replace')[:200]}")
        return _make_result(iid, skip_reason="worktree_failed")

    try:
        start_time = time.monotonic()

        # 4. Sanity check: run target tests on healthy revision (should PASS)
        print(f"  [1/4] Running target tests on healthy revision...")
        healthy_result = run_cmd(
            ["python", "-m", "pytest", "-x", "--tb=short", "-q"] + target_tests,
            cwd=str(worktree_dir), timeout=test_timeout,
        )
        healthy_pass = healthy_result["returncode"] == 0
        print(f"        returncode={healthy_result['returncode']}  "
              f"passed={healthy_result['passed']}  failures={healthy_result['failures']}  "
              f"-> {'PASS' if healthy_pass else 'FAIL'}")

        if not healthy_pass:
            print(f"  [WARN] Target tests already fail on healthy revision!")

        # 5. Apply reverse of golden_patch to create buggy state
        print(f"  [2/4] Applying bug injection (reverse golden_patch)...")
        injection_patch = reverse_patch(golden_patch)

        patch_proc = subprocess.run(
            ["git", "apply", "--allow-empty"],
            cwd=str(worktree_dir),
            input=injection_patch.encode(),
            capture_output=True, timeout=30,
        )
        if patch_proc.returncode != 0:
            # Try with --3way for fuzzy matching
            patch_proc = subprocess.run(
                ["git", "apply", "--3way"],
                cwd=str(worktree_dir),
                input=injection_patch.encode(),
                capture_output=True, timeout=30,
            )
        if patch_proc.returncode != 0:
            err = patch_proc.stderr.decode(errors="replace")[:300]
            print(f"  [FAIL] Patch apply failed: {err}")
            return _make_result(iid, skip_reason="patch_apply_failed")

        print(f"        Patch applied successfully")

        # 6. Run target tests on buggy revision (should FAIL)
        print(f"  [3/4] Running target tests on buggy revision...")
        buggy_result = run_cmd(
            ["python", "-m", "pytest", "-x", "--tb=short", "-q"] + target_tests,
            cwd=str(worktree_dir), timeout=test_timeout,
        )
        target_tests_failed = buggy_result["returncode"] != 0
        print(f"        returncode={buggy_result['returncode']}  "
              f"passed={buggy_result['passed']}  failures={buggy_result['failures']}  "
              f"-> {'FAIL (good!)' if target_tests_failed else 'PASS (bad!)'}")

        # 7. Run full test suite to check blast radius
        print(f"  [4/4] Running full test suite (blast radius check)...")
        full_result = run_cmd(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=str(worktree_dir), timeout=test_timeout,
        )

        duration = time.monotonic() - start_time

        total_tests = full_result["total"]
        total_failures = full_result["failures"]
        target_failure_count = buggy_result["failures"]
        unrelated_failures = max(0, total_failures - target_failure_count)

        if total_tests > 0:
            unrelated_rate = unrelated_failures / total_tests
            unrelated_passed = unrelated_rate <= blast_threshold
        else:
            unrelated_passed = True

        blast_ok = target_tests_failed and unrelated_passed

        print(f"        total={total_tests}  failures={total_failures}  "
              f"unrelated_failures={unrelated_failures}")
        print(f"        blast_radius_ok={blast_ok}")

        verification = {
            "target_tests_failed": target_tests_failed,
            "unrelated_tests_passed": unrelated_passed,
            "blast_radius_ok": blast_ok,
            "healthy_tests_pass": healthy_pass,
            "target_test_names": target_tests,
            "failed_test_names": buggy_result.get("failed_tests", []),
            "total_tests_run": total_tests,
            "total_failures": total_failures,
            "unrelated_failures": unrelated_failures,
            "test_duration_seconds": round(duration, 2),
        }

        status = "PASS" if blast_ok else "FAIL"
        print(f"\n  Result: [{status}] {iid}")
        return _make_result(iid, verification=verification)

    finally:
        # Clean up worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_dir)],
            cwd=str(repo_dir), capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=str(repo_dir), capture_output=True,
        )


def _make_result(
    instance_id: str,
    verification: dict | None = None,
    skip_reason: str | None = None,
) -> dict:
    return {
        "instance_id": instance_id,
        "verification": verification,
        "skip_reason": skip_reason,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Verify benchmark instances")
    parser.add_argument("input", nargs="?", default="benchmark_dataset/benchmark.jsonl",
                        help="Input JSONL file")
    parser.add_argument("--output", "-o", default=None,
                        help="Output JSONL with verification results (default: <input>_verified.jsonl)")
    parser.add_argument("--timeout", "-t", type=int, default=300,
                        help="Test timeout in seconds (default: 300)")
    parser.add_argument("--threshold", type=float, default=0.1,
                        help="Blast radius threshold (default: 0.1)")
    parser.add_argument("--filter", "-f", type=str, default=None,
                        help="Only verify instances matching this substring")
    parser.add_argument("--repos-cache", type=str, default=".pri-workspace",
                        help="Directory for repo clones (default: .pri-workspace)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    output_path = args.output or str(input_path).replace(".jsonl", "_verified.jsonl")
    repos_cache = Path(args.repos_cache)
    repos_cache.mkdir(parents=True, exist_ok=True)
    (repos_cache / "worktrees").mkdir(exist_ok=True)

    # Load instances
    instances = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(json.loads(line))

    # Deduplicate by instance_id (keep first occurrence)
    seen = set()
    unique = []
    for inst in instances:
        iid = inst["instance_id"]
        if iid not in seen:
            seen.add(iid)
            unique.append(inst)
    if len(unique) < len(instances):
        print(f"Deduplicated: {len(instances)} -> {len(unique)} instances")
    instances = unique

    # Apply filter
    if args.filter:
        instances = [i for i in instances if args.filter in i["instance_id"]]

    print(f"Instances to verify: {len(instances)}")
    print(f"Output: {output_path}")
    print(f"Timeout: {args.timeout}s  Threshold: {args.threshold}")

    # Run verification
    results = []
    pass_count = 0
    fail_count = 0
    skip_count = 0

    for i, inst in enumerate(instances, 1):
        print(f"\n[{i}/{len(instances)}]", end="")
        result = verify_instance(inst, repos_cache, args.timeout, args.threshold)

        # Merge verification back into instance
        inst_out = dict(inst)
        inst_out["verification"] = result.get("verification")
        if result.get("skip_reason"):
            inst_out["skip_reason"] = result["skip_reason"]
            skip_count += 1
        elif result.get("verification", {}).get("blast_radius_ok"):
            pass_count += 1
        else:
            fail_count += 1

        results.append(inst_out)

        # Write incrementally (so partial results are saved on crash)
        with open(output_path, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Summary
    total = len(instances)
    print(f"\n{'=' * 60}")
    print(f"  Verification Summary")
    print(f"{'=' * 60}")
    print(f"  Total    : {total}")
    print(f"  Passed   : {pass_count} ({pass_count/total*100:.1f}%)" if total else "")
    print(f"  Failed   : {fail_count}")
    print(f"  Skipped  : {skip_count}")
    print(f"  Output   : {output_path}")


if __name__ == "__main__":
    main()
