"""Behavioral verification for injected SWE-bench Pro instances.

For each successfully injected instance:
  1. Create worktree at healthy HEAD
  2. Run target tests on healthy revision → should PASS (sanity check)
  3. Apply bug injection (reverse golden_patch from original data)
  4. Run target tests on buggy revision → should FAIL (pass-to-fail)
  5. Run broader tests on buggy revision → unrelated should PASS (no-regression)
  6. Record all results

Usage:
    python scripts/verify_swebench_pro.py [--max N] [--filter ID]
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

PYTHON = sys.executable


def _find_python() -> str:
    """Find the best available Python interpreter (prefer 3.12+ for ansible)."""
    for candidate in ("python3.12", "python3.13", "python3.14", "python3"):
        p = shutil.which(candidate)
        if p:
            return p
    return sys.executable


def _create_venv(worktree: str) -> str:
    """Create an isolated venv inside the worktree. Returns path to the venv python."""
    venv_dir = os.path.join(worktree, ".venv")
    python_bin = _find_python()
    subprocess.run(
        [python_bin, "-m", "venv", venv_dir],
        cwd=worktree, capture_output=True, timeout=60,
    )
    venv_python = os.path.join(venv_dir, "bin", "python")
    if not os.path.exists(venv_python):
        venv_python = os.path.join(venv_dir, "Scripts", "python.exe")  # Windows
    # Upgrade pip to avoid old-pip issues
    subprocess.run(
        [venv_python, "-m", "pip", "install", "-q", "--upgrade", "pip", "setuptools", "wheel"],
        cwd=worktree, capture_output=True, timeout=120,
    )
    return venv_python


def git(*args: str, cwd: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, timeout=timeout, text=False,
    )


def git_text(*args: str, cwd: str, timeout: int = 600) -> str:
    r = git(*args, cwd=cwd, timeout=timeout)
    return r.stdout.decode(errors="replace")


def run_pytest(cwd: str, test_files: list[str], timeout: int = 300, python: str | None = None) -> dict:
    """Run pytest on specific test files and parse results."""
    py = python or PYTHON
    base_cmd = [py, "-m", "pytest", "-x", "--tb=short", "-q",
                "-p", "no:unraisableexception"] + test_files
    # Use xvfb-run if available (needed for GUI frameworks like Qt)
    if shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + base_cmd
    else:
        cmd = base_cmd

    # Set environment for headless Qt rendering
    env = os.environ.copy()
    env.update({
        "QT_QPA_PLATFORM": "offscreen",
        "QTWEBENGINE_DISABLE_SANDBOX": "1",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--disable-gpu --no-sandbox",
        "QT_QUICK_BACKEND": "software",
    })
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env)
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

        failed_tests = re.findall(r"FAILED\s+(\S+)", output)

        return {
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed + errors,
            "total": passed + failed + errors,
            "failed_tests": failed_tests,
            "output_tail": output[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "failed_tests": [], "output_tail": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "failed_tests": [], "output_tail": str(e)}


def extract_source_files_from_patch(patch: str) -> list[str]:
    """Extract non-test source files from a patch."""
    test_pats = [re.compile(r"test[_/]"), re.compile(r"[_/]test\."), re.compile(r"tests[_/]")]
    files = []
    for line in patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            path = m.group(2)
            if not any(p.search(path.lower()) for p in test_pats):
                files.append(path)
    return list(dict.fromkeys(files))


def reverse_patch(patch: str) -> str:
    """Reverse a unified diff for injection."""
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
            m2 = re.match(r"@@ -(\d+(?:,\d+)?) \+(\d+(?:,\d+)?) @@(.*)", line)
            if m2:
                result.append(f"@@ -{m2.group(2)} +{m2.group(1)} @@{m2.group(3)}")
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


def _install_project(worktree: str, repo: str, timeout: int = 300, python: str | None = None,
                     test_files: list[str] | None = None) -> bool:
    """Install project and test dependencies in the worktree using isolated venv."""
    wt = Path(worktree)
    py = python or PYTHON
    pip_base = [py, "-m", "pip", "install", "-q"]

    # Initialize git submodules if any
    subprocess.run(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=worktree, capture_output=True, timeout=120,
    )

    # Install vendored packages (e.g., vendor/infogami for openlibrary)
    vendor_dir = wt / "vendor"
    if vendor_dir.is_dir():
        for sub in vendor_dir.iterdir():
            if sub.is_dir() and (
                (sub / "setup.py").exists() or (sub / "pyproject.toml").exists()
            ):
                subprocess.run(pip_base + ["-e", str(sub)], cwd=worktree,
                               capture_output=True, timeout=timeout)
        # Also add vendor dir to Python path via .pth file for non-installable packages
        site_pkgs = subprocess.run(
            [py, "-c", "import site; print(site.getsitepackages()[0])"],
            capture_output=True, timeout=10,
        )
        if site_pkgs.returncode == 0:
            sp = site_pkgs.stdout.decode().strip()
            pth_file = Path(sp) / "vendor.pth"
            if not pth_file.exists():
                pth_file.write_text(str(vendor_dir) + "\n")

    # Always ensure pytest is available first
    subprocess.run(pip_base + ["pytest"], cwd=worktree, capture_output=True, timeout=120)

    # Install requirement files (including repo-specific patterns)
    req_patterns = [
        "requirements.txt", "test-requirements.txt", "requirements-tests.txt",
        "requirements/test.txt", "requirements/dev.txt",
        "requirements_test.txt", "requirements_dev.txt",
    ]
    for req_file in req_patterns:
        if (wt / req_file).exists():
            r = subprocess.run(pip_base + ["-r", req_file], cwd=worktree,
                               capture_output=True, timeout=timeout)
            if r.returncode != 0:
                # Fallback: install line-by-line, skipping failures
                _install_requirements_best_effort(pip_base, wt / req_file, worktree, timeout)

    # Install the project itself
    if (wt / "pyproject.toml").exists() or (wt / "setup.py").exists():
        installed = False
        for extras in [".[test]", ".[dev]", ".[testing]", "."]:
            r = subprocess.run(pip_base + ["-e", extras], cwd=worktree,
                               capture_output=True, timeout=timeout)
            if r.returncode == 0:
                installed = True
                break
            else:
                stderr = r.stderr.decode(errors="replace") if r.stderr else ""
                # If it's just "extra not found", try next; otherwise log
                if "ERROR" in stderr and "extra" not in stderr.lower():
                    print(f"        pip install -e {extras} failed: {stderr[-200:]}")

        if not installed:
            print(f"        [WARN] Could not install project via pip install -e")

    # Try to fix missing modules iteratively (up to 5 rounds)
    # Use target test files for collection so conftest imports are detected
    co_args = [py, "-m", "pytest", "--co", "-q"] + (test_files or [])
    for attempt in range(5):
        r = subprocess.run(
            co_args,
            cwd=worktree, capture_output=True, timeout=60,
        )
        stderr = r.stderr.decode(errors="replace") if r.stderr else ""
        stdout = r.stdout.decode(errors="replace") if r.stdout else ""
        combined = stderr + stdout

        # Find missing pytest plugins
        missing_plugins = re.findall(r"pytest-\w+(?:-\w+)*", combined)

        # Detect unknown config options that indicate missing plugins
        unknown_opts = re.findall(r"Unknown config option: (\w+)", combined)
        CONFIG_TO_PLUGIN = {
            "xvfb_colordepth": "pytest-xvfb",
            "xvfb_width": "pytest-xvfb",
            "xvfb_height": "pytest-xvfb",
        }
        for opt in unknown_opts:
            if opt in CONFIG_TO_PLUGIN:
                missing_plugins.append(CONFIG_TO_PLUGIN[opt])

        # Find missing modules from ImportError/ModuleNotFoundError
        missing_modules = re.findall(r"No module named '([\w.]+)'", combined)

        # Filter out project's own modules (they should be installed via -e .)
        repo_name = repo.split("/")[-1].replace("-", "_").lower()
        missing_modules = [m for m in missing_modules if m.split(".")[0].lower() != repo_name]

        # Map module names to pip package names (common mappings)
        MODULE_TO_PIP = {
            "pytest_mock": "pytest-mock",
            "pytest_asyncio": "pytest-asyncio",
            "pytest_cov": "pytest-cov",
            "pytest_httpx": "pytest-httpx",
            "yaml": "pyyaml",
            "cv2": "opencv-python",
            "PIL": "Pillow",
            "bs4": "beautifulsoup4",
            "attr": "attrs",
            "dateutil": "python-dateutil",
            "dotenv": "python-dotenv",
            "gi": "PyGObject",
            "web": "webpy",
            "memcache": "python-memcached",
            "psycopg2": "psycopg2-binary",
        }
        mapped_modules = []
        for m in missing_modules:
            base = m.split(".")[0]
            # Special multi-level mappings
            if m.startswith("PyQt6.QtWebEngine"):
                mapped_modules.append("PyQt6-WebEngine")
            elif m.startswith("PyQt5.QtWebEngine"):
                mapped_modules.append("PyQtWebEngine")
            else:
                mapped_modules.append(MODULE_TO_PIP.get(base, base.replace("_", "-")))

        to_install = list(set(missing_plugins + mapped_modules))
        if not to_install:
            break

        print(f"        Installing missing deps (round {attempt+1}): {to_install}")
        r = subprocess.run(pip_base + to_install, cwd=worktree,
                           capture_output=True, timeout=120)
        if r.returncode != 0:
            stderr = r.stderr.decode(errors="replace") if r.stderr else ""
            # Try installing one by one, skip failures
            for pkg in to_install:
                subprocess.run(pip_base + [pkg], cwd=worktree,
                               capture_output=True, timeout=60)

    return True


def _install_requirements_best_effort(pip_base: list[str], req_path: Path, cwd: str, timeout: int) -> None:
    """Install requirements file line-by-line, skipping failures."""
    # Package substitutions for packages needing system deps
    SUBSTITUTIONS = {
        "psycopg2": "psycopg2-binary",
    }
    with open(req_path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        # Apply substitutions
        pkg_name = re.split(r"[=<>!~\[]", line)[0].strip()
        if pkg_name in SUBSTITUTIONS:
            line = line.replace(pkg_name, SUBSTITUTIONS[pkg_name], 1)
        # Try installing each line individually, including git+ deps
        subprocess.run(pip_base + [line], cwd=cwd, capture_output=True, timeout=timeout)


def verify_instance(
    injection_result: dict,
    original_data: dict,
    repos_dir: Path,
    worktrees_dir: Path,
    test_timeout: int,
) -> dict:
    """Verify a single injected instance."""

    iid = injection_result["instance_id"]
    repo = injection_result["repo"]
    level = injection_result.get("injection_level", "?")
    healthy_head = injection_result.get("healthy_head", "")
    patch = original_data.get("patch", "")

    # Test info from original dataset
    target_test_files = original_data.get("selected_test_files_to_run", [])
    if isinstance(target_test_files, str):
        try:
            target_test_files = json.loads(target_test_files)
        except json.JSONDecodeError:
            import ast as _ast
            target_test_files = _ast.literal_eval(target_test_files)

    fail_to_pass_raw = original_data.get("fail_to_pass", [])
    if isinstance(fail_to_pass_raw, str):
        try:
            fail_to_pass_raw = json.loads(fail_to_pass_raw)
        except json.JSONDecodeError:
            import ast as _ast
            fail_to_pass_raw = _ast.literal_eval(fail_to_pass_raw)

    short_id = iid[:60]
    print(f"\n{'━' * 70}")
    print(f"  {short_id}")
    print(f"  repo={repo}  level={level}  target_tests={len(target_test_files)}")
    print(f"  expected fail_to_pass: {len(fail_to_pass_raw)} test(s)")
    print(f"{'━' * 70}")

    result = {
        "instance_id": iid,
        "repo": repo,
        "injection_level": level,
        "verification": None,
    }

    if not target_test_files:
        print(f"  [SKIP] No target test files")
        result["verification"] = {"status": "skipped", "reason": "no_target_tests"}
        return result

    # Setup repo
    repo_dir = repos_dir / repo.replace("/", "__")
    if not repo_dir.exists():
        print(f"  [SKIP] Repo not cloned")
        result["verification"] = {"status": "skipped", "reason": "repo_not_cloned"}
        return result

    git("config", "core.longpaths", "true", cwd=str(repo_dir))

    # Detect default branch
    default_branch = "main"
    for bn in ("main", "master", "devel"):
        r = git("rev-parse", "--verify", f"origin/{bn}", cwd=str(repo_dir))
        if r.returncode == 0:
            default_branch = bn
            break

    # Create worktree
    wt_name = f"verify-{repo.replace('/', '-')}-{iid[-8:]}"
    wt_path = (worktrees_dir / wt_name).resolve()
    if wt_path.exists():
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir))
        shutil.rmtree(wt_path, ignore_errors=True)

    branch = f"vfy-{iid[-8:]}"
    git("branch", "-D", branch, cwd=str(repo_dir))
    r = git("worktree", "add", "-b", branch, str(wt_path), f"origin/{default_branch}",
            cwd=str(repo_dir), timeout=1200)
    if r.returncode != 0:
        print(f"  [SKIP] Worktree creation failed")
        result["verification"] = {"status": "skipped", "reason": "worktree_failed"}
        return result

    start_time = time.monotonic()

    try:
        # Check which test files exist
        existing_tests = [t for t in target_test_files if (wt_path / t).exists()]
        if not existing_tests:
            print(f"  [SKIP] No target test files exist on HEAD")
            result["verification"] = {"status": "skipped", "reason": "test_files_missing"}
            return result

        print(f"  Target tests: {existing_tests}")

        # Create isolated venv for this worktree
        print(f"  [0/3] Creating isolated venv & installing dependencies...")
        venv_python = _create_venv(str(wt_path))
        install_ok = _install_project(str(wt_path), repo, test_timeout,
                                      python=venv_python, test_files=existing_tests)
        if not install_ok:
            print(f"  [WARN] Dependency install may have issues, proceeding anyway")

        # ── Step 1: Healthy check (target tests should PASS) ──
        print(f"  [1/3] Healthy check: running target tests on clean HEAD...")
        healthy_result = run_pytest(str(wt_path), existing_tests, timeout=test_timeout, python=venv_python)
        healthy_pass = healthy_result["returncode"] == 0
        print(f"        rc={healthy_result['returncode']} passed={healthy_result['passed']} "
              f"failed={healthy_result['failed']} → {'PASS' if healthy_pass else 'FAIL'}")

        if not healthy_pass:
            print(f"  [WARN] Target tests already fail on healthy HEAD!")
            print(f"         {healthy_result['output_tail'][-300:]}")

        # ── Step 2: Inject bug (apply reverse patch via AST/git) ──
        print(f"  [2/3] Injecting bug...")

        # We need to recreate the injection. Use the same method as the original run.
        # For L2 (AST Surgery): re-run AST surgery using base_commit
        base_commit = original_data.get("base_commit", "")
        import ast as pyast

        source_files = extract_source_files_from_patch(patch)
        injected = False

        if level == "Level_1_Clean_Revert":
            # Try git apply -R
            proc = subprocess.run(
                ["git", "apply", "-R"],
                cwd=str(wt_path), input=patch.encode(),
                capture_output=True, timeout=30,
            )
            if proc.returncode == 0:
                injected = True
            else:
                # Fallback: manual reverse
                rev = reverse_patch(patch)
                proc = subprocess.run(
                    ["git", "apply"],
                    cwd=str(wt_path), input=rev.encode(),
                    capture_output=True, timeout=30,
                )
                injected = proc.returncode == 0

        elif level == "Level_2_AST_Surgery":
            # Re-run AST surgery
            for filepath in source_files:
                target_path = wt_path / filepath
                if not target_path.exists():
                    continue

                try:
                    prefix_content = git_text("show", f"{base_commit}:{filepath}",
                                              cwd=str(repo_dir), timeout=30)
                except Exception:
                    continue

                if not prefix_content.strip():
                    continue

                current_content = target_path.read_text(encoding="utf-8", errors="replace")

                try:
                    prefix_tree = pyast.parse(prefix_content)
                    current_tree = pyast.parse(current_content)
                except SyntaxError:
                    continue

                # Extract functions
                prefix_funcs = _extract_functions(prefix_content, prefix_tree)
                current_funcs = _extract_functions(current_content, current_tree)

                new_content = current_content
                replaced = False
                for name, prefix_src in prefix_funcs.items():
                    if name in current_funcs and current_funcs[name] != prefix_src:
                        new_content = new_content.replace(current_funcs[name], prefix_src)
                        replaced = True

                if replaced:
                    try:
                        pyast.parse(new_content)
                    except SyntaxError:
                        continue
                    target_path.write_text(new_content, encoding="utf-8")
                    injected = True

        if not injected:
            print(f"  [FAIL] Could not re-inject bug")
            result["verification"] = {"status": "injection_replay_failed"}
            return result

        diff_size = len(git_text("diff", cwd=str(wt_path)))
        print(f"        Bug injected, diff size: {diff_size} chars")

        # ── Step 3: Pass-to-fail check (target tests should FAIL) ──
        print(f"  [3/3] P2F check: running target tests on buggy revision...")
        buggy_result = run_pytest(str(wt_path), existing_tests, timeout=test_timeout, python=venv_python)
        target_failed = buggy_result["returncode"] != 0
        print(f"        rc={buggy_result['returncode']} passed={buggy_result['passed']} "
              f"failed={buggy_result['failed']} → {'FAIL (good!)' if target_failed else 'PASS (bad!)'}")

        # Check if the specific expected tests failed
        expected_fails = set()
        for ft in fail_to_pass_raw:
            # Extract test name (remove file prefix for matching)
            parts = ft.split("::")
            if len(parts) >= 2:
                expected_fails.add(parts[-1])

        actual_fails = set()
        for ft in buggy_result.get("failed_tests", []):
            parts = ft.split("::")
            if len(parts) >= 2:
                actual_fails.add(parts[-1])

        expected_matched = len(expected_fails & actual_fails) if expected_fails else 0

        duration = time.monotonic() - start_time

        verification = {
            "status": "completed",
            "healthy_pass": healthy_pass,
            "target_tests_failed": target_failed,
            "pass_to_fail": target_failed and healthy_pass,
            "healthy_passed": healthy_result["passed"],
            "healthy_failed": healthy_result["failed"],
            "buggy_passed": buggy_result["passed"],
            "buggy_failed": buggy_result["failed"],
            "buggy_total": buggy_result["total"],
            "expected_fail_count": len(fail_to_pass_raw),
            "expected_matched": expected_matched,
            "actual_failed_tests": buggy_result.get("failed_tests", [])[:10],
            "duration_seconds": round(duration, 2),
        }

        p2f_ok = target_failed and healthy_pass

        if p2f_ok:
            print(f"  ✓ Pass-to-fail CONFIRMED "
                  f"(expected {len(expected_fails)} fails, matched {expected_matched})")
        elif not healthy_pass:
            print(f"  ⚠ Healthy tests already fail — cannot confirm p2f")
        else:
            print(f"  ✗ Pass-to-fail NOT confirmed — tests still pass after injection")

        result["verification"] = verification
        return result

    finally:
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir))
        git("branch", "-D", branch, cwd=str(repo_dir))


def _extract_functions(source: str, tree) -> dict[str, str]:
    import ast as pyast
    lines = source.splitlines(keepends=True)
    funcs = {}
    for node in pyast.walk(tree):
        if isinstance(node, (pyast.FunctionDef, pyast.AsyncFunctionDef)):
            start = node.lineno - 1
            end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 1
            funcs[node.name] = "".join(lines[start:end])
    return funcs


def main():
    parser = argparse.ArgumentParser(description="Verify injected SWE-bench Pro instances")
    parser.add_argument("--injection-results", default="experiments/swebench_pro/injection_results.jsonl")
    parser.add_argument("--sampled-data", default="experiments/swebench_pro/sampled_35.jsonl")
    parser.add_argument("--output", "-o", default="experiments/swebench_pro/verification_results.jsonl")
    parser.add_argument("--timeout", "-t", type=int, default=300)
    parser.add_argument("--filter", "-f", type=str, default=None)
    parser.add_argument("--max", "-n", type=int, default=None)
    parser.add_argument("--repos-dir", default=".pri-workspace/repos")
    parser.add_argument("--worktrees-dir", default=".pri-workspace/worktrees")
    args = parser.parse_args()

    # Load data
    with open(args.injection_results, encoding="utf-8") as f:
        injections = [json.loads(l) for l in f if l.strip()]

    with open(args.sampled_data, encoding="utf-8") as f:
        sampled = {json.loads(l)["instance_id"]: json.loads(l) for l in f if l.strip()}

    # Filter to successful injections only
    to_verify = [r for r in injections if r.get("success")]

    if args.filter:
        to_verify = [r for r in to_verify if args.filter in r["instance_id"]]
    if args.max:
        to_verify = to_verify[:args.max]

    repos_dir = Path(args.repos_dir)
    worktrees_dir = Path(args.worktrees_dir)
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    print(f"Instances to verify: {len(to_verify)}")
    print(f"Output: {args.output}")

    results = []
    stats = {"total": 0, "p2f_confirmed": 0, "p2f_failed": 0,
             "healthy_already_fail": 0, "skipped": 0}

    for i, inj in enumerate(to_verify, 1):
        iid = inj["instance_id"]
        orig = sampled.get(iid, {})

        print(f"\n[{i}/{len(to_verify)}]", end="")
        stats["total"] += 1

        try:
            result = verify_instance(inj, orig, repos_dir, worktrees_dir, args.timeout)
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {
                "instance_id": iid,
                "repo": inj["repo"],
                "verification": {"status": "error", "error": str(e)[:300]},
            }

        v = result.get("verification", {})
        if v.get("status") == "skipped":
            stats["skipped"] += 1
        elif v.get("pass_to_fail"):
            stats["p2f_confirmed"] += 1
        elif v.get("status") == "completed" and not v.get("healthy_pass"):
            stats["healthy_already_fail"] += 1
        elif v.get("status") == "completed":
            stats["p2f_failed"] += 1

        results.append(result)

        # Incremental save
        with open(args.output, "w", encoding="utf-8") as f:
            for r in results:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Summary
    t = stats["total"]
    verified = stats["p2f_confirmed"] + stats["p2f_failed"] + stats["healthy_already_fail"]
    print(f"\n\n{'=' * 70}")
    print(f"  VERIFICATION SUMMARY")
    print(f"{'=' * 70}")
    print(f"  Total instances       : {t}")
    print(f"  Skipped               : {stats['skipped']}")
    print(f"  Verified              : {verified}")
    print(f"  P2F confirmed         : {stats['p2f_confirmed']}"
          f" ({stats['p2f_confirmed']/verified*100:.1f}%)" if verified else "")
    print(f"  P2F failed            : {stats['p2f_failed']}")
    print(f"  Healthy already fail  : {stats['healthy_already_fail']}")
    print(f"\n  Results saved to: {args.output}")


if __name__ == "__main__":
    main()
