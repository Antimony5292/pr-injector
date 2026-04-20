"""End-to-end experiment pipeline for Azure DevOps C# repositories.

Targets internal repos (async_azure_anapa, service-shared_azure_transcoder).
Based on experiment_csharp.py but uses Azure DevOps REST API instead of GitHub.

Phases:
  1. Sample: Mine merged bug-fix PRs from Azure DevOps that touch both src/ and test/ .cs files
  2. Inject+Verify: In a single worktree pass — healthy check, inject (L1/L2), then P2F verify

Usage:
    python scripts/experiment_ado.py --repo anapa --phase all --max 10
    python scripts/experiment_ado.py --repo transcoder --phase sample --max 20
    python scripts/experiment_ado.py --repo anapa --phase inject
"""

from __future__ import annotations

import argparse
import base64
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKTREES_DIR = PROJECT_ROOT / ".pri-workspace" / "worktrees"

# Azure DevOps org/project
ADO_ORG = "https://skype.visualstudio.com/DefaultCollection"
ADO_PROJECT = "SCC"

# Repo configs: name -> (local_path, default_branch, output_dir)
REPO_CONFIGS = {
    "anapa": {
        "ado_repo": "async_azure_anapa",
        "local_path": PROJECT_ROOT / ".product-workspace" / "async_azure_anapa",
        "default_branch": "master",
        "output_dir": PROJECT_ROOT / "experiments" / "ado_anapa",
    },
    "transcoder": {
        "ado_repo": "service-shared_azure_transcoder",
        "local_path": PROJECT_ROOT / ".product-workspace" / "service-shared_azure_transcoder",
        "default_branch": "master",
        "output_dir": PROJECT_ROOT / "experiments" / "ado_transcoder",
    },
}


# ---------------------------------------------------------------------------
# Shared helpers (reused from experiment_csharp.py)
# ---------------------------------------------------------------------------


def _load_env() -> None:
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


def _get_ado_pat() -> str:
    pat = os.environ.get("SCC_PAT", "")
    if not pat:
        print("[ERROR] SCC_PAT not set. Put it in .env or export it.")
        sys.exit(1)
    return pat


def _dotnet_env() -> dict[str, str]:
    env = os.environ.copy()
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    env["DOTNET_NOLOGO"] = "1"
    env["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1"
    return env


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


_NON_BUGFIX_RE = re.compile(
    r"\b(refactor|format(?:ting)?|clean\s*up|rename|update.*(?:sdk|net)|"
    r"bump\s+\w|preparation|chore|docs?|readme|typo|style|nit|"
    r"ci|cd|pipeline|workflow|dependabot|nuget|upgrade|migration|"
    r"deprecat|remove\s+unused|housekeep|cosmetic|scale\s*out|"
    r"scale\s*down|monitor|config\s*version|probe|onboard|buildout)\b",
    re.IGNORECASE,
)

_BUGFIX_RE = re.compile(
    r"\b(fix|bug|crash|deadlock|issue|error|exception|broken|regression|"
    r"correct|repair|resolve|patch|fault|defect|overflow|underflow|"
    r"race\s*condition|null\s*ref|npe|oom|leak|hang|timeout|"
    r"wrong|invalid|fail|unexpected|incorrect|hotfix)\b",
    re.IGNORECASE,
)


def _is_likely_bugfix(title: str, labels: list[str] | None = None) -> bool:
    combined = f"{title} {' '.join(labels or [])}"
    if _BUGFIX_RE.search(combined):
        return True
    if _NON_BUGFIX_RE.search(combined):
        return False
    return True


def _split_patch_source_only(patch: str) -> str:
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
# Level 2: C# AST surgery
# ---------------------------------------------------------------------------

_CS_METHOD_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?:(?:public|private|protected|internal|static|virtual|override|abstract|sealed|async|partial|new|readonly)\s+)*'
    r'[\w<>\[\],\?\s]+\s+'
    r'(?P<name>\w+)\s*'
    r'(?:<[^>]*>)?\s*'
    r'\([^)]*\)',
    re.MULTILINE,
)


