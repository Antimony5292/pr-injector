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
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from pathlib import Path

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


PYTHON = sys.executable
PYTHON_312 = os.environ.get("PRINJECTOR_PYTHON_312", "python3.12")


def _stable_id_suffix(*parts: str, length: int = 12) -> str:
    data = "::".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha1(data).hexdigest()[:length]


def _read_requires_python(worktree: str | Path | None) -> str | None:
    if not worktree:
        return None
    pyproject = Path(worktree) / "pyproject.toml"
    if not pyproject.exists():
        return None
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:
        return None
    project = data.get("project", {})
    value = project.get("requires-python")
    return value if isinstance(value, str) and value.strip() else None


def _python_version_tuple(python_bin: str) -> tuple[int, int, int] | None:
    proc = subprocess.run(
        [
            python_bin,
            "-c",
            "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return None
    try:
        parts = proc.stdout.decode().strip().split(".")
        return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]
    except Exception:
        return None


def _version_satisfies(version: tuple[int, int, int], spec: str | None) -> bool:
    if not spec:
        return True
    for raw_part in spec.split(","):
        part = raw_part.strip()
        if not part:
            continue
        match = re.match(r"(<=|>=|==|<|>|~=)\s*(\d+(?:\.\d+){0,2})", part)
        if not match:
            continue
        op, rhs_text = match.groups()
        rhs_parts = [int(p) for p in rhs_text.split(".")]
        rhs = tuple((rhs_parts + [0, 0, 0])[:3])
        if op == ">=" and not (version >= rhs):
            return False
        if op == ">" and not (version > rhs):
            return False
        if op == "<=" and not (version <= rhs):
            return False
        if op == "<" and not (version < rhs):
            return False
        if op == "==" and not (version == rhs):
            return False
        if op == "~=":
            # PEP 440 compatible releases: ~=3.9 means >=3.9,<4.0, while
            # ~=3.9.1 means >=3.9.1,<3.10.0.
            upper = (
                (rhs[0] + 1, 0, 0)
                if len(rhs_parts) <= 2
                else (rhs[0], rhs[1] + 1, 0)
            )
            if not (version >= rhs and version < upper):
                return False
    return True


def _patchlevel_runtime_compatible(version: tuple[int, int, int], spec: str | None) -> bool:
    """Allow a one-patch drift only when every bound targets one minor line."""

    if not spec:
        return False
    bounds = [
        tuple((([int(p) for p in text.split(".")] + [0, 0, 0])[:3]))
        for text in re.findall(r"(?:<=|>=|==|<|>|~=)\s*(\d+(?:\.\d+){0,2})", spec)
    ]
    if not bounds or any(bound[:2] != version[:2] for bound in bounds):
        return False
    return all(abs(version[2] - bound[2]) <= 1 for bound in bounds)


def _find_python(repo: str = "", worktree: str | Path | None = None) -> str | None:
    """Find the best available Python interpreter for creating test venvs."""
    requires_python = _read_requires_python(worktree)
    # Old compiled scientific-project snapshots frequently build unreliably on
    # Python 3.13 even when their metadata has no upper Python bound. Prefer the
    # bundled 3.12 runtime for these repositories; strict healthy-head tests
    # remain the authority on whether the environment is actually usable.
    if repo in {
        "astropy/astropy",
        "matplotlib/matplotlib",
        "scikit-learn/scikit-learn",
    }:
        candidates = [
            "python3.12",
            PYTHON_312,
            sys.executable,
            "python3.13",
            "python3",
            "python3.14",
        ]
    elif requires_python:
        candidates = [
            sys.executable,
            "python3.13",
            "python3.12",
            PYTHON_312,
            "python3.14",
            "python3",
        ]
    else:
        candidates = [
            sys.executable,
            "python3.13",
            "python3.12",
            PYTHON_312,
            "python3",
            "python3.14",
        ]
    seen: set[str] = set()
    for candidate in candidates:
        p = shutil.which(candidate)
        if not p and Path(candidate).exists():
            p = str(Path(candidate))
        if not p or p in seen:
            continue
        seen.add(p)
        version = _python_version_tuple(p)
        if version and _python_has_venv_and_pip(p):
            if _version_satisfies(version, requires_python):
                return p
            if (
                repo == "internetarchive/openlibrary"
                and _patchlevel_runtime_compatible(version, requires_python)
            ):
                print(
                    "        [runtime-tolerance] Using "
                    f"Python {'.'.join(map(str, version))} for {requires_python}; "
                    "healthy target tests remain authoritative"
                )
                return p
    if requires_python:
        print(
            "        [ERROR] No available Python satisfies "
            f"{requires_python} for {repo or worktree}"
        )
        return None
    return sys.executable


def _python_has_venv_and_pip(python_bin: str) -> bool:
    probe = subprocess.run(
        [python_bin, "-c", "import ensurepip, venv"],
        capture_output=True,
        timeout=10,
    )
    return probe.returncode == 0


def _prepare_python_runtime_env(python_bin: str) -> None:
    version = _python_version_tuple(python_bin)
    if not version or version < (3, 14, 0):
        return
    expat_lib = Path("/opt/homebrew/opt/expat/lib")
    if not expat_lib.exists():
        return
    for key in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        current = os.environ.get(key, "")
        paths = [p for p in current.split(os.pathsep) if p]
        if str(expat_lib) not in paths:
            os.environ[key] = os.pathsep.join([str(expat_lib)] + paths)


def _create_venv(worktree: str, repo: str = "") -> str:
    """Create an isolated venv inside the worktree. Returns path to the venv python."""
    python_bin = _find_python(repo, worktree)
    if not python_bin:
        return ""
    _prepare_python_runtime_env(python_bin)
    venv_dir = os.path.join(worktree, ".venv")
    if os.environ.get("PRI_SHARED_REPO_VENV") == "1":
        version = _python_version_tuple(python_bin) or (0, 0, 0)
        repo_id = repo.replace("/", "__") if repo else "unknown_repo"
        tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.environ.get("PRI_SHARED_REPO_VENV_TAG", "default"))
        project_root = Path(__file__).resolve().parents[3]
        venv_dir = str(
            project_root
            / ".pri-workspace"
            / "shared_venvs"
            / f"{tag}-{repo_id}-py{version[0]}.{version[1]}.{version[2]}"
        )
    venv_preexists = os.path.exists(venv_dir)
    version = _python_version_tuple(python_bin) or (0, 0, 0)
    venv_cmd = [python_bin, "-m", "venv"]
    if version >= (3, 14, 0):
        # Homebrew Python 3.14.5 can expose a working global pip while
        # ensurepip inside venv fails due to pyexpat/libexpat linkage. Use
        # system site packages for Python-version-constrained projects such as
        # openlibrary, and let pip install project deps into the venv overlay.
        venv_cmd += ["--without-pip", "--system-site-packages"]
    venv_cmd.append(venv_dir)
    venv_timeout = max(60, int(os.environ.get("PRI_VENV_CREATE_TIMEOUT", "180")))
    subprocess.run(venv_cmd, cwd=worktree, capture_output=True, timeout=venv_timeout)
    venv_python = os.path.join(venv_dir, "bin", "python")
    if not os.path.exists(venv_python):
        windows_python = os.path.join(venv_dir, "Scripts", "python.exe")
        if os.path.exists(windows_python):
            venv_python = windows_python
        elif venv_preexists:
            # A cancelled install can leave a shared venv directory with a
            # dangling interpreter symlink. Rebuild it once instead of
            # returning a nonexistent Windows fallback on macOS/Linux.
            shutil.rmtree(venv_dir, ignore_errors=True)
            subprocess.run(
                venv_cmd,
                cwd=worktree,
                capture_output=True,
                timeout=venv_timeout,
            )
            venv_python = os.path.join(venv_dir, "bin", "python")
        if not os.path.exists(venv_python):
            return ""
    if not venv_preexists and version < (3, 14, 0):
        subprocess.run(
            [venv_python, "-m", "ensurepip", "--upgrade"],
            cwd=worktree, capture_output=True, timeout=120,
        )
        # Upgrade pip to avoid old-pip issues.
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


def _verification_checkout_ref(
    repo_dir: Path,
    healthy_head: str,
    default_branch: str,
) -> tuple[str, str]:
    """Resolve the exact construction revision used by strict verification."""

    desired = str(healthy_head or "").strip()
    if desired:
        resolved = git(
            "rev-parse", "--verify", f"{desired}^{{commit}}", cwd=str(repo_dir), timeout=60
        )
        if resolved.returncode != 0:
            git("fetch", "origin", desired, cwd=str(repo_dir), timeout=300)
            resolved = git(
                "rev-parse", "--verify", f"{desired}^{{commit}}", cwd=str(repo_dir), timeout=60
            )
        if resolved.returncode == 0:
            return resolved.stdout.decode(errors="replace").strip(), "injection_healthy_head"
    return f"origin/{default_branch}", "default_branch_fallback"


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
    """Remove stale worktree/branch state left by interrupted verification runs."""
    git("worktree", "prune", cwd=str(repo_dir), timeout=120)
    if wt_path and wt_path.exists():
        git("worktree", "remove", "--force", str(wt_path), cwd=str(repo_dir), timeout=120)
        shutil.rmtree(wt_path, ignore_errors=True)

    delete = git("branch", "-D", branch, cwd=str(repo_dir), timeout=120)
    if delete.returncode == 0:
        return

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


