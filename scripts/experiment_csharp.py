"""End-to-end experiment pipeline for C# repository (Humanizer).

Phases:
  1. Sample: Mine merged PRs from GitHub that touch both src/ and tests/ .cs files
  2. Inject: Reverse-apply source-only patches (Level 1 git apply -R)
  3. Verify: Confirm pass-to-fail behavior with dotnet test

Usage:
    python scripts/experiment_csharp.py --phase all
    python scripts/experiment_csharp.py --phase sample --max 10
    python scripts/experiment_csharp.py --phase inject
    python scripts/experiment_csharp.py --phase verify
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_OWNER = "App-vNext"
REPO_NAME = "Polly"
REPO_FULL = f"{REPO_OWNER}/{REPO_NAME}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_DIR = PROJECT_ROOT / ".pri-workspace" / "repos" / f"{REPO_OWNER}__{REPO_NAME}"
WORKTREES_DIR = PROJECT_ROOT / ".pri-workspace" / "worktrees"
OUTPUT_DIR = PROJECT_ROOT / "experiments" / "csharp_polly"

SAMPLED_FILE = OUTPUT_DIR / "sampled.jsonl"
INJECTION_FILE = OUTPUT_DIR / "injection_results.jsonl"
VERIFICATION_FILE = OUTPUT_DIR / "verification_results.jsonl"

TEST_PROJECT = "test/Polly.Core.Tests/Polly.Core.Tests.csproj"


def _detect_test_projects(test_files: list[str], worktree: str) -> list[str]:
    """Detect which .csproj test projects to run based on changed test files.

    Maps test file paths like 'test/Polly.Specs/Timeout/TimeoutSpecs.cs'
    to 'test/Polly.Specs/Polly.Specs.csproj'.
    """
    projects = set()
    for tf in test_files:
        # Extract the test project directory: test/<ProjectDir>/...
        parts = tf.split("/")
        if len(parts) >= 2:
            project_dir = "/".join(parts[:2])  # e.g., "test/Polly.Specs"
            # Find .csproj in that directory
            proj_dir = Path(worktree) / project_dir
            if proj_dir.is_dir():
                for csproj in proj_dir.glob("*.csproj"):
                    projects.add(str(csproj.relative_to(worktree)))

    # Fallback to default test project if nothing detected
    if not projects:
        projects.add(TEST_PROJECT)

    return sorted(projects)


def run_dotnet_test_multi(
    cwd: str,
    test_projects: list[str],
    timeout: int = 300,
) -> dict:
    """Run dotnet test on multiple test projects and aggregate results."""
    total_passed = 0
    total_failed = 0
    total_skipped = 0
    total_total = 0
    worst_rc = 0
    all_output = []

    for proj in test_projects:
        result = run_dotnet_test(cwd, test_project=proj, timeout=timeout)
        total_passed += result["passed"]
        total_failed += result["failed"]
        total_skipped += result.get("skipped", 0)
        total_total += result["total"]
        if result["returncode"] != 0:
            worst_rc = result["returncode"]
        all_output.append(f"--- {proj} ---\n{result['output_tail']}")

    return {
        "returncode": worst_rc,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        "total": total_total,
        "output_tail": "\n".join(all_output)[-2000:],
        "projects_tested": test_projects,
    }

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _load_env() -> None:
    """Load .env file using dotenv if available."""
    try:
        import dotenv
        dotenv.load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())


def _get_github_token() -> str:
    token = os.environ.get("PRI_GITHUB_TOKEN", "")
    if not token:
        print("[ERROR] PRI_GITHUB_TOKEN not set. Put it in .env or export it.")
        sys.exit(1)
    return token


def _dotnet_env() -> dict[str, str]:
    """Return an env dict with dotnet on the PATH."""
    env = os.environ.copy()
    dotnet_home = os.path.join(os.path.expanduser("~"), ".dotnet")
    env["DOTNET_ROOT"] = dotnet_home
    env["PATH"] = dotnet_home + os.pathsep + env.get("PATH", "")
    # Suppress .NET telemetry & first-run noise
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"
    return env


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def git(*args: str, cwd: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, timeout=timeout, text=False,
    )


def git_text(*args: str, cwd: str, timeout: int = 600) -> str:
    r = git(*args, cwd=cwd, timeout=timeout)
    return r.stdout.decode(errors="replace")


# ---------------------------------------------------------------------------
# Patch helpers
# ---------------------------------------------------------------------------


def _files_from_diff(diff_text: str) -> list[str]:
    """Extract all file paths from a unified diff."""
    files: list[str] = []
    for line in diff_text.splitlines():
        m = re.match(r"^diff --git a/\S+ b/(\S+)", line)
        if m:
            files.append(m.group(1))
    return list(dict.fromkeys(files))


def _is_source_cs(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".cs")


def _is_test_cs(path: str) -> bool:
    return (path.startswith("test/") or path.startswith("tests/")) and path.endswith(".cs")


def _split_patch_source_only(patch: str) -> str:
    """Return only the hunks that touch src/ .cs files (exclude tests)."""
    lines = patch.split("\n")
    result: list[str] = []
    include = False
    for line in lines:
        if line.startswith("diff --git"):
            m = re.match(r"^diff --git a/\S+ b/(\S+)", line)
            include = bool(m and _is_source_cs(m.group(1)))
        if include:
            result.append(line)
    output = "\n".join(result)
    if output and not output.endswith("\n"):
        output += "\n"
    return output


# ---------------------------------------------------------------------------
# dotnet test runner
# ---------------------------------------------------------------------------


def run_dotnet_test(
    cwd: str,
    test_project: str = TEST_PROJECT,
    timeout: int = 300,
    extra_args: list[str] | None = None,
) -> dict:
    """Run dotnet test and parse output.

    Returns dict with keys: returncode, passed, failed, total, output_tail.
    """
    cmd = ["dotnet", "test", test_project, "-v", "q", "--nologo",
           "/p:CollectCoverage=false"]
    if extra_args:
        cmd.extend(extra_args)

    env = _dotnet_env()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env,
        )
        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        output = stdout + "\n" + stderr

        passed, failed, skipped, total = 0, 0, 0, 0

        # dotnet test summary line patterns:
        #   "Passed! - Failed: 0, Passed: 123, Skipped: 0, Total: 123"
        #   "Failed! - Failed: 2, Passed: 121, Skipped: 0, Total: 123"
        #   Or Microsoft.Testing.Platform style:
        #   "Tests succeeded: ..."  /  "Tests failed: ..."
        m = re.search(r"Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+),\s*Total:\s*(\d+)", output)
        if m:
            failed = int(m.group(1))
            passed = int(m.group(2))
            skipped = int(m.group(3))
            total = int(m.group(4))
        else:
            # Newer Microsoft.Testing.Platform format
            m2 = re.search(r"Tests succeeded:\s*(\d+)", output)
            if m2:
                passed = int(m2.group(1))
            m2 = re.search(r"Tests failed:\s*(\d+)", output)
            if m2:
                failed = int(m2.group(1))
            # Also try: "total: N"
            m2 = re.search(r"[Tt]otal:\s*(\d+)", output)
            if m2:
                total = int(m2.group(1))
            else:
                total = passed + failed + skipped

            # Fallback: count lines like "Passed  TestName"
            if passed == 0 and failed == 0:
                passed = len(re.findall(r"^\s*Passed\s+", output, re.MULTILINE))
                failed = len(re.findall(r"^\s*Failed\s+", output, re.MULTILINE))
                total = passed + failed

        return {
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "total": total,
            "output_tail": output[-2000:],
        }

    except subprocess.TimeoutExpired:
        return {
            "returncode": -1, "passed": 0, "failed": 0, "skipped": 0,
            "total": 0, "output_tail": "TIMEOUT",
        }
    except Exception as e:
        return {
            "returncode": -1, "passed": 0, "failed": 0, "skipped": 0,
            "total": 0, "output_tail": str(e),
        }


def run_dotnet_build(cwd: str, timeout: int = 300) -> bool:
    """Run dotnet build. Returns True on success."""
    env = _dotnet_env()
    proc = subprocess.run(
        ["dotnet", "build", "--nologo", "-v", "q"],
        cwd=cwd, capture_output=True, timeout=timeout, env=env,
    )
    return proc.returncode == 0


def run_dotnet_restore(cwd: str, timeout: int = 300) -> bool:
    """Run dotnet restore. Returns True on success."""
    env = _dotnet_env()
    proc = subprocess.run(
        ["dotnet", "restore", "--nologo"],
        cwd=cwd, capture_output=True, timeout=timeout, env=env,
    )
    return proc.returncode == 0


# ============================================================================
# Phase 1: Sample PRs
# ============================================================================


def phase_sample(max_instances: int = 10) -> list[dict]:
    """Sample qualifying PRs from GitHub."""
    import httpx

    token = _get_github_token()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    api = f"https://api.github.com/repos/{REPO_FULL}"

    print(f"\n{'=' * 70}")
    print(f"  PHASE 1: SAMPLE PRs from {REPO_FULL}")
    print(f"{'=' * 70}")

    # Fetch merged PRs (paginated, up to 200)
    all_prs: list[dict] = []
    page = 1
    per_page = 100
    while len(all_prs) < 500:
        print(f"  Fetching PRs page {page}...")
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{api}/pulls",
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                },
                headers=headers,
            )
            resp.raise_for_status()
            prs = resp.json()

        if not prs:
            break

        for pr in prs:
            if pr.get("merged_at"):
                all_prs.append(pr)

        page += 1
        if len(prs) < per_page:
            break

    print(f"  Total merged PRs fetched: {len(all_prs)}")

    # Filter qualifying PRs
    qualifying: list[dict] = []

    for i, pr in enumerate(all_prs):
        pr_num = pr["number"]
        pr_title = pr["title"]

        if len(qualifying) >= max_instances:
            break

        # Fetch the diff (use .diff URL which works for large PRs too)
        try:
            import time as _time
            for attempt in range(3):
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    resp = client.get(
                        f"https://github.com/{REPO_FULL}/pull/{pr_num}.diff",
                    )
                if resp.status_code == 429:
                    wait = 10 * (attempt + 1)
                    print(f"    PR #{pr_num}: rate limited, waiting {wait}s...")
                    _time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            else:
                print(f"    PR #{pr_num}: rate limited after retries, skipping")
                continue
            diff = resp.text
            _time.sleep(0.5)  # Avoid rate limiting
        except Exception as e:
            print(f"    PR #{pr_num}: failed to fetch diff: {e}")
            continue

        files = _files_from_diff(diff)
        src_files = [f for f in files if _is_source_cs(f)]
        test_files = [f for f in files if _is_test_cs(f)]

        if not src_files or not test_files:
            continue

        # Get merge commit SHA
        merge_sha = pr.get("merge_commit_sha", "")

        # Get base SHA (the parent before merge)
        base_sha = ""
        if merge_sha:
            try:
                with httpx.Client(timeout=30) as client:
                    resp = client.get(
                        f"{api}/commits/{merge_sha}",
                        headers=headers,
                    )
                    resp.raise_for_status()
                    commit_data = resp.json()
                    parents = commit_data.get("parents", [])
                    if parents:
                        base_sha = parents[0]["sha"]
            except Exception:
                pass

        record = {
            "pr_number": pr_num,
            "title": pr_title,
            "merge_commit_sha": merge_sha,
            "base_sha": base_sha,
            "patch": diff,
            "source_files": src_files,
            "test_files": test_files,
            "merged_at": pr.get("merged_at", ""),
            "html_url": pr.get("html_url", ""),
        }
        qualifying.append(record)
        print(f"    PR #{pr_num}: {pr_title[:60]}  "
              f"[{len(src_files)} src, {len(test_files)} test]")

    print(f"\n  Qualifying PRs: {len(qualifying)}")

    # Save
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(SAMPLED_FILE, "w", encoding="utf-8") as f:
        for rec in qualifying:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  Saved to: {SAMPLED_FILE}")

    # Summary
    print(f"\n  Summary:")
    for rec in qualifying:
        print(f"    PR #{rec['pr_number']}: {rec['title'][:50]}  "
              f"src={len(rec['source_files'])} test={len(rec['test_files'])}")

    return qualifying


# ============================================================================
# Phase 2: Inject (Level 1 only)
# ============================================================================


def phase_inject(max_instances: int | None = None) -> list[dict]:
    """Inject bugs via git apply -R of source-only patches."""

    print(f"\n{'=' * 70}")
    print(f"  PHASE 2: INJECT (Level 1 - git apply -R)")
    print(f"{'=' * 70}")

    if not SAMPLED_FILE.exists():
        print(f"  [ERROR] Sampled file not found: {SAMPLED_FILE}")
        print(f"  Run --phase sample first.")
        sys.exit(1)

    # Load sampled instances
    instances: list[dict] = []
    with open(SAMPLED_FILE, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                instances.append(json.loads(line))

    if max_instances:
        instances = instances[:max_instances]

    print(f"  Instances to process: {len(instances)}")

    # Ensure repo exists
    if not REPO_DIR.exists():
        print(f"  [ERROR] Repo not found: {REPO_DIR}")
        sys.exit(1)

    # Get latest HEAD of default branch
    git("fetch", "origin", cwd=str(REPO_DIR), timeout=120)
    default_branch = "main"
    for bn in ("main", "master"):
        r = git("rev-parse", "--verify", f"origin/{bn}", cwd=str(REPO_DIR))
        if r.returncode == 0:
            default_branch = bn
            break
    head_sha = git_text("rev-parse", f"origin/{default_branch}", cwd=str(REPO_DIR)).strip()
    print(f"  Latest HEAD: {head_sha[:12]} ({default_branch})")

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    stats = {"total": 0, "success": 0, "failed": 0, "errors": 0}

    for idx, inst in enumerate(instances, 1):
        pr_num = inst["pr_number"]
        stats["total"] += 1

        print(f"\n  [{idx}/{len(instances)}] PR #{pr_num}: {inst['title'][:50]}")

        result = {
            "pr_number": pr_num,
            "title": inst["title"],
            "merge_commit_sha": inst["merge_commit_sha"],
            "base_sha": inst["base_sha"],
            "source_files": inst["source_files"],
            "test_files": inst["test_files"],
            "healthy_head": head_sha,
            "injection_level": None,
            "success": False,
            "failure_reason": None,
            "injected_diff": None,
        }

        # Create worktree
        wt_name = f"inject-humanizer-pr{pr_num}"
        wt_path = (WORKTREES_DIR / wt_name).resolve()
        branch = f"inj-pr{pr_num}"

        try:
            # Cleanup any stale worktree
            if wt_path.exists():
                git("worktree", "remove", "--force", str(wt_path), cwd=str(REPO_DIR))
                shutil.rmtree(wt_path, ignore_errors=True)
            git("branch", "-D", branch, cwd=str(REPO_DIR))

            # Create worktree at HEAD
            r = git("worktree", "add", "-b", branch, str(wt_path),
                    f"origin/{default_branch}", cwd=str(REPO_DIR), timeout=120)
            if r.returncode != 0:
                stderr_msg = r.stderr.decode(errors="replace")
                if "already exists" in stderr_msg:
                    git("branch", "-D", branch, cwd=str(REPO_DIR))
                    r = git("worktree", "add", "-b", branch, str(wt_path),
                            f"origin/{default_branch}", cwd=str(REPO_DIR), timeout=120)
                if r.returncode != 0:
                    result["failure_reason"] = f"worktree_failed: {stderr_msg[:200]}"
                    print(f"    [FAIL] Worktree creation failed")
                    stats["errors"] += 1
                    results.append(result)
                    continue

            # Extract source-only patch
            source_patch = _split_patch_source_only(inst["patch"])
            if not source_patch.strip():
                result["failure_reason"] = "no_source_hunks_in_patch"
                print(f"    [FAIL] No source hunks in patch")
                stats["failed"] += 1
                results.append(result)
                continue

            # Try git apply -R --check first
            proc = subprocess.run(
                ["git", "apply", "--check", "-R"],
                cwd=str(wt_path), input=source_patch.encode(),
                capture_output=True, timeout=30,
            )

            if proc.returncode == 0:
                # Actually apply
                proc = subprocess.run(
                    ["git", "apply", "-R"],
                    cwd=str(wt_path), input=source_patch.encode(),
                    capture_output=True, timeout=30,
                )
                if proc.returncode == 0:
                    injected_diff = git_text("diff", cwd=str(wt_path))
                    if injected_diff.strip():
                        result["success"] = True
                        result["injection_level"] = "Level_1_Clean_Revert"
                        result["injected_diff"] = injected_diff
                        print(f"    [OK] Level 1 success, diff={len(injected_diff)} chars")
                        stats["success"] += 1
                    else:
                        result["failure_reason"] = "apply_produced_no_diff"
                        print(f"    [FAIL] Apply succeeded but no diff")
                        stats["failed"] += 1
                else:
                    err = proc.stderr.decode(errors="replace")[:200]
                    result["failure_reason"] = f"apply_failed: {err}"
                    print(f"    [FAIL] git apply -R failed: {err[:100]}")
                    stats["failed"] += 1
            else:
                err = proc.stderr.decode(errors="replace")[:200]
                result["failure_reason"] = f"check_failed: {err}"
                print(f"    [FAIL] git apply --check -R failed: {err[:100]}")
                stats["failed"] += 1

        except Exception as e:
            result["failure_reason"] = f"exception: {str(e)[:200]}"
            print(f"    [ERROR] {e}")
            stats["errors"] += 1
        finally:
            # Cleanup worktree
            git("worktree", "remove", "--force", str(wt_path), cwd=str(REPO_DIR))
            git("branch", "-D", branch, cwd=str(REPO_DIR))
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        results.append(result)

    # Save results
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(INJECTION_FILE, "w", encoding="utf-8") as f:
        for r in results:
            # Truncate large diffs for storage
            r_out = dict(r)
            if r_out.get("injected_diff") and len(r_out["injected_diff"]) > 50000:
                r_out["injected_diff"] = r_out["injected_diff"][:50000] + "\n... (truncated)"
            f.write(json.dumps(r_out, ensure_ascii=False) + "\n")

    # Summary
    t = stats["total"]
    print(f"\n{'=' * 70}")
    print(f"  INJECTION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total instances   : {t}")
    print(f"  Level 1 success   : {stats['success']} ({stats['success']/t*100:.1f}%)" if t else "")
    print(f"  Failed            : {stats['failed']}")
    print(f"  Errors            : {stats['errors']}")
    print(f"  Saved to: {INJECTION_FILE}")

    return results


# ============================================================================
# Phase 3: Verify
# ============================================================================


def phase_verify(max_instances: int | None = None) -> list[dict]:
    """Verify injected instances with dotnet test (pass-to-fail check)."""

    print(f"\n{'=' * 70}")
    print(f"  PHASE 3: VERIFY (dotnet test pass-to-fail)")
    print(f"{'=' * 70}")

    if not INJECTION_FILE.exists():
        print(f"  [ERROR] Injection results not found: {INJECTION_FILE}")
        print(f"  Run --phase inject first.")
        sys.exit(1)

    # Load injection results (only successful ones)
    injections: list[dict] = []
    with open(INJECTION_FILE, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rec = json.loads(line)
                if rec.get("success"):
                    injections.append(rec)

    if max_instances:
        injections = injections[:max_instances]

    print(f"  Successfully injected instances to verify: {len(injections)}")

    if not injections:
        print(f"  Nothing to verify.")
        return []

    # Also load sampled data for full patch info
    sampled_by_pr: dict[int, dict] = {}
    if SAMPLED_FILE.exists():
        with open(SAMPLED_FILE, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    sampled_by_pr[rec["pr_number"]] = rec

    # Get latest HEAD
    git("fetch", "origin", cwd=str(REPO_DIR), timeout=120)
    default_branch = "main"
    for bn in ("main", "master"):
        r = git("rev-parse", "--verify", f"origin/{bn}", cwd=str(REPO_DIR))
        if r.returncode == 0:
            default_branch = bn
            break
    head_sha = git_text("rev-parse", f"origin/{default_branch}", cwd=str(REPO_DIR)).strip()

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    stats = {
        "total": 0,
        "p2f_confirmed": 0,
        "p2f_failed": 0,
        "healthy_already_fail": 0,
        "build_fail": 0,
        "errors": 0,
    }

    for idx, inj in enumerate(injections, 1):
        pr_num = inj["pr_number"]
        stats["total"] += 1

        print(f"\n{'_' * 60}")
        print(f"  [{idx}/{len(injections)}] PR #{pr_num}: {inj['title'][:50]}")
        print(f"  source_files={inj['source_files']}")
        print(f"  test_files={inj['test_files']}")

        result = {
            "pr_number": pr_num,
            "title": inj["title"],
            "injection_level": inj["injection_level"],
            "verification": None,
        }

        sampled = sampled_by_pr.get(pr_num, {})
        full_patch = sampled.get("patch", inj.get("patch", ""))
        source_patch = _split_patch_source_only(full_patch)

        wt_name = f"verify-humanizer-pr{pr_num}"
        wt_path = (WORKTREES_DIR / wt_name).resolve()
        branch = f"vfy-pr{pr_num}"

        try:
            # Cleanup stale worktree
            if wt_path.exists():
                git("worktree", "remove", "--force", str(wt_path), cwd=str(REPO_DIR))
                shutil.rmtree(wt_path, ignore_errors=True)
            git("branch", "-D", branch, cwd=str(REPO_DIR))

            # Create worktree at HEAD
            r = git("worktree", "add", "-b", branch, str(wt_path),
                    f"origin/{default_branch}", cwd=str(REPO_DIR), timeout=120)
            if r.returncode != 0:
                stderr_msg = r.stderr.decode(errors="replace")
                if "already exists" in stderr_msg:
                    git("branch", "-D", branch, cwd=str(REPO_DIR))
                    r = git("worktree", "add", "-b", branch, str(wt_path),
                            f"origin/{default_branch}", cwd=str(REPO_DIR), timeout=120)
                if r.returncode != 0:
                    result["verification"] = {"status": "skipped", "reason": "worktree_failed"}
                    print(f"    [SKIP] Worktree failed")
                    stats["errors"] += 1
                    results.append(result)
                    continue

            start_time = time.monotonic()

            # Step 0: Restore + Build
            print(f"    [0/3] Restoring and building...")
            if not run_dotnet_restore(str(wt_path), timeout=300):
                print(f"    [WARN] dotnet restore had issues, proceeding anyway")

            build_ok = run_dotnet_build(str(wt_path), timeout=300)
            if not build_ok:
                print(f"    [WARN] dotnet build failed on clean HEAD, trying test anyway")

            # Step 1: Healthy check - tests should PASS
            test_projects = _detect_test_projects(inj.get("test_files", []), str(wt_path))
            print(f"    Test projects: {test_projects}")
            print(f"    [1/3] Healthy check: running dotnet test on clean HEAD...")
            healthy = run_dotnet_test_multi(str(wt_path), test_projects, timeout=600)
            healthy_pass = healthy["returncode"] == 0 or (healthy["passed"] > 0 and healthy["failed"] == 0)
            print(f"          rc={healthy['returncode']} passed={healthy['passed']} "
                  f"failed={healthy['failed']} total={healthy['total']} "
                  f"-> {'PASS' if healthy_pass else 'FAIL'}")

            if not healthy_pass:
                print(f"    [WARN] Tests already fail on clean HEAD!")
                # Show last bit of output for debugging
                tail = healthy["output_tail"][-500:]
                for line in tail.splitlines()[-5:]:
                    print(f"          {line}")

            # Step 2: Inject bug
            print(f"    [2/3] Injecting bug (git apply -R)...")

            if not source_patch.strip():
                result["verification"] = {"status": "skipped", "reason": "no_source_patch"}
                print(f"    [SKIP] No source patch available")
                stats["errors"] += 1
                results.append(result)
                continue

            proc = subprocess.run(
                ["git", "apply", "-R"],
                cwd=str(wt_path), input=source_patch.encode(),
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode(errors="replace")[:200]
                result["verification"] = {
                    "status": "injection_replay_failed",
                    "error": err,
                }
                print(f"    [FAIL] Could not re-inject: {err[:100]}")
                stats["errors"] += 1
                results.append(result)
                continue

            injected_diff = git_text("diff", cwd=str(wt_path))
            print(f"          Injected diff: {len(injected_diff)} chars")

            # Rebuild after injection
            print(f"          Rebuilding after injection...")
            build_ok = run_dotnet_build(str(wt_path), timeout=300)
            if not build_ok:
                print(f"          [WARN] Build failed after injection (may cause test failures)")

            # Step 3: Pass-to-fail check - tests should FAIL
            print(f"    [3/3] P2F check: running dotnet test on buggy revision...")
            buggy = run_dotnet_test_multi(str(wt_path), test_projects, timeout=600)
            target_failed = buggy["failed"] > 0 or (buggy["returncode"] != 0 and buggy["passed"] < healthy["passed"])
            print(f"          rc={buggy['returncode']} passed={buggy['passed']} "
                  f"failed={buggy['failed']} total={buggy['total']} "
                  f"-> {'FAIL (good!)' if target_failed else 'PASS (bad!)'}")

            duration = time.monotonic() - start_time

            verification = {
                "status": "completed",
                "healthy_pass": healthy_pass,
                "healthy_rc": healthy["returncode"],
                "healthy_passed": healthy["passed"],
                "healthy_failed": healthy["failed"],
                "healthy_total": healthy["total"],
                "buggy_rc": buggy["returncode"],
                "buggy_passed": buggy["passed"],
                "buggy_failed": buggy["failed"],
                "buggy_total": buggy["total"],
                "target_tests_failed": target_failed,
                "pass_to_fail": healthy_pass and target_failed,
                "duration_seconds": round(duration, 2),
            }

            result["verification"] = verification

            if healthy_pass and target_failed:
                print(f"    [OK] Pass-to-fail CONFIRMED")
                stats["p2f_confirmed"] += 1
            elif not healthy_pass:
                print(f"    [WARN] Healthy tests already fail - cannot confirm P2F")
                stats["healthy_already_fail"] += 1
            else:
                print(f"    [BAD] Tests still pass after injection")
                stats["p2f_failed"] += 1

        except Exception as e:
            result["verification"] = {"status": "error", "error": str(e)[:300]}
            print(f"    [ERROR] {e}")
            stats["errors"] += 1
        finally:
            git("worktree", "remove", "--force", str(wt_path), cwd=str(REPO_DIR))
            git("branch", "-D", branch, cwd=str(REPO_DIR))
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        results.append(result)

        # Incremental save
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(VERIFICATION_FILE, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Summary
    t = stats["total"]
    print(f"\n{'=' * 70}")
    print(f"  VERIFICATION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total instances        : {t}")
    if t > 0:
        print(f"  P2F confirmed          : {stats['p2f_confirmed']} ({stats['p2f_confirmed']/t*100:.1f}%)")
        print(f"  P2F failed (still pass): {stats['p2f_failed']}")
        print(f"  Healthy already fail   : {stats['healthy_already_fail']}")
        print(f"  Errors                 : {stats['errors']}")
    print(f"  Saved to: {VERIFICATION_FILE}")

    return results


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="C# Humanizer experiment pipeline: sample, inject, verify"
    )
    parser.add_argument(
        "--phase",
        choices=["sample", "inject", "verify", "all"],
        default="all",
        help="Which phase to run (default: all)",
    )
    parser.add_argument(
        "--max", "-n",
        type=int,
        default=10,
        help="Maximum number of instances (default: 10)",
    )
    args = parser.parse_args()

    _load_env()

    if args.phase in ("sample", "all"):
        phase_sample(max_instances=args.max)

    if args.phase in ("inject", "all"):
        phase_inject(max_instances=args.max)

    if args.phase in ("verify", "all"):
        phase_verify(max_instances=args.max)

    print(f"\nDone.")


if __name__ == "__main__":
    main()