def _extract_cs_methods(source: str) -> dict[str, str]:
    methods: dict[str, str] = {}
    for m in _CS_METHOD_RE.finditer(source):
        name = m.group("name")
        start_pos = m.start()
        brace_search_start = m.end()
        rest = source[brace_search_start:]
        stripped = rest.lstrip()
        if not stripped.startswith("{") and not stripped.startswith("=>"):
            continue
        if stripped.startswith("=>"):
            semi_pos = source.find(";", brace_search_start)
            if semi_pos == -1:
                continue
            method_text = source[start_pos:semi_pos + 1]
        else:
            brace_pos = source.index("{", brace_search_start)
            depth = 0
            i = brace_pos
            while i < len(source):
                if source[i] == "{":
                    depth += 1
                elif source[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth != 0:
                continue
            method_text = source[start_pos:i + 1]
        methods[name] = method_text
    return methods


def try_level2_csharp(
    worktree: str, repo_dir: str, base_commit: str, patch: str,
) -> tuple[bool, str]:
    source_files = [f for f in _files_from_diff(patch) if _is_source_cs(f)]
    if not source_files:
        return False, "No source .cs files in patch"

    any_changed = False
    for filepath in source_files:
        target_path = Path(worktree) / filepath
        if not target_path.exists():
            continue
        try:
            prefix_content = git_text(
                "show", f"{base_commit}:{filepath}", cwd=repo_dir, timeout=30,
            )
        except Exception:
            continue
        if not prefix_content.strip():
            continue

        current_content = target_path.read_text(encoding="utf-8", errors="replace")
        prefix_methods = _extract_cs_methods(prefix_content)
        current_methods = _extract_cs_methods(current_content)

        new_content = current_content
        replaced = False
        for name, prefix_src in prefix_methods.items():
            if name in current_methods and current_methods[name] != prefix_src:
                new_content = new_content.replace(current_methods[name], prefix_src, 1)
                if new_content != current_content:
                    replaced = True

        if replaced and new_content != current_content:
            target_path.write_text(new_content, encoding="utf-8")
            any_changed = True

    if any_changed:
        diff = git_text("diff", cwd=worktree)
        if diff.strip():
            return True, diff
    return False, "Level 2: no C# methods matched or replaced"


# ---------------------------------------------------------------------------
# Test project detection & dotnet runner
# ---------------------------------------------------------------------------


def _detect_test_projects(test_files: list[str], worktree: str) -> list[str]:
    projects = set()
    for tf in test_files:
        parts = tf.split("/")
        if len(parts) >= 2:
            project_dir = "/".join(parts[:2])
            proj_dir = Path(worktree) / project_dir
            if proj_dir.is_dir():
                for csproj in proj_dir.glob("*.csproj"):
                    projects.add(str(csproj.relative_to(worktree)))
    return sorted(projects)


def run_dotnet_test(
    cwd: str, test_project: str, timeout: int = 300,
) -> dict:
    cmd = ["dotnet", "test", test_project, "-v", "q", "--nologo"]
    env = _dotnet_env()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env)
        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        output = stdout + "\n" + stderr

        passed, failed, skipped, total = 0, 0, 0, 0
        m = re.search(r"Failed:\s*(\d+),\s*Passed:\s*(\d+),\s*Skipped:\s*(\d+),\s*Total:\s*(\d+)", output)
        if m:
            failed, passed, skipped, total = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        else:
            m2 = re.search(r"Tests succeeded:\s*(\d+)", output)
            if m2:
                passed = int(m2.group(1))
            m2 = re.search(r"Tests failed:\s*(\d+)", output)
            if m2:
                failed = int(m2.group(1))
            m2 = re.search(r"[Tt]otal:\s*(\d+)", output)
            if m2:
                total = int(m2.group(1))
            else:
                total = passed + failed + skipped
            if passed == 0 and failed == 0:
                passed = len(re.findall(r"^\s*Passed\s+", output, re.MULTILINE))
                failed = len(re.findall(r"^\s*Failed\s+", output, re.MULTILINE))
                total = passed + failed

        return {"returncode": proc.returncode, "passed": passed, "failed": failed,
                "skipped": skipped, "total": total, "output_tail": output[-2000:]}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": 0, "failed": 0, "skipped": 0,
                "total": 0, "output_tail": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -1, "passed": 0, "failed": 0, "skipped": 0,
                "total": 0, "output_tail": str(e)}