def run_pytest(cwd: str, test_files: list[str], timeout: int = 300, python: str | None = None) -> dict:
    """Run pytest on specific test files and parse results."""
    py = python or PYTHON
    qutebrowser_runtime = (Path(cwd) / "qutebrowser").is_dir()
    runtime_args = ["--no-qt-log"] if qutebrowser_runtime else []
    base_cmd = [py, "-m", "pytest", *_pytest_config_args(cwd), "-x", "--tb=short", "-q",
                "-p", "no:unraisableexception", *runtime_args] + test_files
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
    if qutebrowser_runtime:
        # qutebrowser owns its Chromium flags. Overriding them makes healthy
        # WebEngine tests fail and emits an environment-only Qt warning.
        env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
    env["PYTHONPATH"] = cwd + os.pathsep + env.get("PYTHONPATH", "")
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


def run_django_tests(cwd: str, labels: list[str], timeout: int = 300, python: str | None = None) -> dict:
    """Run Django's in-repo test runner on dotted test labels."""
    py = python or PYTHON
    cmd = [
        py, "tests/runtests.py",
        "--verbosity=1",
        "--failfast",
        "--parallel=1",
        "--noinput",
    ] + labels
    env = os.environ.copy()
    env.update({
        "PYTHONWARNINGS": "default",
        "DJANGO_SETTINGS_MODULE": "test_sqlite",
    })
    env["PYTHONPATH"] = cwd + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout, env=env)
        stdout = proc.stdout.decode(errors="replace")
        stderr = proc.stderr.decode(errors="replace")
        output = stdout + "\n" + stderr

        passed, failed = 0, 0
        ran = re.search(r"Ran (\d+) tests?", output)
        if ran:
            passed = int(ran.group(1)) if proc.returncode == 0 else 0
            failed = 0 if proc.returncode == 0 else int(ran.group(1))
        elif proc.returncode == 0:
            passed = len(labels)
        else:
            failed = max(1, len(labels))

        failed_tests: list[str] = []
        for label in labels:
            method = label.rsplit(".", 1)[-1]
            if proc.returncode != 0 and method:
                failed_tests.append(label)

        return {
            "returncode": proc.returncode,
            "passed": passed,
            "failed": failed,
            "total": passed + failed,
            "failed_tests": failed_tests,
            "output_tail": output[-2000:],
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "failed_tests": [], "output_tail": "TIMEOUT"}
    except Exception as e:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0,
                "failed_tests": [], "output_tail": str(e)}


def run_repo_tests(
    cwd: str,
    repo: str,
    tests: list[str],
    timeout: int = 300,
    python: str | None = None,
) -> dict:
    started = time.monotonic()
    if repo == "django/django":
        result = run_django_tests(cwd, tests, timeout=timeout, python=python)
    else:
        result = run_pytest(cwd, tests, timeout=timeout, python=python)
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    result["command_count"] = 1
    result["nodeid_count"] = len(tests)
    return result


def _record_test_metrics(metrics: dict | None, phase: str, tests: list[str], result: dict) -> None:
    if metrics is None:
        return
    metrics["test_command_count"] = int(metrics.get("test_command_count") or 0) + 1
    metrics["test_nodeid_count"] = int(metrics.get("test_nodeid_count") or 0) + len(tests)
    by_phase = metrics.setdefault("test_command_count_by_phase", {})
    by_phase[phase] = int(by_phase.get(phase) or 0) + 1
    nodeids_by_phase = metrics.setdefault("test_nodeid_count_by_phase", {})
    nodeids_by_phase[phase] = int(nodeids_by_phase.get(phase) or 0) + len(tests)
    duration_by_phase = metrics.setdefault("test_command_duration_seconds_by_phase", {})
    duration_by_phase[phase] = round(
        float(duration_by_phase.get(phase) or 0.0)
        + float(result.get("duration_seconds") or 0.0),
        3,
    )


def _test_runner_available(worktree: str, repo: str, python: str) -> bool:
    if not python:
        return False
    if repo == "django/django":
        return (Path(worktree) / "tests" / "runtests.py").exists()
    probe = subprocess.run(
        [python, "-m", "pytest", "--version"],
        cwd=worktree,
        capture_output=True,
        timeout=30,
    )
    return probe.returncode == 0


def _pytest_config_args(worktree: str | Path) -> list[str]:
    """Bypass only pyproject pytest configs that modern pytest cannot parse."""

    pyproject = Path(worktree) / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        options = ((data.get("tool") or {}).get("pytest") or {}).get("ini_options") or {}
    except (OSError, ValueError):
        return []
    # pytest 8+ requires an array here. Historical projects commonly used a
    # scalar string accepted by their pinned pytest. Tests are passed
    # explicitly by the harness, so ignoring only this invalid config is safe.
    if isinstance(options.get("testpaths"), str):
        return ["-c", os.devnull]
    return []


def _collection_timeout(requested: int = 60) -> int:
    """Use a configurable floor for slow modern pytest/Qt collection."""
    return max(requested, int(os.environ.get("PRI_COLLECT_TIMEOUT", "120")))