def run_dotnet_test_multi(cwd: str, test_projects: list[str], timeout: int = 300) -> dict:
    total_passed, total_failed, total_skipped, total_total, worst_rc = 0, 0, 0, 0, 0
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
        "returncode": worst_rc, "passed": total_passed, "failed": total_failed,
        "skipped": total_skipped, "total": total_total,
        "output_tail": "\n".join(all_output)[-2000:], "projects_tested": test_projects,
    }


def run_dotnet_build(cwd: str, timeout: int = 300) -> bool:
    env = _dotnet_env()
    try:
        proc = subprocess.run(
            ["dotnet", "build", "--nologo", "-v", "q"],
            cwd=cwd, capture_output=True, timeout=timeout, env=env,
        )
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"          [WARN] dotnet build timed out after {timeout}s")
        return False


def _run_dotnet_build_project(cwd: str, project: str, timeout: int = 600) -> bool:
    """Build a specific project (not the whole solution)."""
    env = _dotnet_env()
    try:
        proc = subprocess.run(
            ["dotnet", "build", project, "--nologo", "-v", "q"],
            cwd=cwd, capture_output=True, timeout=timeout, env=env,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace")[-300:]
            print(f"          [WARN] Build failed for {project}: {stderr[:150]}")
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"          [WARN] Build timed out for {project} after {timeout}s")
        return False


def run_dotnet_restore(cwd: str, timeout: int = 300) -> bool:
    env = _dotnet_env()
    try:
        proc = subprocess.run(
            ["dotnet", "restore", "--nologo"],
            cwd=cwd, capture_output=True, timeout=timeout, env=env,
        )
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"          [WARN] dotnet restore timed out after {timeout}s")
        return False


def _inject_nuget_credentials(worktree: str) -> None:
    """Ensure nuget.config in the worktree has PAT credentials for private feeds."""
    pat = os.environ.get("SCC_PAT", "")
    if not pat:
        return

    nuget_cfg = Path(worktree) / "nuget.config"
    if not nuget_cfg.exists():
        return

    content = nuget_cfg.read_text(encoding="utf-8")
    # Skip if credentials already present
    if "packageSourceCredentials" in content:
        return

    # Find all source keys and inject credentials for each
    source_keys = re.findall(r'<add\s+key="([^"]+)"\s+value="https://skype\.pkgs\.visualstudio\.com', content)
    if not source_keys:
        return

    creds_block = "  <packageSourceCredentials>\n"
    for key in source_keys:
        creds_block += f"    <{key}>\n"
        creds_block += f'      <add key="Username" value="az" />\n'
        creds_block += f'      <add key="ClearTextPassword" value="{pat}" />\n'
        creds_block += f"    </{key}>\n"
    creds_block += "  </packageSourceCredentials>\n"

    # Insert before </configuration>
    content = content.replace("</configuration>", creds_block + "</configuration>")
    nuget_cfg.write_text(content, encoding="utf-8")


# ============================================================================
# Azure DevOps API helpers
# ============================================================================


def _ado_headers(pat: str) -> dict[str, str]:
    b64 = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {b64}"}


def _ado_get(pat: str, url: str, params: dict | None = None) -> dict:
    import httpx
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_ado_headers(pat), params=params)
        resp.raise_for_status()
        return resp.json()


def _ado_get_text(pat: str, url: str) -> str:
    import httpx
    with httpx.Client(timeout=30) as client:
        resp = client.get(url, headers=_ado_headers(pat))
        resp.raise_for_status()
        return resp.text


# ============================================================================
# Phase 1: Sample PRs from Azure DevOps
# ============================================================================


def phase_sample(repo_cfg: dict, max_instances: int = 10) -> list[dict]:
    """Sample qualifying bug-fix PRs from Azure DevOps."""
    pat = _get_ado_pat()
    ado_repo = repo_cfg["ado_repo"]
    output_dir = repo_cfg["output_dir"]
    sampled_file = output_dir / "sampled.jsonl"

    api_base = f"{ADO_ORG}/{ADO_PROJECT}/_apis/git/repositories/{ado_repo}"

    print(f"\n{'=' * 70}")
    print(f"  PHASE 1: SAMPLE PRs from {ADO_PROJECT}/{ado_repo}")
    print(f"{'=' * 70}")

    # Fetch completed (merged) PRs — paginated
    all_prs: list[dict] = []
    skip = 0
    top = 100
    while len(all_prs) < 500:
        print(f"  Fetching PRs (skip={skip})...")
        data = _ado_get(pat, f"{api_base}/pullrequests", params={
            "status": "completed",
            "$top": top,
            "$skip": skip,
            "api-version": "7.1",
        })
        prs = data.get("value", [])
        if not prs:
            break
        all_prs.extend(prs)
        skip += len(prs)
        if len(prs) < top:
            break

    print(f"  Total completed PRs fetched: {len(all_prs)}")

    # Filter qualifying PRs
    qualifying: list[dict] = []
    skipped_non_bugfix = 0

    for pr in all_prs:
        if len(qualifying) >= max_instances:
            break

        pr_id = pr["pullRequestId"]
        pr_title = pr.get("title", "")

        # Bug-fix filter
        pr_labels = [label.get("name", "") for label in pr.get("labels", [])]
        if not _is_likely_bugfix(pr_title, pr_labels):
            skipped_non_bugfix += 1
            continue

        # Get the diff (iterations -> changes, or use git diff via commits)
        merge_commit = pr.get("lastMergeCommit", {}).get("commitId", "")
        source_commit = pr.get("lastMergeSourceCommit", {}).get("commitId", "")

        if not merge_commit:
            continue

        # Get diff between target and source using git
        # ADO provides /diffs endpoint but git diff is more reliable
        repo_dir = str(repo_cfg["local_path"])
        # Fetch the merge commit if not available locally
        git("fetch", "origin", merge_commit, cwd=repo_dir, timeout=120)
        if source_commit:
            git("fetch", "origin", source_commit, cwd=repo_dir, timeout=120)

        # Get the diff of the merge commit against its first parent
        diff = git_text("diff", f"{merge_commit}^", merge_commit, cwd=repo_dir, timeout=60)
        if not diff.strip():
            # Try source commit diff
            if source_commit:
                diff = git_text("log", "-1", "-p", "--format=", source_commit, cwd=repo_dir, timeout=60)
            if not diff.strip():
                continue

        files = _files_from_diff(diff)
        src_files = [f for f in files if _is_source_cs(f)]
        test_files = [f for f in files if _is_test_cs(f)]

        if not src_files or not test_files:
            continue

        # Get base SHA (parent of merge commit)
        base_sha = git_text("rev-parse", f"{merge_commit}^", cwd=repo_dir, timeout=30).strip()

        record = {
            "pr_number": pr_id,
            "title": pr_title,
            "merge_commit_sha": merge_commit,
            "base_sha": base_sha,
            "patch": diff,
            "source_files": src_files,
            "test_files": test_files,
            "merged_at": pr.get("closedDate", ""),
            "html_url": f"{ADO_ORG}/{ADO_PROJECT}/_git/{ado_repo}/pullrequest/{pr_id}",
            "labels": pr_labels,
        }
        qualifying.append(record)
        print(f"    PR #{pr_id}: {pr_title[:60]}  "
              f"[{len(src_files)} src, {len(test_files)} test]")
        time.sleep(0.2)

    print(f"\n  Qualifying PRs: {len(qualifying)}")
    print(f"  Skipped (non bug-fix): {skipped_non_bugfix}")

    output_dir.mkdir(parents=True, exist_ok=True)
    with open(sampled_file, "w", encoding="utf-8") as f:
        for rec in qualifying:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"  Saved to: {sampled_file}")

    print(f"\n  Summary:")
    for rec in qualifying:
        print(f"    PR #{rec['pr_number']}: {rec['title'][:50]}  "
              f"src={len(rec['source_files'])} test={len(rec['test_files'])}")

    return qualifying