def _collectable_tests(
    worktree: str,
    repo: str,
    tests: list[str],
    python: str,
    timeout: int = 60,
    allow_static_fallback: bool | None = None,
) -> list[str]:
    if repo == "django/django":
        return tests
    timeout = _collection_timeout(timeout)
    collectable: list[str] = []
    batch_matches: dict[str, list[str]] = {}
    if len(tests) >= 4:
        requested_by_file: dict[str, list[str]] = {}
        for test in tests:
            path = test.split("::", 1)[0]
            if "::" in test and (Path(worktree) / path).exists():
                requested_by_file.setdefault(path, []).append(test)
        for path, requested in requested_by_file.items():
            collected = _pytest_collect_nodeids(
                Path(worktree), path, python, timeout=timeout
            )
            if not collected:
                continue
            for test in requested:
                if test in collected:
                    batch_matches[test] = [test]
                    continue
                target_func = _pytest_selector_function(test.split("::")[-1])
                matched = [
                    nodeid
                    for nodeid in collected
                    if _pytest_selector_function(nodeid.split("::")[-1]) == target_func
                ]
                if matched:
                    batch_matches[test] = matched
    for test in tests:
        if test in batch_matches:
            collectable.extend(
                nodeid for nodeid in batch_matches[test] if nodeid not in collectable
            )
            continue
        try:
            proc = subprocess.run(
                [python, "-m", "pytest", *_pytest_config_args(worktree), "--collect-only", "-q", test],
                cwd=worktree,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            proc = None
        if proc is not None and proc.returncode == 0:
            collectable.append(test)
            continue

        remapped = _remap_pytest_nodeid(Path(worktree), test, python, timeout=timeout)
        if not remapped:
            remapped = _static_pytest_nodeid_candidates(Path(worktree), test)
        if not remapped:
            remapped = _target_execution_fallback_tests(Path(worktree), repo, [test], python)
        for nodeid in remapped:
            if nodeid in collectable:
                continue
            try:
                check = subprocess.run(
                    [python, "-m", "pytest", *_pytest_config_args(worktree), "--collect-only", "-q", nodeid],
                    cwd=worktree,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                check = None
            if check is not None and check.returncode == 0:
                collectable.append(nodeid)
            elif (
                (
                    allow_static_fallback
                    if allow_static_fallback is not None
                    else os.environ.get("PRI_ALLOW_STATIC_TARGET_FALLBACK", "1").lower()
                    in {"1", "true", "yes", "on"}
                )
                and nodeid not in collectable
                    and (Path(worktree) / nodeid.split("::", 1)[0]).exists()
            ):
                # Some modern projects fail pytest collection for an exact
                # historical parametrized id even though the broader function
                # remains runnable. Let the healthy-target run below be the
                # authoritative gate instead of discarding the candidate here.
                collectable.append(nodeid)
    return collectable


def _target_execution_fallback_tests(
    worktree: Path,
    repo: str,
    tests: list[str],
    python: str | None = None,
    max_candidates: int = 20,
) -> list[str]:
    """Return narrow fallback tests when stale exact nodeids execute zero tests.

    This is deliberately conservative: it stays within likely test files and
    prefers the same test function. It is used only before the healthy/baseline
    gate, so a bad fallback still has to pass on healthy HEAD and later satisfy
    P2F/P2P/golden repair verification.
    """

    out: list[str] = []
    for test in tests:
        if len(out) >= max_candidates:
            break
        if repo == "django/django":
            for label in _remap_django_test_label(worktree, test, max_candidates=max_candidates):
                if label not in out:
                    out.append(label)
            continue
        if "::" not in test:
            continue
        path_part, *selectors = test.split("::")
        if not selectors:
            continue
        target_func = _pytest_selector_function(selectors[-1])
        if not target_func:
            continue
        py = python or _find_python(repo, worktree) or PYTHON
        candidates: list[str] = []
        for file_path in _candidate_pytest_files(worktree, path_part, target_func):
            if len(candidates) >= max_candidates:
                break
            if not file_path.exists() or not file_path.is_file():
                continue
            rel = str(file_path.relative_to(worktree)).replace("\\", "/")
            collected = _pytest_collect_nodeids(worktree, rel, py)
            matches = [
                nodeid for nodeid in collected
                if _pytest_selector_function(nodeid.split("::")[-1]) == target_func
            ]
            if matches:
                candidates.extend(matches)
                continue
            selector_parts = [_pytest_selector_function(part) for part in selectors]
            selector = "::".join(part for part in selector_parts if part)
            if selector:
                candidates.append(f"{rel}::{selector}")
            candidates.append(f"{rel}::{target_func}")
            # Last resort: the containing file can still be useful when the
            # exact parametrized/class nodeid drifted. The target/P2F gate below
            # decides whether it is a valid benchmark target.
            candidates.append(rel)
        for candidate in candidates:
            if candidate not in out:
                out.append(candidate)
            if len(out) >= max_candidates:
                break
    return out


def _retry_with_target_execution_fallback(
    cwd: str,
    repo: str,
    tests: list[str],
    python: str,
    timeout: int,
    metrics: dict | None = None,
    phase: str = "target_execution_fallback",
) -> tuple[list[str], dict]:
    fallback_tests = _target_execution_fallback_tests(Path(cwd), repo, tests, python)
    if not fallback_tests or fallback_tests == tests:
        return tests, {}
    result = run_repo_tests(cwd, repo, fallback_tests, timeout=timeout, python=python)
    _record_test_metrics(metrics, phase, fallback_tests, result)
    if int(result.get("total") or 0) > 0:
        return fallback_tests, result
    return tests, result


def _filter_passing_tests(
    cwd: str,
    repo: str,
    tests: list[str],
    python: str,
    timeout: int,
    metrics: dict | None = None,
    phase: str = "filter_passing",
    cache_key_prefix: str = "",
) -> tuple[list[str], list[str]]:
    """Return tests that pass when run individually and tests that do not."""
    passing: list[str] = []
    failing: list[str] = []
    for test in tests:
        cache_key = f"{cache_key_prefix}::{test}" if cache_key_prefix else ""
        cached = _healthy_p2p_cache_get(cache_key) if cache_key else None
        if cached is not None:
            if metrics is not None:
                metrics["healthy_p2p_cache_hits"] = int(
                    metrics.get("healthy_p2p_cache_hits") or 0
                ) + 1
            (passing if cached else failing).append(test)
            continue
        result = run_repo_tests(cwd, repo, [test], timeout=timeout, python=python)
        _record_test_metrics(metrics, phase, [test], result)
        passed = result["returncode"] == 0 and int(result.get("total") or 0) > 0
        if cache_key:
            _healthy_p2p_cache_put(cache_key, passed)
        if passed:
            passing.append(test)
        else:
            failing.append(test)
    return passing, failing


def _healthy_p2p_cache_path() -> Path:
    return Path(
        os.environ.get(
            "PRI_HEALTHY_P2P_CACHE",
            ".pri-workspace/preflight_cache/healthy_p2p.sqlite3",
        )
    )


def _healthy_p2p_cache_get(key: str) -> bool | None:
    if not key:
        return None
    path = _healthy_p2p_cache_path()
    if not path.exists():
        return None
    try:
        with sqlite3.connect(path, timeout=30) as connection:
            row = connection.execute(
                "SELECT passed FROM healthy_p2p WHERE cache_key = ?",
                (key,),
            ).fetchone()
        return None if row is None else bool(row[0])
    except sqlite3.Error:
        return None


def _healthy_p2p_cache_put(key: str, passed: bool) -> None:
    if not key:
        return
    path = _healthy_p2p_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(path, timeout=30) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS healthy_p2p ("
                "cache_key TEXT PRIMARY KEY, passed INTEGER NOT NULL, updated_at REAL NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO healthy_p2p(cache_key, passed, updated_at) VALUES (?, ?, ?)",
                (key, int(passed), time.time()),
            )
    except sqlite3.Error:
        return


def _editable_install_args(repo: str, extras: str) -> list[str]:
    """Return editable-install arguments whose build tools remain available."""

    args = ["-e", extras]
    # Meson editable loaders retain their build-tool paths. Build isolation
    # would point them at a deleted pip-build-env directory after installation.
    if repo in {"scikit-learn/scikit-learn", "matplotlib/matplotlib"}:
        args.insert(0, "--no-build-isolation")
    return args


def _install_project(worktree: str, repo: str, timeout: int = 300, python: str | None = None,
                     test_files: list[str] | None = None) -> bool:
    """Install project and test dependencies in the worktree using isolated venv."""
    wt = Path(worktree)
    py = PYTHON if python is None else python
    if not py:
        print("        [ERROR] No Python interpreter available for dependency install")
        return False
    _prepare_python_runtime_env(py)
    project_install_env = os.environ.copy()
    venv_bin = str(Path(py).parent)
    project_install_env["PATH"] = os.pathsep.join(
        [venv_bin, project_install_env.get("PATH", "")]
    ).rstrip(os.pathsep)
    if repo == "internetarchive/openlibrary":
        os.environ.setdefault("PIP_IGNORE_REQUIRES_PYTHON", "1")
    pip_base = [py, "-m", "pip", "install", "-q"]
    pip_check = subprocess.run(
        [py, "-m", "pip", "--version"], cwd=worktree, capture_output=True, timeout=30
    )
    if pip_check.returncode != 0:
        stderr = pip_check.stderr.decode(errors="replace") if pip_check.stderr else ""
        print(f"        [ERROR] pip is unavailable in venv: {stderr[-300:]}")
        return False

    # Initialize git submodules if any
    submodule_cmd = ["git", "submodule", "update", "--init", "--recursive"]
    if repo == "internetarchive/openlibrary":
        cached_infogami = (
            Path(__file__).resolve().parents[3]
            / ".pri-workspace"
            / "repos"
            / "internetarchive__openlibrary"
            / "vendor"
            / "infogami"
        )
        if (cached_infogami / ".git").exists() or (cached_infogami / "setup.py").exists():
            subprocess.run(
                ["git", "config", "submodule.vendor/infogami.url", str(cached_infogami)],
                cwd=worktree,
                capture_output=True,
                timeout=30,
            )
        submodule_cmd = [
            "git", "-c", "protocol.file.allow=always", "submodule", "update",
            "--init", "--depth", "1", "vendor/infogami"
        ]
    try:
        submodule = subprocess.run(
            submodule_cmd,
            cwd=worktree, capture_output=True, timeout=120,
        )
        if submodule.returncode != 0:
            stderr = submodule.stderr.decode(errors="replace") if submodule.stderr else ""
            print(f"        [WARN] Submodule initialization failed: {stderr[-500:]}")
    except subprocess.TimeoutExpired:
        # Submodules are optional for many targeted benchmark tests. A slow
        # network must not preempt the authoritative healthy-target gate;
        # required content surfaces as an import/test failure.
        print("        [WARN] Submodule initialization timed out; continuing to targeted checks")

    # Install vendored packages (e.g., vendor/infogami for openlibrary)
    vendor_dir = wt / "vendor"
    if vendor_dir.is_dir():
        for sub in vendor_dir.iterdir():
            if sub.is_dir() and (
                (sub / "setup.py").exists() or (sub / "pyproject.toml").exists()
            ):
                for vendor_req in ("requirements.txt", "requirements_test.txt"):
                    if repo == "internetarchive/openlibrary" and vendor_req == "requirements_test.txt":
                        continue
                    if (sub / vendor_req).exists():
                        r = subprocess.run(
                            pip_base + ["-r", str(sub / vendor_req)],
                            cwd=worktree, capture_output=True, timeout=timeout
                        )
                        if r.returncode != 0:
                            _install_requirements_best_effort(
                                pip_base, sub / vendor_req, worktree, timeout
                            )
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
    pytest_install = subprocess.run(
        pip_base + ["pytest"], cwd=worktree, capture_output=True, timeout=120
    )
    if pytest_install.returncode != 0:
        stderr = pytest_install.stderr.decode(errors="replace") if pytest_install.stderr else ""
        print(f"        [WARN] Could not install pytest: {stderr[-300:]}")

    repo_bootstrap = {
        "astropy/astropy": [
            "extension-helpers", "Cython", "numpy", "setuptools<81"
        ],
        "conan-io/conan": ["pyyaml"],
        "internetarchive/openlibrary": [
            "babel", "genshi", "beautifulsoup4", "httpx", "python-memcached",
            "isbnlib", "pytest-asyncio", "apscheduler", "luqum==0.11.0",
            "amightygirl.paapi5-python-sdk==1.0.0"
        ],
        "getsentry/responses": ["pytest-asyncio"],
        "igorbenav/fastcrud": ["httpx2", "aiosqlite", "greenlet"],
        "pennylaneai/pennylane": ["sybil"],
        "scikit-learn/scikit-learn": [
            "ninja", "meson", "meson-python", "Cython", "numpy", "scipy", "joblib", "threadpoolctl"
        ],
        "matplotlib/matplotlib": [
            "ninja", "meson", "meson-python", "pybind11", "numpy", "setuptools_scm"
        ],
        "qutebrowser/qutebrowser": [
            "Pillow",
        ],
    }
    bootstrap_packages = repo_bootstrap.get(repo, [])
    if bootstrap_packages:
        subprocess.run(
            pip_base + bootstrap_packages,
            cwd=worktree, capture_output=True, timeout=timeout,
        )

    # Install requirement files (including repo-specific patterns)
    req_patterns = [
        "requirements.txt", "test-requirements.txt", "requirements-tests.txt",
        "requirements/test.txt", "requirements/dev.txt",
        "requirements_test.txt", "requirements_dev.txt",
    ]
    for req_file in req_patterns:
        if repo == "internetarchive/openlibrary":
            # The production requirements set is large and repeated for every
            # candidate. Targeted Pro tests are better served by vendored
            # packages plus the missing-import installation loop below.
            continue
        if (wt / req_file).exists():
            r = subprocess.run(pip_base + ["-r", req_file], cwd=worktree,
                               capture_output=True, timeout=timeout)
            if r.returncode != 0:
                # Fallback: install line-by-line, skipping failures
                _install_requirements_best_effort(pip_base, wt / req_file, worktree, timeout)

    # Install the project itself. Open Library's root pyproject has a narrow
    # Python pin and setup.py cythonizes optional Solr code; targeted tests can
    # import package sources from the worktree root after dependencies/vendor
    # packages are installed.
    if repo == "internetarchive/openlibrary":
        installed = True
    elif (wt / "pyproject.toml").exists() or (wt / "setup.py").exists():
        installed = False
        for extras in [".[test]", ".[dev]", ".[testing]", "."]:
            install_args = _editable_install_args(repo, extras)
            r = subprocess.run(
                pip_base + install_args,
                cwd=worktree,
                capture_output=True,
                timeout=timeout,
                env=project_install_env,
            )
            if r.returncode == 0:
                installed = True
                break
            else:
                stderr = r.stderr.decode(errors="replace") if r.stderr else ""
                if repo in {"scikit-learn/scikit-learn", "matplotlib/matplotlib"}:
                    print(
                        f"        compiled pip install {install_args} failed: "
                        f"{stderr[-2000:]}"
                    )
                # If it's just "extra not found", try next; otherwise log
                if "ERROR" in stderr and "extra" not in stderr.lower():
                    print(f"        pip install -e {extras} failed: {stderr[-200:]}")

        if not installed:
            print(f"        [WARN] Could not install project via pip install -e")

    if repo == "astropy/astropy":
        extension_check = subprocess.run(
            [py, "-c", "import astropy.table._column_mixins"],
            cwd=worktree,
            capture_output=True,
            timeout=60,
        )
        if extension_check.returncode != 0 and (wt / "setup.py").exists():
            # Older Astropy snapshots import Cython extensions directly from
            # the source tree. Editable installation can succeed without
            # placing those extensions in-tree, so build them explicitly.
            build = subprocess.run(
                [py, "setup.py", "build_ext", "--inplace"],
                cwd=worktree,
                capture_output=True,
                timeout=max(timeout, 900),
            )
            if build.returncode != 0:
                stderr = build.stderr.decode(errors="replace") if build.stderr else ""
                print(f"        [WARN] Astropy in-place extension build failed: {stderr[-500:]}")

    # Django uses tests/runtests.py labels, not pytest nodeids. The project
    # install above is enough for these smoke-level targeted runs.
    if repo == "django/django":
        return True

    # Try to fix missing modules iteratively. OpenLibrary's modern conftest
    # imports a deeper optional dependency chain than the usual repositories.
    # Use target test files for collection so conftest imports are detected
    co_args = [py, "-m", "pytest", *_pytest_config_args(worktree), "--co", "-q"] + (test_files or [])
    dependency_rounds = 12 if repo == "internetarchive/openlibrary" else 5
    for attempt in range(dependency_rounds):
        try:
            r = subprocess.run(
                co_args,
                cwd=worktree,
                capture_output=True,
                timeout=_collection_timeout(),
            )
        except subprocess.TimeoutExpired:
            # Dependency discovery is advisory. Exact target collection and
            # healthy execution below remain the authoritative preflight gate.
            print(
                "        [WARN] pytest dependency-discovery collection timed out; "
                "continuing to target-level preflight"
            )
            break
        stderr = r.stderr.decode(errors="replace") if r.stderr else ""
        stdout = r.stdout.decode(errors="replace") if r.stdout else ""
        combined = stderr + stdout

        # Find explicitly missing pytest plugins. Avoid scanning arbitrary
        # paths, which can contain strings like "pytest-net-worktrees".
        missing_plugins = re.findall(
            r"(?:No module named|ModuleNotFoundError: No module named|ImportError: No module named) "
            r"'?(pytest[-_]\w+(?:[-_]\w+)*)'?",
            combined,
        )
        for plugin_line in re.findall(r"Missing required plugins: ([^\n]+)", combined):
            missing_plugins.extend(
                p.strip()
                for p in plugin_line.split(",")
                if p.strip().startswith("pytest-")
            )

        # Detect unknown config options that indicate missing plugins
        unknown_opts = re.findall(r"Unknown config option: (\w+)", combined)
        CONFIG_TO_PLUGIN = {
            "xvfb_colordepth": "pytest-xvfb",
            "xvfb_width": "pytest-xvfb",
            "xvfb_height": "pytest-xvfb",
            "mypy_pyproject_toml_file": "pytest-mypy-plugins",
            "asyncio_default_fixture_loop_scope": "pytest-asyncio",
            "asyncio_default_test_loop_scope": "pytest-asyncio",
            "asyncio_mode": "pytest-asyncio",
        }
        for opt in unknown_opts:
            if opt in CONFIG_TO_PLUGIN:
                missing_plugins.append(CONFIG_TO_PLUGIN[opt])
        if "'asyncio' not found in `markers`" in combined:
            missing_plugins.append("pytest-asyncio")
        if "PytestUnknownMarkWarning: Unknown pytest.mark.asyncio" in combined:
            missing_plugins.append("pytest-asyncio")

        unrecognized_args = re.findall(r"unrecognized arguments?: ([^\n]+)", combined)
        for arg_line in unrecognized_args:
            if "--mypy-pyproject-toml-file" in arg_line:
                missing_plugins.append("pytest-mypy-plugins")

        # Find missing modules from ImportError/ModuleNotFoundError
        missing_modules = re.findall(r"No module named '([\w.]+)'", combined)
        required_packages = re.findall(
            r"requires the ([A-Za-z0-9_.-]+) package to be installed",
            combined,
            flags=re.IGNORECASE,
        )

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
            "pkg_resources": "setuptools<81",
            "erfa": "pyerfa",
            "cv2": "opencv-python",
            "PIL": "Pillow",
            "bs4": "beautifulsoup4",
            "attr": "attrs",
            "dateutil": "python-dateutil",
            "dotenv": "python-dotenv",
            "gi": "PyGObject",
            "web": "web.py",
            "memcache": "python-memcached",
            "psycopg2": "psycopg2-binary",
            "paapi5_python_sdk": "amightygirl.paapi5-python-sdk==1.0.0",
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

        to_install = list(set(missing_plugins + mapped_modules + required_packages))
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
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            nested = (req_path.parent / line.split(maxsplit=1)[1]).resolve()
            if nested.exists():
                _install_requirements_best_effort(pip_base, nested, cwd, timeout)
            continue
        # Apply substitutions
        pkg_name = re.split(r"[=<>!~\[]", line)[0].strip()
        if pkg_name in SUBSTITUTIONS:
            line = line.replace(pkg_name, SUBSTITUTIONS[pkg_name], 1)
        # Try installing each line individually, including git+ deps
        r = subprocess.run(pip_base + [line], cwd=cwd, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            stderr = r.stderr.decode(errors="replace") if r.stderr else ""
            print(f"        [WARN] requirement skipped: {line[:100]} :: {stderr[-180:]}")


def _coerce_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], str):
            nested = _coerce_list(value[0])
            if nested != value:
                return nested
        return [str(item) for item in value]
    if isinstance(value, str):
        import ast as _ast

        current = value
        for _ in range(3):
            try:
                parsed = json.loads(current)
            except json.JSONDecodeError:
                try:
                    parsed = _ast.literal_eval(current)
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


def _worktree_relative_nodeid(worktree: Path, nodeid: str) -> str:
    """Strip pytest root-path prefixes while preserving selectors."""

    path, separator, selector = nodeid.partition("::")
    normalized = path.replace("\\", "/")
    worktree_text = str(worktree.resolve()).replace("\\", "/")
    if normalized.startswith(f"{worktree_text}/"):
        normalized = normalized[len(worktree_text) + 1 :]
    elif not (worktree / normalized).exists():
        parts = normalized.split("/")
        for index in range(1, len(parts)):
            suffix = "/".join(parts[index:])
            if (worktree / suffix).exists():
                normalized = suffix
                break
    return normalized + (f"::{selector}" if separator else "")


def _normalize_test_id(worktree: Path, repo: str, test_id: str) -> str | None:
    """Map benchmark test identifiers to runnable pytest nodeids on HEAD."""
    if repo == "django/django":
        remapped = _remap_django_test_label(worktree, test_id)
        if len(remapped) == 1:
            return remapped[0]

        # Already-normalized Django runner labels:
        #   app.test_module.TestCase.test_method
        if "::" not in test_id and " " not in test_id:
            parts = test_id.split(".")
            if len(parts) >= 3:
                module = ".".join(parts[:-2])
                candidate = Path("tests") / (module.replace(".", "/") + ".py")
                if (worktree / candidate).exists():
                    return test_id
        # SWE-bench Django tests often use unittest labels:
        #   test_name (app_tests.test_module.TestCase)
        m = re.match(r"(?P<method>[\w_]+) \((?P<qual>[\w.]+)\)$", test_id)
        if m:
            qual = m.group("qual")
            method = m.group("method")
            module, _, cls = qual.rpartition(".")
            if module and cls:
                candidate = Path("tests") / (module.replace(".", "/") + ".py")
                if (worktree / candidate).exists():
                    return f"{module}.{cls}.{method}"
        # Already-normalized pytest-style labels may appear after prior data
        # processing. Convert them back to Django's dotted runner label.
        if "::" in test_id:
            path, *parts = test_id.split("::")
            if path.startswith("tests/") and path.endswith(".py") and len(parts) >= 2:
                module = path.removeprefix("tests/").removesuffix(".py").replace("/", ".")
                if (worktree / path).exists():
                    return ".".join([module] + parts)
        return None

    path_part = test_id.split("::", 1)[0]
    if (worktree / path_part).exists():
        return test_id
    return None


def _existing_nodeids(worktree: Path, tests: list[str], repo: str = "") -> list[str]:
    out: list[str] = []
    python = _find_python(repo, worktree) or PYTHON
    for t in tests:
        normalized = _normalize_test_id(worktree, repo, t)
        if normalized:
            out.append(normalized)
            continue
        if repo == "django/django":
            remapped = _remap_django_test_label(worktree, t)
            out.extend(label for label in remapped if label not in out)
            continue
        if repo != "django/django":
            remapped = _remap_pytest_nodeid(worktree, t, python=python)
            if not remapped:
                remapped = _static_pytest_nodeid_candidates(worktree, t)
            out.extend(nodeid for nodeid in remapped if nodeid not in out)
    return out


def _prune_parent_nodeids(tests: list[str]) -> list[str]:
    """Drop broad pytest selectors already covered by more specific nodeids.

    Pro metadata can contain a file, class, and method for the same target
    behavior. Counting all of them inflates the target budget and running the
    parent selector also executes unrelated tests. Keep the leaf selectors;
    unrelated leaves remain independent targets.
    """

    unique = list(dict.fromkeys(test for test in tests if test))
    return [
        test
        for test in unique
        if not any(other.startswith(f"{test}::") for other in unique if other != test)
    ]


def _target_behavior_family(nodeid: str) -> str:
    """Collapse pytest parameter variants into one target behavior family."""

    if "::" not in nodeid:
        return nodeid
    prefix, selector = nodeid.rsplit("::", 1)
    return f"{prefix}::{selector.split('[', 1)[0]}"


def _target_budget_allows(tests: list[str], max_target_tests: int | None) -> bool:
    """Allow bounded parameter variants without treating each as a new behavior."""

    if max_target_tests is None or len(tests) <= max_target_tests:
        return True
    max_parameter_variants = int(os.environ.get("PRI_MAX_PARAMETER_TARGETS", "24"))
    families = {_target_behavior_family(test) for test in tests}
    return len(tests) <= max_parameter_variants and len(families) <= max_target_tests


def _remap_django_test_label(
    worktree: Path,
    test_id: str,
    max_candidates: int = 20,
) -> list[str]:
    """Conservatively remap stale Django test labels by class/method.

    Django's runner uses dotted labels under tests/. Historical SWE-bench
    labels can point at modules that were moved or renamed. To avoid broad
    false remaps, only return labels when a method/class lookup has a small,
    exact candidate set.
    """

    method = ""
    cls = ""

    m = re.match(r"(?P<method>[\w_]+) \((?P<qual>[\w.]+)\)$", test_id)
    if m:
        method = m.group("method")
        _, _, cls = m.group("qual").rpartition(".")
    elif "::" in test_id:
        _path, *selectors = test_id.split("::")
        if selectors:
            method = _pytest_selector_function(selectors[-1])
            if len(selectors) >= 2:
                cls = selectors[-2].split("[", 1)[0].strip()
    else:
        parts = test_id.split(".")
        if len(parts) >= 2:
            method = parts[-1]
            cls = parts[-2] if parts[-2][:1].isupper() else ""

    if not method:
        return []

    roots = [worktree / "tests"]
    candidates: list[str] = []
    class_pattern = re.compile(rf"^\s*class\s+{re.escape(cls)}\b") if cls else None
    method_pattern = re.compile(rf"^\s*(?:async\s+def|def)\s+{re.escape(method)}\s*\(")

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if len(candidates) >= max_candidates:
                break
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            method_lines = [idx for idx, line in enumerate(lines) if method_pattern.match(line)]
            if not method_lines:
                continue

            if class_pattern:
                class_lines = [idx for idx, line in enumerate(lines) if class_pattern.match(line)]
                if not class_lines:
                    continue
                # Require the method to appear after the class declaration. This
                # is intentionally simple and only used to avoid obvious remaps
                # to the right method name in the wrong class.
                if not any(method_idx > class_idx for method_idx in method_lines for class_idx in class_lines):
                    continue

            rel = path.relative_to(worktree / "tests").with_suffix("")
            module = ".".join(rel.parts)
            label_parts = [module]
            if cls:
                label_parts.append(cls)
            label_parts.append(method)
            candidates.append(".".join(label_parts))

    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique
    return []


def _remap_pytest_nodeid(
    worktree: Path,
    test_id: str,
    python: str,
    timeout: int = 60,
) -> list[str]:
    """Best-effort remap for stale or truncated pytest nodeids.

    SWE-bench nodeids often drift when test files move or parametrized IDs
    change. This remapper intentionally stays conservative: it only maps to
    collected nodeids with the same test function name, and it prefers the
    original file path or files with the same basename.
    """

    if not test_id or "::" not in test_id:
        return []

    path_part, *selectors = test_id.split("::")
    if not selectors:
        return []

    target_func = _pytest_selector_function(selectors[-1])
    if not target_func:
        return []

    candidate_files = _candidate_pytest_files(worktree, path_part, target_func)

    out: list[str] = []
    for file_path in candidate_files:
        if not file_path.exists() or not file_path.is_file():
            continue
        rel = str(file_path.relative_to(worktree)).replace("\\", "/")
        for nodeid in _pytest_collect_nodeids(worktree, rel, python, timeout=timeout):
            parts = nodeid.split("::")
            if not parts:
                continue
            if _pytest_selector_function(parts[-1]) == target_func:
                out.append(nodeid)
    return list(dict.fromkeys(out))


def _static_pytest_nodeid_candidates(
    worktree: Path,
    test_id: str,
    max_candidates: int = 40,
) -> list[str]:
    """Return conservative runnable-looking nodeids without importing pytest.

    This is used before dependency installation. It prevents moved test files
    from being misclassified as missing just because pytest collection cannot
    run yet in the host interpreter.
    """

    if not test_id or "::" not in test_id:
        return []
    path_part, *selectors = test_id.split("::")
    if not selectors:
        return []

    target_func = _pytest_selector_function(selectors[-1])
    if not target_func:
        return []

    selector_parts = [_pytest_selector_function(part) for part in selectors]
    selector = "::".join(part for part in selector_parts if part)
    out: list[str] = []
    for file_path in _candidate_pytest_files(worktree, path_part, target_func):
        if len(out) >= max_candidates:
            break
        rel = str(file_path.relative_to(worktree)).replace("\\", "/")
        out.append(f"{rel}::{selector}")
    return list(dict.fromkeys(out))


def _candidate_pytest_files(
    worktree: Path,
    path_part: str,
    target_func: str,
    max_basename_matches: int = 30,
    max_symbol_matches: int = 80,
) -> list[Path]:
    candidate_files: list[Path] = []
    original = worktree / path_part
    if original.exists():
        candidate_files.append(original)
        return candidate_files

    basename = Path(path_part).name
    search_roots = [worktree / "tests", worktree / "test", worktree / "testing"]
    parts = Path(path_part).parts
    if parts:
        package_root = worktree / parts[0]
        if package_root.exists() and package_root.is_dir():
            search_roots.append(package_root)
    search_roots = list(dict.fromkeys(search_roots))
    for root in search_roots:
        if root.exists():
            candidate_files.extend(sorted(root.rglob(basename))[:max_basename_matches])

    if candidate_files:
        return list(dict.fromkeys(candidate_files))

    needles = (
        f"def {target_func}(",
        f"async def {target_func}(",
    )
    for root in search_roots:
        if not root.exists():
            continue
        for file_path in sorted(root.rglob("*.py")):
            if len(candidate_files) >= max_symbol_matches:
                break
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(needle in text for needle in needles):
                candidate_files.append(file_path)
        if len(candidate_files) >= max_symbol_matches:
            break

    if not candidate_files:
        skip_parts = {
            ".git",
            ".hg",
            ".tox",
            ".venv",
            "venv",
            "__pycache__",
            "site-packages",
            "build",
            "dist",
            "node_modules",
        }
        for file_path in sorted(worktree.rglob("*.py")):
            if len(candidate_files) >= max_symbol_matches:
                break
            rel_parts = set(file_path.relative_to(worktree).parts)
            if rel_parts & skip_parts:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(needle in text for needle in needles):
                candidate_files.append(file_path)

    return list(dict.fromkeys(candidate_files))


def _pytest_selector_function(selector: str) -> str:
    """Return the function part of a pytest selector, ignoring parameters."""

    selector = selector.split("[", 1)[0]
    return selector.strip()


def _pytest_collect_nodeids(
    worktree: Path,
    test_file: str,
    python: str,
    timeout: int = 60,
) -> list[str]:
    try:
        proc = subprocess.run(
            [python, "-m", "pytest", *_pytest_config_args(worktree), "--collect-only", "-q", test_file],
            cwd=worktree,
            capture_output=True,
            timeout=_collection_timeout(timeout),
        )
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    output = proc.stdout.decode(errors="replace") + "\n" + proc.stderr.decode(errors="replace")
    nodeids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith("<"):
            nodeids.append(_worktree_relative_nodeid(worktree, line))
    return nodeids


def _adjacent_pass_to_pass_candidates(
    worktree: Path,
    repo: str,
    target_tests: list[str],
    existing_pass_to_pass: list[str],
    python: str,
    max_tests: int = 25,
    timeout: int = 60,
    target_exclusions: list[str] | None = None,
) -> list[str]:
    """Collect extra nearby tests to make injected-task P2P less local.

    The goal is not broad full-suite coverage; it is to add adjacent behavior
    from the same test files/directories as the injected failure while avoiding
    the FAIL_TO_PASS nodeids themselves. Django's dotted labels need separate
    runner semantics, so this conservative helper currently targets pytest
    nodeids only.
    """

    if max_tests <= 0 or repo == "django/django":
        return []

    target_set = set(target_tests)
    target_set.update(target_exclusions or [])
    existing_set = set(existing_pass_to_pass)
    candidate_files: list[str] = []
    for nodeid in target_tests:
        path = nodeid.split("::", 1)[0]
        if path.endswith(".py") and (worktree / path).exists():
            candidate_files.append(path)

    for path in list(candidate_files):
        parent = (worktree / path).parent
        if not parent.exists():
            continue
        for sibling in sorted(parent.glob("test*.py"))[:12]:
            rel = str(sibling.relative_to(worktree)).replace("\\", "/")
            candidate_files.append(rel)

    out: list[str] = []
    for rel in list(dict.fromkeys(candidate_files)):
        for nodeid in _pytest_collect_nodeids(worktree, rel, python, timeout=timeout):
            if nodeid in target_set or nodeid in existing_set or nodeid in out:
                continue
            out.append(nodeid)
            if len(out) >= max_tests:
                return out
    return out


def verify_instance(
    injection_result: dict,
    original_data: dict,
    repos_dir: Path,
    worktrees_dir: Path,
    test_timeout: int,
    check_pass_to_pass: bool = False,
    max_pass_to_pass: int = 50,
    check_golden_repair: bool = False,
    max_target_tests: int | None = None,
    clean_pass_to_pass: bool = False,
    require_clean_pass_to_pass: bool = False,
    include_adjacent_pass_to_pass: bool = True,
    max_adjacent_pass_to_pass: int = 25,
) -> dict:
    """Verify a single injected instance."""

    iid = injection_result["instance_id"]
    repo = injection_result["repo"]
    level = injection_result.get("injection_level", "?")
    healthy_head = injection_result.get("healthy_head", "")
    patch = original_data.get("patch", "")

    # Test info from original dataset. Prefer fail_to_pass nodeids when present:
    # running whole files can turn unrelated baseline failures into false negatives.
    target_test_files = _coerce_list(original_data.get("selected_test_files_to_run", []))
    injected_fail_to_pass = _coerce_list(injection_result.get("fail_to_pass", []))
    original_fail_to_pass = _coerce_list(original_data.get("fail_to_pass", []))
    fail_to_pass_raw = injected_fail_to_pass or original_fail_to_pass
    if (
        injected_fail_to_pass
        and original_fail_to_pass
        and _target_budget_allows(original_fail_to_pass, max_target_tests)
    ):
        # Preflight may have sampled a large parameterized target family. Strict
        # verification can cheaply restore the full bounded family so omitted
        # official FAIL_TO_PASS variants are not mislabeled as adjacent P2P.
        fail_to_pass_raw = list(dict.fromkeys([
            *injected_fail_to_pass,
            *original_fail_to_pass,
        ]))
    pass_to_pass_raw = _coerce_list(original_data.get("pass_to_pass", []))

    target_tests_to_run = fail_to_pass_raw or target_test_files

    short_id = iid[:60]
    print(f"\n{'━' * 70}")
    print(f"  {short_id}")
    print(f"  repo={repo}  level={level}  target_tests={len(target_tests_to_run)}")
    print(f"  expected fail_to_pass: {len(fail_to_pass_raw)} test(s)")
    print(f"{'━' * 70}")

    result = {
        "instance_id": iid,
        "repo": repo,
        "injection_level": level,
        "verification": None,
    }

    if not target_tests_to_run:
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
    ensure_repo_head(repo_dir, default_branch)
    checkout_ref, checkout_ref_source = _verification_checkout_ref(
        repo_dir, str(healthy_head or ""), default_branch
    )

    # Create worktree. Use a hash of the full instance id because many
    # SWE-bench Pro ids share the same trailing commit suffix.
    run_key = _stable_id_suffix(repo, iid)
    wt_name = f"verify-{repo.replace('/', '-')}-{run_key}"
    wt_path = (worktrees_dir / wt_name).resolve()
    branch = f"vfy-{run_key}"
    r = None
    for attempt in range(2):
        cleanup_worktree_branch(repo_dir, branch, wt_path)
        r = git("worktree", "add", "-b", branch, str(wt_path), checkout_ref,
                cwd=str(repo_dir), timeout=1200)
        if r.returncode == 0:
            break
    if r.returncode != 0:
        stderr = r.stderr.decode(errors="replace") if r.stderr else ""
        print(f"  [SKIP] Worktree creation failed: {stderr[-300:]}")
        result["verification"] = {
            "status": "skipped",
            "reason": "worktree_failed",
            "stderr_tail": stderr[-500:],
        }
        return result

    start_time = time.monotonic()
    result["verification_checkout_ref"] = checkout_ref
    result["verification_checkout_ref_source"] = checkout_ref_source
    test_metrics: dict = {
        "test_command_count": 0,
        "test_nodeid_count": 0,
        "test_command_count_by_phase": {},
        "test_nodeid_count_by_phase": {},
        "test_command_duration_seconds_by_phase": {},
    }

    try:
        # Check which test files exist. Pytest nodeids include "::"; only the path
        # component should be checked on disk.
        initial_existing_tests = _existing_nodeids(wt_path, target_tests_to_run, repo)
        tests_for_collect = initial_existing_tests or target_tests_to_run
        print(f"  Target tests: {tests_for_collect}")

        # Create isolated venv for this worktree
        print(f"  [0/3] Creating isolated venv & installing dependencies...")
        venv_python = _create_venv(str(wt_path), repo)
        if not venv_python:
            result["verification"] = {
                "status": "skipped",
                "reason": "python_version_unavailable",
                "requires_python": _read_requires_python(wt_path),
            }
            return result
        install_ok = _install_project(str(wt_path), repo, test_timeout,
                                      python=venv_python, test_files=tests_for_collect)
        if not install_ok:
            print(f"  [WARN] Dependency install may have issues, proceeding anyway")
        if not _test_runner_available(str(wt_path), repo, venv_python):
            print("  [SKIP] Test runner is unavailable after dependency installation")
            result["verification"] = {
                "status": "skipped",
                "reason": "test_runner_unavailable",
                "healthy_pass": False,
            }
            return result
        existing_tests = _prune_parent_nodeids(_collectable_tests(
            str(wt_path), repo, tests_for_collect, venv_python
        ))
        if not existing_tests:
            print("  [SKIP] No target test nodeids are collectable on HEAD")
            result["verification"] = {
                "status": "skipped",
                "reason": "target_nodeids_not_collectable"
                if initial_existing_tests else "target_nodeids_not_remappable",
                "healthy_pass": False,
                "raw_target_tests": target_tests_to_run,
                "initial_existing_target_tests": initial_existing_tests,
                "test_metrics": test_metrics,
            }
            return result
        if not initial_existing_tests:
            print(
                "  [target-remap] Target nodeids remapped: "
                f"{len(target_tests_to_run)} raw → {len(existing_tests)} collectable"
            )
        collectable_target_count_before_healthy_filter = len(existing_tests)
        if not _target_budget_allows(existing_tests, max_target_tests):
            print(
                "  [SKIP] Too many collectable target tests "
                f"({len(existing_tests)} > {max_target_tests})"
            )
            result["verification"] = {
                "status": "skipped",
                "reason": "too_many_target_tests",
                "target_test_count": len(existing_tests),
                "target_behavior_family_count": len({
                    _target_behavior_family(test) for test in existing_tests
                }),
                "max_target_tests": max_target_tests,
                "collectable_target_tests": existing_tests[:20],
                "test_metrics": test_metrics,
            }
            return result
        print(f"  Collectable target tests: {existing_tests}")

        p2p_existing_tests: list[str] = []
        p2p_healthy_failed_tests: list[str] = []
        if check_pass_to_pass:
            if pass_to_pass_raw:
                p2p_existing_tests = _existing_nodeids(
                    wt_path, pass_to_pass_raw, repo
                )[:max_pass_to_pass]
                if p2p_existing_tests:
                    p2p_existing_tests = _collectable_tests(
                        str(wt_path), repo, p2p_existing_tests, venv_python
                    )
                    if max_pass_to_pass is not None:
                        p2p_existing_tests = p2p_existing_tests[:max_pass_to_pass]

            adjacent_p2p_tests: list[str] = []
            if include_adjacent_pass_to_pass:
                adjacent_p2p_tests = _adjacent_pass_to_pass_candidates(
                    wt_path,
                    repo,
                    existing_tests,
                    p2p_existing_tests,
                    venv_python,
                    max_tests=max_adjacent_pass_to_pass,
                    timeout=test_timeout,
                    # Manual/semantic transplantation may provide modern
                    # equivalent target nodeids in the injection row. Those
                    # are authoritative for this B case and must never be
                    # reclassified as adjacent P2P tests merely because the
                    # historical benchmark metadata used older nodeids.
                    target_exclusions=_prune_parent_nodeids(
                        _existing_nodeids(wt_path, fail_to_pass_raw, repo)
                    ),
                )
                if adjacent_p2p_tests:
                    print(
                        "  [p2p-adjacent] Added "
                        f"{len(adjacent_p2p_tests)} same-module/sibling tests"
                    )
                    p2p_existing_tests = list(dict.fromkeys([*p2p_existing_tests, *adjacent_p2p_tests]))

            if clean_pass_to_pass and p2p_existing_tests:
                print(
                    "  [p2p-clean] Healthy filter: running "
                    f"{len(p2p_existing_tests)} pass_to_pass tests individually..."
                )
                p2p_existing_tests, p2p_healthy_failed_tests = _filter_passing_tests(
                    str(wt_path), repo, p2p_existing_tests,
                    python=venv_python, timeout=test_timeout,
                    metrics=test_metrics,
                    phase="p2p_healthy_filter",
                    cache_key_prefix=(
                        f"{repo}:{healthy_head}:{_python_version_tuple(venv_python)}"
                    ),
                )
                print(
                    f"        healthy-clean={len(p2p_existing_tests)} "
                    f"healthy-failed={len(p2p_healthy_failed_tests)}"
                )

        # ── Step 1: Healthy check (target tests should PASS) ──
        print(f"  [1/3] Healthy check: running target tests on clean HEAD...")
        healthy_result = run_repo_tests(str(wt_path), repo, existing_tests,
                                        timeout=test_timeout, python=venv_python)
        _record_test_metrics(test_metrics, "target_healthy", existing_tests, healthy_result)
        healthy_executed = int(healthy_result.get("total") or 0) > 0
        target_execution_fallback = None
        if not healthy_executed:
            fallback_tests, fallback_result = _retry_with_target_execution_fallback(
                str(wt_path),
                repo,
                existing_tests,
                venv_python,
                test_timeout,
                metrics=test_metrics,
                phase="target_healthy_execution_fallback",
            )
            if fallback_result:
                target_execution_fallback = {
                    "from": existing_tests,
                    "to": fallback_tests,
                    "result": fallback_result,
                }
                fallback_executed = int(fallback_result.get("total") or 0) > 0
                if fallback_executed:
                    existing_tests = fallback_tests
                    healthy_result = fallback_result
                    healthy_executed = True
        target_healthy_minimized_from_failure = False
        target_healthy_failed_tests: list[str] = []
        if healthy_result["returncode"] != 0 and healthy_executed and len(existing_tests) > 1:
            print(
                "  [target-clean] Healthy target group failed; "
                "minimizing to individually passing target tests..."
            )
            passing, failing = _filter_passing_tests(
                str(wt_path), repo, existing_tests,
                python=venv_python, timeout=test_timeout,
                metrics=test_metrics,
                phase="target_healthy_minimize",
            )
            target_healthy_failed_tests = failing
            if passing:
                existing_tests = passing[:max_target_tests] if max_target_tests is not None else passing
                healthy_result = run_repo_tests(
                    str(wt_path), repo, existing_tests,
                    timeout=test_timeout, python=venv_python
                )
                _record_test_metrics(test_metrics, "target_healthy_minimized", existing_tests, healthy_result)
                healthy_executed = int(healthy_result.get("total") or 0) > 0
                target_healthy_minimized_from_failure = True
        healthy_pass = healthy_result["returncode"] == 0 and healthy_executed
        print(f"        rc={healthy_result['returncode']} passed={healthy_result['passed']} "
              f"failed={healthy_result['failed']} → {'PASS' if healthy_pass else 'FAIL'}")

        if not healthy_pass:
            print(f"  [WARN] Target tests already fail on healthy HEAD!")
            print(f"         {healthy_result['output_tail'][-300:]}")
            result["verification"] = {
                "status": "baseline_failed",
                "reason": "healthy_target_failed" if healthy_executed else "healthy_target_not_executed",
                "healthy_pass": False,
                "healthy_result": healthy_result,
                "target_tests": existing_tests,
                "target_test_count": len(existing_tests),
                "target_healthy_minimized_from_failure": target_healthy_minimized_from_failure,
                "target_healthy_failed_tests": target_healthy_failed_tests[:10],
                "target_execution_fallback": target_execution_fallback,
                "pass_to_fail": False,
                "test_metrics": test_metrics,
            }
            return result

        # ── Step 2: Apply saved diff ──
        print(f"  [2/3] Applying saved diff...")
        diff_rel_path = injection_result.get("injected_diff", "")

        if not diff_rel_path:
            print(f"  [FAIL] No injected_diff path in result")
            result["verification"] = {"status": "diff_file_missing", "test_metrics": test_metrics}
            return result

        project_root = Path(__file__).resolve().parents[3]
        diff_path = project_root / diff_rel_path

        if not diff_path.exists():
            print(f"  [FAIL] Diff file not found: {diff_path}")
            result["verification"] = {"status": "diff_file_missing", "test_metrics": test_metrics}
            return result

        proc = subprocess.run(
            ["git", "apply", str(diff_path)],
            cwd=str(wt_path),
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            print(f"  [FAIL] git apply failed: {proc.stderr.decode()[:200]}")
            result["verification"] = {"status": "diff_apply_failed", "test_metrics": test_metrics}
            return result

        diff_size = len(git_text("diff", cwd=str(wt_path)))
        print(f"        Bug injected, diff size: {diff_size} chars")

        # ── Step 3: Pass-to-fail check (target tests should FAIL) ──
        print(f"  [3/3] P2F check: running target tests on buggy revision...")
        buggy_result = run_repo_tests(str(wt_path), repo, existing_tests,
                                      timeout=test_timeout, python=venv_python)
        _record_test_metrics(test_metrics, "target_buggy", existing_tests, buggy_result)
        buggy_executed = int(buggy_result.get("total") or 0) > 0
        target_failed = buggy_result["returncode"] != 0 and buggy_executed
        all_verified_targets_failed = (
            target_failed
            and int(buggy_result.get("passed") or 0) == 0
            and int(buggy_result.get("failed") or 0) == int(buggy_result.get("total") or 0)
        )
        print(f"        rc={buggy_result['returncode']} passed={buggy_result['passed']} "
              f"failed={buggy_result['failed']} → {'FAIL (good!)' if target_failed else 'PASS (bad!)'}")

        p2p_buggy_result = None
        repaired_result = None
        p2p_repaired_result = None
        p2p_buggy_failed_tests: list[str] = []
        p2p_repaired_failed_tests: list[str] = []
        clean_p2p_tests: list[str] = []
        no_regression = None
        golden_repair_pass = None
        p2p_repaired_pass = None
        if check_pass_to_pass and healthy_pass and target_failed:
            if p2p_existing_tests:
                print(
                    "  [4/4] No-regression check: running "
                    f"{len(p2p_existing_tests)} pass_to_pass tests..."
                )
                if clean_pass_to_pass:
                    clean_p2p_tests, p2p_buggy_failed_tests = _filter_passing_tests(
                        str(wt_path), repo, p2p_existing_tests,
                        python=venv_python, timeout=test_timeout,
                        metrics=test_metrics,
                        phase="p2p_buggy_filter",
                    )
                    p2p_buggy_result = {
                        "returncode": 0 if len(clean_p2p_tests) == len(p2p_existing_tests) else 1,
                        "passed": len(clean_p2p_tests),
                        "failed": len(p2p_buggy_failed_tests),
                        "total": len(p2p_existing_tests),
                        "failed_tests": p2p_buggy_failed_tests,
                    }
                    no_regression = len(p2p_buggy_failed_tests) == 0
                    print(
                        f"        buggy-clean={len(clean_p2p_tests)} "
                        f"buggy-failed={len(p2p_buggy_failed_tests)}"
                    )
                else:
                    p2p_buggy_result = run_repo_tests(
                        str(wt_path), repo, p2p_existing_tests,
                        timeout=test_timeout, python=venv_python
                    )
                    _record_test_metrics(test_metrics, "p2p_buggy", p2p_existing_tests, p2p_buggy_result)
                    no_regression = p2p_buggy_result["returncode"] == 0
                    clean_p2p_tests = p2p_existing_tests if no_regression else []
                    print(f"        rc={p2p_buggy_result['returncode']} passed={p2p_buggy_result['passed']} "
                          f"failed={p2p_buggy_result['failed']} → {'PASS' if no_regression else 'FAIL'}")
            elif require_clean_pass_to_pass:
                no_regression = False

        if check_golden_repair and healthy_pass and target_failed:
            print("  [repair] Applying golden repair by reversing injected diff...")
            proc = subprocess.run(
                ["git", "apply", "-R", str(diff_path)],
                cwd=str(wt_path),
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                err = proc.stderr.decode(errors="replace")[:200]
                print(f"        golden repair apply failed: {err}")
                golden_repair_pass = False
            else:
                repaired_result = run_repo_tests(
                    str(wt_path), repo, existing_tests,
                    timeout=test_timeout, python=venv_python
                )
                _record_test_metrics(test_metrics, "target_repaired", existing_tests, repaired_result)
                repaired_executed = int(repaired_result.get("total") or 0) > 0
                golden_repair_pass = repaired_result["returncode"] == 0 and repaired_executed
                print(f"        target rc={repaired_result['returncode']} "
                      f"passed={repaired_result['passed']} failed={repaired_result['failed']} "
                      f"→ {'PASS' if golden_repair_pass else 'FAIL'}")
                if clean_p2p_tests:
                    if clean_pass_to_pass:
                        clean_p2p_tests, p2p_repaired_failed_tests = _filter_passing_tests(
                            str(wt_path), repo, clean_p2p_tests,
                            python=venv_python, timeout=test_timeout,
                            metrics=test_metrics,
                            phase="p2p_repaired_filter",
                        )
                        p2p_repaired_result = {
                            "returncode": 0 if not p2p_repaired_failed_tests else 1,
                            "passed": len(clean_p2p_tests),
                            "failed": len(p2p_repaired_failed_tests),
                            "total": len(clean_p2p_tests) + len(p2p_repaired_failed_tests),
                            "failed_tests": p2p_repaired_failed_tests,
                        }
                        p2p_repaired_pass = len(p2p_repaired_failed_tests) == 0 and bool(clean_p2p_tests)
                        print(
                            f"        p2p repaired-clean={len(clean_p2p_tests)} "
                            f"repaired-failed={len(p2p_repaired_failed_tests)}"
                        )
                    else:
                        p2p_repaired_result = run_repo_tests(
                            str(wt_path), repo, clean_p2p_tests,
                            timeout=test_timeout, python=venv_python
                        )
                        _record_test_metrics(test_metrics, "p2p_repaired", clean_p2p_tests, p2p_repaired_result)
                        p2p_repaired_pass = p2p_repaired_result["returncode"] == 0
                        print(f"        p2p rc={p2p_repaired_result['returncode']} "
                              f"passed={p2p_repaired_result['passed']} "
                              f"failed={p2p_repaired_result['failed']} "
                              f"→ {'PASS' if p2p_repaired_pass else 'FAIL'}")
                elif require_clean_pass_to_pass:
                    p2p_repaired_pass = False

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
            "all_verified_targets_failed": all_verified_targets_failed,
            "pass_to_fail": all_verified_targets_failed and healthy_pass,
            "requested_target_count": len(fail_to_pass_raw),
            "collectable_target_count_before_healthy_filter": collectable_target_count_before_healthy_filter,
            "verified_target_count": len(existing_tests),
            "verified_target_tests": existing_tests,
            "target_retention_ratio": round(
                len(existing_tests) / collectable_target_count_before_healthy_filter, 4
            ) if collectable_target_count_before_healthy_filter else 0.0,
            "target_healthy_minimized_from_failure": target_healthy_minimized_from_failure,
            "target_healthy_failed_tests": target_healthy_failed_tests[:10],
            "healthy_passed": healthy_result["passed"],
            "healthy_failed": healthy_result["failed"],
            "buggy_passed": buggy_result["passed"],
            "buggy_failed": buggy_result["failed"],
            "buggy_total": buggy_result["total"],
            "expected_fail_count": len(fail_to_pass_raw),
            "expected_matched": expected_matched,
            "actual_failed_tests": buggy_result.get("failed_tests", [])[:10],
            "duration_seconds": round(duration, 2),
            "pass_to_pass_checked": bool(p2p_existing_tests),
            "pass_to_pass_test_count": len(p2p_existing_tests),
            "adjacent_pass_to_pass_enabled": include_adjacent_pass_to_pass,
            "max_adjacent_pass_to_pass": max_adjacent_pass_to_pass,
            "no_regression": no_regression,
            "clean_pass_to_pass": clean_p2p_tests,
            "clean_pass_to_pass_count": len(clean_p2p_tests),
            "p2p_healthy_failed_tests": p2p_healthy_failed_tests[:10],
            "p2p_buggy_passed": p2p_buggy_result["passed"] if p2p_buggy_result else 0,
            "p2p_buggy_failed": p2p_buggy_result["failed"] if p2p_buggy_result else 0,
            "p2p_buggy_failed_tests": (
                p2p_buggy_result.get("failed_tests", [])[:10] if p2p_buggy_result else []
            ),
            "golden_repair_checked": bool(check_golden_repair and healthy_pass and target_failed),
            "golden_repair_pass": golden_repair_pass,
            "repaired_passed": repaired_result["passed"] if repaired_result else 0,
            "repaired_failed": repaired_result["failed"] if repaired_result else 0,
            "repaired_failed_tests": (
                repaired_result.get("failed_tests", [])[:10] if repaired_result else []
            ),
            "p2p_repaired_pass": p2p_repaired_pass,
            "p2p_repaired_passed": p2p_repaired_result["passed"] if p2p_repaired_result else 0,
            "p2p_repaired_failed": p2p_repaired_result["failed"] if p2p_repaired_result else 0,
            "p2p_repaired_failed_tests": p2p_repaired_failed_tests[:10],
            "test_metrics": test_metrics,
        }

        p2f_ok = all_verified_targets_failed and healthy_pass

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


def main():
    parser = argparse.ArgumentParser(description="Verify injected SWE-bench Pro instances")
    parser.add_argument("--injection-results", default="artifacts/bug-run/injection_results.jsonl")
    parser.add_argument("--sampled-data", default="artifacts/bug-pool/candidate_pool.jsonl")
    parser.add_argument("--output", "-o", default="artifacts/bug-run/verification_results.jsonl")
    parser.add_argument("--timeout", "-t", type=int, default=300)
    parser.add_argument("--filter", "-f", type=str, default=None)
    parser.add_argument("--max", "-n", type=int, default=None)
    parser.add_argument("--repos-dir", default=".pri-workspace/repos")
    parser.add_argument("--worktrees-dir", default=".pri-workspace/worktrees")
    parser.add_argument("--check-pass-to-pass", action="store_true",
                        help="Run pass_to_pass tests on the injected revision after P2F succeeds")
    parser.add_argument("--clean-pass-to-pass", action="store_true",
                        help="Filter pass_to_pass tests through healthy, buggy, and repaired states individually")
    parser.add_argument("--require-clean-pass-to-pass", action="store_true",
                        help="Require at least one pass_to_pass test to pass in all checked states")
    parser.add_argument("--max-pass-to-pass", type=int, default=50,
                        help="Maximum pass_to_pass nodeids to run per instance")
    parser.add_argument("--check-golden-repair", action="store_true",
                        help="After P2F, reverse the injected diff and run target tests again")
    parser.add_argument("--max-target-tests", type=int, default=None,
                        help="Skip instances with more target nodeids than this")
    parser.add_argument("--no-adjacent-pass-to-pass", action="store_true",
                        help="Disable same-file/sibling-test P2P expansion")
    parser.add_argument("--max-adjacent-pass-to-pass", type=int, default=25,
                        help="Maximum adjacent P2P nodeids to add per instance")
    parser.add_argument("--force", action="store_true",
                        help="Re-run instances even if an output verification row already exists")
    args = parser.parse_args()

    # Setup log file (tee stdout to file)
    output_path = Path(args.output)
    log_path = output_path.parent / (output_path.stem.replace("_results", "") + ".log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, log_file)

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

    results_by_id: dict[str, dict] = {}
    if output_path.exists() and not args.force:
        with open(output_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if row.get("instance_id"):
                        results_by_id[row["instance_id"]] = row

    for i, inj in enumerate(to_verify, 1):
        iid = inj["instance_id"]
        orig = sampled.get(iid, {})

        print(f"\n[{i}/{len(to_verify)}]", end="")
        existing_verification = (
            (results_by_id.get(iid) or {}).get("verification") or {}
        )
        retryable_existing_error = existing_verification.get("status") == "error"
        if iid in results_by_id and not args.force and not retryable_existing_error:
            existing = results_by_id[iid]
            verification = existing.get("verification") or {}
            print(
                f"  [SKIP existing] {iid[:60]} "
                f"status={verification.get('status')} p2f={verification.get('pass_to_fail')}",
                flush=True,
            )
            continue

        try:
            result = verify_instance(
                inj, orig, repos_dir, worktrees_dir, args.timeout,
                check_pass_to_pass=args.check_pass_to_pass,
                max_pass_to_pass=args.max_pass_to_pass,
                check_golden_repair=args.check_golden_repair,
                max_target_tests=args.max_target_tests,
                clean_pass_to_pass=args.clean_pass_to_pass,
                require_clean_pass_to_pass=args.require_clean_pass_to_pass,
                include_adjacent_pass_to_pass=not args.no_adjacent_pass_to_pass,
                max_adjacent_pass_to_pass=args.max_adjacent_pass_to_pass,
            )
        except Exception as e:
            print(f"  [ERROR] {e}")
            result = {
                "instance_id": iid,
                "repo": inj["repo"],
                "verification": {"status": "error", "error": str(e)[:300]},
            }

        results_by_id[iid] = result

        # Incremental save
        with open(args.output, "w", encoding="utf-8") as f:
            for inj_out in to_verify:
                r = results_by_id.get(inj_out["instance_id"])
                if r is not None:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Summary
    final_rows = [
        results_by_id[inj["instance_id"]]
        for inj in to_verify
        if inj["instance_id"] in results_by_id
    ]
    stats = {"total": len(final_rows), "p2f_confirmed": 0, "p2f_failed": 0,
             "healthy_already_fail": 0, "skipped": 0}
    for result in final_rows:
        v = result.get("verification", {})
        if v.get("status") == "skipped":
            stats["skipped"] += 1
        elif v.get("pass_to_fail"):
            stats["p2f_confirmed"] += 1
        elif v.get("status") == "completed" and not v.get("healthy_pass"):
            stats["healthy_already_fail"] += 1
        elif v.get("status") == "completed":
            stats["p2f_failed"] += 1
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
    print(f"  Log saved to: {log_path}")


if __name__ == "__main__":
    main()