# ============================================================================
# Phase 2: Inject + Verify
# ============================================================================


def phase_inject(repo_cfg: dict, max_instances: int | None = None) -> list[dict]:
    """Inject bugs and verify P2F in a single worktree pass per PR."""

    output_dir = repo_cfg["output_dir"]
    sampled_file = output_dir / "sampled.jsonl"
    injection_file = output_dir / "injection_results.jsonl"
    verification_file = output_dir / "verification_results.jsonl"
    repo_dir = str(repo_cfg["local_path"])
    default_branch = repo_cfg["default_branch"]
    ado_repo = repo_cfg["ado_repo"]

    print(f"\n{'=' * 70}")
    print(f"  PHASE 2: INJECT + VERIFY ({ado_repo})")
    print(f"{'=' * 70}")

    if not sampled_file.exists():
        print(f"  [ERROR] Sampled file not found: {sampled_file}")
        print(f"  Run --phase sample first.")
        sys.exit(1)

    instances: list[dict] = []
    with open(sampled_file, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                instances.append(json.loads(line))

    if max_instances:
        instances = instances[:max_instances]

    print(f"  Instances to process: {len(instances)}")

    if not Path(repo_dir).exists():
        print(f"  [ERROR] Repo not found: {repo_dir}")
        sys.exit(1)

    # Get latest HEAD
    git("fetch", "origin", cwd=repo_dir, timeout=120)
    git("config", "core.longpaths", "true", cwd=repo_dir)
    head_sha = git_text("rev-parse", f"origin/{default_branch}", cwd=repo_dir).strip()
    print(f"  Latest HEAD: {head_sha[:12]} ({default_branch})")

    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    stats = {
        "total": 0, "l1_success": 0, "l2_success": 0,
        "failed": 0, "errors": 0, "healthy_skip": 0,
        "p2f_confirmed": 0, "p2f_failed": 0,
    }

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
            "verification": None,
        }

        safe_name = ado_repo.replace("/", "-")
        wt_name = f"inject-{safe_name}-pr{pr_num}"
        wt_path = (WORKTREES_DIR / wt_name).resolve()
        branch = f"inj-{safe_name}-pr{pr_num}"

        try:
            if wt_path.exists():
                git("worktree", "remove", "--force", str(wt_path), cwd=repo_dir)
                shutil.rmtree(wt_path, ignore_errors=True)
            git("branch", "-D", branch, cwd=repo_dir)

            r = git("worktree", "add", "-b", branch, str(wt_path),
                    f"origin/{default_branch}", cwd=repo_dir, timeout=600)
            if r.returncode != 0:
                stderr_msg = r.stderr.decode(errors="replace")
                if "already exists" in stderr_msg:
                    git("branch", "-D", branch, cwd=repo_dir)
                    r = git("worktree", "add", "-b", branch, str(wt_path),
                            f"origin/{default_branch}", cwd=repo_dir, timeout=600)
                if r.returncode != 0:
                    result["failure_reason"] = f"worktree_failed: {stderr_msg[:200]}"
                    print(f"    [FAIL] Worktree creation failed")
                    stats["errors"] += 1
                    results.append(result)
                    continue

            start_time = time.monotonic()

            # Inject NuGet credentials into worktree's nuget.config
            _inject_nuget_credentials(str(wt_path))

            # --- Step 1: Healthy check ---
            print(f"    [1/4] Healthy check: restore + build + test...")
            restore_ok = run_dotnet_restore(str(wt_path), timeout=900)
            if not restore_ok:
                print(f"          [WARN] dotnet restore failed")

            # Detect test projects first, then build only those
            test_projects = _detect_test_projects(inst.get("test_files", []), str(wt_path))
            print(f"          Test projects: {test_projects}")
            if not test_projects:
                result["failure_reason"] = "no_test_projects_detected"
                print(f"    [SKIP] No test projects detected")
                stats["failed"] += 1
                results.append(result)
                continue

            # Build only the test projects (avoids Deploy/BackofficeScriptsDoc errors)
            build_ok = True
            for proj in test_projects:
                ok = _run_dotnet_build_project(str(wt_path), proj, timeout=900)
                if not ok:
                    build_ok = False
            if not build_ok:
                result["failure_reason"] = "build_failed_on_clean_head"
                print(f"    [SKIP] Build failed on clean HEAD")
                stats["healthy_skip"] += 1
                results.append(result)
                continue

            healthy = run_dotnet_test_multi(str(wt_path), test_projects, timeout=600)
            healthy_pass = healthy["returncode"] == 0 or (healthy["passed"] > 0 and healthy["failed"] == 0)
            print(f"          rc={healthy['returncode']} passed={healthy['passed']} "
                  f"failed={healthy['failed']} total={healthy['total']} "
                  f"-> {'PASS' if healthy_pass else 'FAIL'}")

            if not healthy_pass:
                result["failure_reason"] = "healthy_check_failed"
                print(f"    [SKIP] Tests already fail on clean HEAD")
                stats["healthy_skip"] += 1
                results.append(result)
                continue

            # --- Step 2: Inject ---
            source_patch = _split_patch_source_only(inst["patch"])
            if not source_patch.strip():
                result["failure_reason"] = "no_source_hunks_in_patch"
                print(f"    [FAIL] No source hunks in patch")
                stats["failed"] += 1
                results.append(result)
                continue

            # Level 1: git apply -R
            print(f"    [2/4] Inject L1: git apply -R...")
            proc = subprocess.run(
                ["git", "apply", "--check", "-R"],
                cwd=str(wt_path), input=source_patch.encode(),
                capture_output=True, timeout=30,
            )

            if proc.returncode == 0:
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
                        print(f"          [L1] Success! diff={len(injected_diff)} chars")
                        stats["l1_success"] += 1

            if not result["success"]:
                l1_err = proc.stderr.decode(errors="replace")[:200] if proc.returncode != 0 else "no diff"
                print(f"          [L1] Failed: {l1_err[:100]}")

                git("checkout", ".", cwd=str(wt_path))
                git("clean", "-fd", cwd=str(wt_path))

                # Level 2: C# AST surgery
                base_commit = inst.get("base_sha", "")
                if not base_commit:
                    base_commit = inst.get("merge_commit_sha", "")
                    if base_commit:
                        parent_sha = git_text(
                            "rev-parse", f"{base_commit}^", cwd=repo_dir, timeout=30,
                        ).strip()
                        if parent_sha and len(parent_sha) >= 7:
                            base_commit = parent_sha

                if base_commit:
                    print(f"    [2/4] Inject L2: AST surgery (base={base_commit[:12]})...")
                    ok, l2_result = try_level2_csharp(
                        str(wt_path), repo_dir, base_commit, inst["patch"],
                    )
                    if ok:
                        result["success"] = True
                        result["injection_level"] = "Level_2_AST_Surgery"
                        result["injected_diff"] = l2_result
                        print(f"          [L2] Success! diff={len(l2_result)} chars")
                        stats["l2_success"] += 1
                    else:
                        print(f"          [L2] Failed: {l2_result[:100]}")
                        result["failure_reason"] = l2_result
                        stats["failed"] += 1
                else:
                    print(f"          [L2] Skipped: no base_commit available")
                    result["failure_reason"] = f"l1_failed_no_base_commit: {l1_err}"
                    stats["failed"] += 1

            # --- Step 3+4: Rebuild + P2F verify ---
            if result["success"]:
                print(f"    [3/4] Rebuilding after injection...")
                for proj in test_projects:
                    _run_dotnet_build_project(str(wt_path), proj, timeout=900)

                print(f"    [4/4] P2F check: running tests on buggy code...")
                buggy = run_dotnet_test_multi(str(wt_path), test_projects, timeout=600)
                target_failed = buggy["failed"] > 0 or (buggy["returncode"] != 0 and buggy["passed"] < healthy["passed"])
                print(f"          rc={buggy['returncode']} passed={buggy['passed']} "
                      f"failed={buggy['failed']} total={buggy['total']} "
                      f"-> {'FAIL (good!)' if target_failed else 'PASS (bad!)'}")

                duration = time.monotonic() - start_time

                result["verification"] = {
                    "status": "completed",
                    "healthy_pass": healthy_pass,
                    "healthy_passed": healthy["passed"],
                    "healthy_failed": healthy["failed"],
                    "healthy_total": healthy["total"],
                    "buggy_rc": buggy["returncode"],
                    "buggy_passed": buggy["passed"],
                    "buggy_failed": buggy["failed"],
                    "buggy_total": buggy["total"],
                    "target_tests_failed": target_failed,
                    "pass_to_fail": target_failed,
                    "duration_seconds": round(duration, 2),
                }

                if target_failed:
                    print(f"    [OK] Pass-to-fail CONFIRMED")
                    stats["p2f_confirmed"] += 1
                else:
                    print(f"    [BAD] Tests still pass after injection")
                    stats["p2f_failed"] += 1

        except Exception as e:
            result["failure_reason"] = f"exception: {str(e)[:200]}"
            print(f"    [ERROR] {e}")
            stats["errors"] += 1
        finally:
            git("worktree", "remove", "--force", str(wt_path), cwd=repo_dir)
            git("branch", "-D", branch, cwd=repo_dir)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)

        results.append(result)

        # Incremental save
        output_dir.mkdir(parents=True, exist_ok=True)
        with open(injection_file, "w", encoding="utf-8") as f:
            for r in results:
                r_out = dict(r)
                if r_out.get("injected_diff") and len(r_out["injected_diff"]) > 50000:
                    r_out["injected_diff"] = r_out["injected_diff"][:50000] + "\n... (truncated)"
                f.write(json.dumps(r_out, ensure_ascii=False, default=str) + "\n")
        with open(verification_file, "w", encoding="utf-8") as f:
            for r in results:
                r_out = {k: v for k, v in r.items() if k != "injected_diff"}
                f.write(json.dumps(r_out, ensure_ascii=False, default=str) + "\n")

    # Summary
    t = stats["total"]
    l_success = stats["l1_success"] + stats["l2_success"]
    print(f"\n{'=' * 70}")
    print(f"  INJECT + VERIFY SUMMARY ({ado_repo})")
    print(f"{'=' * 70}")
    print(f"  Total instances   : {t}")
    if t > 0:
        print(f"  Healthy skip      : {stats['healthy_skip']}")
        print(f"  Level 1 success   : {stats['l1_success']}")
        print(f"  Level 2 success   : {stats['l2_success']}")
        print(f"  Injection rate    : {l_success}/{t} ({l_success/t*100:.1f}%)")
        print(f"  P2F confirmed     : {stats['p2f_confirmed']}")
        print(f"  P2F failed        : {stats['p2f_failed']}")
        print(f"  Failed            : {stats['failed']}")
        print(f"  Errors            : {stats['errors']}")
    print(f"  Saved to: {injection_file}")
    print(f"           {verification_file}")

    return results


# ============================================================================
# Main
# ============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Azure DevOps C# experiment pipeline: sample, inject+verify"
    )
    parser.add_argument(
        "--repo", "-r",
        choices=list(REPO_CONFIGS.keys()),
        required=True,
        help="Which repo to target (anapa, transcoder)",
    )
    parser.add_argument(
        "--phase",
        choices=["sample", "inject", "all"],
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

    repo_cfg = REPO_CONFIGS[args.repo]

    if args.phase in ("sample", "all"):
        phase_sample(repo_cfg, max_instances=args.max)

    if args.phase in ("inject", "all"):
        phase_inject(repo_cfg, max_instances=args.max)

    print(f"\nDone.")


if __name__ == "__main__":
    main()
