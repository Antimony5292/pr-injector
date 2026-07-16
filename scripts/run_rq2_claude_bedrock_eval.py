"""Run RQ2 A/B agent evaluation with Claude Code over AWS Bedrock.

The runner is resumable. It writes one JSON result per case/group pair.

Protocol:
  A: checkout the official original benchmark base_commit, run the agent on the
     problem statement, apply test_patch only for evaluation, then run tests.
  B: checkout the PR-INJECTOR healthy_head, apply injected_diff to create the
     buggy revision, optionally seal that buggy tree as a clean baseline, run
     the agent on the same problem statement, then run tests.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import textwrap
import time
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PAIRING = ROOT / "experiments" / "rq2_100" / "rq2_b_p2f_100_final" / "rq2_pairing_table.jsonl"
CLAUDE_WRAPPER = Path("/Users/harmin/Desktop/BenchInject/BenchInject-file/scripts/run_claude_inject_bedrock.py")
CODEX_BUNDLED_PYTHON = Path("/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3")
DEFAULT_MODEL = "arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6"
CLAUDE_BIN = shutil.which("claude") or "claude"
DEFAULT_CODEX_RUNNER = ROOT / "scripts" / "run_codex_headless.py"
DEFAULT_OPENCODE_RUNNER = ROOT / "scripts" / "run_opencode_headless.py"
DEFAULT_OPENHANDS_RUNNER = ROOT / "scripts" / "run_openhands_headless.py"

TRANSIENT_CLAUDE_ERRORS = (
    "API Error: Connection error",
    "Could not load credentials from any providers",
    "ECONNRESET",
    "ETIMEDOUT",
    "ENOTFOUND",
    "EAI_AGAIN",
    "socket hang up",
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailable",
    "InternalServerException",
    "ModelTimeoutException",
)

GENERIC_INFRA_STOP_ERRORS = (
    "You've hit your usage limit",
    "usage limit",
    "purchase more credits",
    "try again at",
    "not logged in",
    "authentication",
    "Unauthorized",
    "AccessDenied",
    "Could not load credentials from any providers",
    "Failed to run the query 'PRAGMA wal_checkpoint(PASSIVE)'",
    "PRAGMA wal_checkpoint",
)

GENERIC_INFRA_RETRY_ERRORS = (
    "APIConnectionError",
    "ConnectionError",
    "ReadTimeout",
    "ConnectTimeout",
    "Connection reset",
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailable",
    "InternalServerException",
    "ModelTimeoutException",
    "ECONNRESET",
    "ETIMEDOUT",
    "ENOTFOUND",
    "EAI_AGAIN",
    "socket hang up",
)

FORBIDDEN_AGENT_EDIT_PATTERNS = [
    "test/**",
    "tests/**",
    "testing/**",
    "**/test/**",
    "**/tests/**",
    "**/testing/**",
    "test_*.py",
    "*_test.py",
    "**/test_*.py",
    "**/*_test.py",
    "conftest.py",
    "**/conftest.py",
    "*_fixture.py",
    "**/*_fixture.py",
    "tox.ini",
    "noxfile.py",
    ".github/**",
    ".gitlab-ci.yml",
    "azure-pipelines.yml",
    "pytest.ini",
    "setup.cfg",
    "pyproject.toml",
]


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def run(cmd: list[str], cwd: Path, timeout: int = 600, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env)


def claude_wrapper_python() -> str:
    override = os.environ.get("RQ2_CLAUDE_WRAPPER_PYTHON")
    candidates = [override, str(CODEX_BUNDLED_PYTHON), "/usr/bin/python3", sys.executable]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() or shutil.which(candidate):
            return candidate
    return sys.executable


def terminate_process_group(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except Exception:
        return
    for sig, delay in [(signal.SIGTERM, 2), (signal.SIGKILL, 0)]:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except Exception:
            pass
        if delay:
            time.sleep(delay)


def agent_completed_successfully(result: dict) -> bool:
    evaluation = result.get("evaluation") or {}
    if evaluation.get("status") in {"error", "agent_failed_or_timed_out", "agent_infra_blocked"}:
        return False
    agent = result.get("agent") or {}
    raw = agent.get("raw") or {}
    if not agent and "evaluation" in result:
        return False
    if agent.get("timed_out"):
        return False
    for returncode in (agent.get("returncode"), raw.get("returncode")):
        if isinstance(returncode, int) and returncode != 0:
            return False
    return True


class InfrastructureError(RuntimeError):
    """A transient provider/local-credential failure, not a benchmark result."""


def compact_text(value: object, limit: int = 1200) -> str:
    if value is None:
        return ""
    text = str(value)
    text = " ".join(text.split())
    return text[:limit]


def agent_error_text(payload: dict) -> str:
    raw = payload.get("raw") if isinstance(payload, dict) else {}
    parts = [
        payload.get("stdout"),
        payload.get("stderr"),
        payload.get("result"),
        payload.get("opencode_error"),
    ]
    if isinstance(raw, dict):
        parts.extend(
            [
                raw.get("stdout"),
                raw.get("stderr"),
                raw.get("result"),
                raw.get("opencode_error"),
                raw.get("last_error"),
                raw.get("failure_class"),
            ]
        )
    return compact_text("\n".join(str(p) for p in parts if p), 2000)


def is_generic_infra_failure(payload: dict) -> bool:
    raw = payload.get("raw") if isinstance(payload, dict) else {}
    if isinstance(raw, dict) and raw.get("failure_class") == "bedrock_transient":
        return True
    if payload.get("timed_out") or (isinstance(raw, dict) and raw.get("failure_class") == "timeout"):
        return False
    text = agent_error_text(payload)
    if not text:
        return False
    return any(pattern in text for pattern in GENERIC_INFRA_STOP_ERRORS + GENERIC_INFRA_RETRY_ERRORS)


def is_retryable_generic_infra_failure(payload: dict) -> bool:
    raw = payload.get("raw") if isinstance(payload, dict) else {}
    if isinstance(raw, dict) and raw.get("retryable_infra") is True:
        return True
    text = agent_error_text(payload)
    if any(pattern in text for pattern in GENERIC_INFRA_STOP_ERRORS):
        return False
    return any(pattern in text for pattern in GENERIC_INFRA_RETRY_ERRORS)


def claude_home() -> Path:
    path = ROOT / ".pri-workspace" / "claude-home"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".claude").mkdir(parents=True, exist_ok=True)
    return path


def python_shims_dir() -> Path:
    """Keep agent shell commands away from macOS Framework Python.app."""

    shim_dir = ROOT / ".pri-workspace" / "python-shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    bundled = CODEX_BUNDLED_PYTHON
    if not bundled.exists():
        return shim_dir
    for name in ("python", "python3", "python3.12", "python3.13"):
        shim = shim_dir / name
        shim.write_text(f"#!/usr/bin/env bash\nexec {bundled} \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
    return shim_dir


def build_claude_env(aws_region: str, aws_profile: str, model: str) -> dict[str, str]:
    env = os.environ.copy()
    home = claude_home()
    shims = python_shims_dir()
    aws_credentials = Path("/Users/harmin/.aws/credentials")
    aws_config = Path("/Users/harmin/.aws/config")

    env.setdefault("PYTHONFAULTHANDLER", "1")
    env.setdefault("PYTHONNOUSERSITE", "1")
    env["PATH"] = f"{shims}:{env.get('PATH', '')}"
    env["PYTHON_BIN"] = str(CODEX_BUNDLED_PYTHON)
    env["HOME"] = str(home)
    env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
    env["CLAUDE_CODE_USE_BEDROCK"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    env["AWS_REGION"] = aws_region
    env["AWS_DEFAULT_REGION"] = aws_region
    env["AWS_PROFILE"] = aws_profile
    env["AWS_SDK_LOAD_CONFIG"] = "1"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["AWS_RETRY_MODE"] = "standard"
    env["AWS_MAX_ATTEMPTS"] = "10"
    env["ANTHROPIC_MODEL"] = model
    if aws_credentials.exists():
        env["AWS_SHARED_CREDENTIALS_FILE"] = str(aws_credentials)
    if aws_config.exists():
        env["AWS_CONFIG_FILE"] = str(aws_config)
    for key in [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "OPENROUTER_API_KEY",
        "CODEX_API_KEY",
        "GOOGLE_API_KEY",
    ]:
        env.pop(key, None)
    return env


def claude_env_audit(env: dict[str, str]) -> dict:
    credentials = Path(env.get("AWS_SHARED_CREDENTIALS_FILE", ""))
    config = Path(env.get("AWS_CONFIG_FILE", ""))
    return {
        "HOME": env.get("HOME"),
        "PATH_PREFIX": str(python_shims_dir()),
        "PYTHON_BIN": env.get("PYTHON_BIN"),
        "CLAUDE_CONFIG_DIR": env.get("CLAUDE_CONFIG_DIR"),
        "CLAUDE_CODE_USE_BEDROCK": env.get("CLAUDE_CODE_USE_BEDROCK"),
        "AWS_REGION": env.get("AWS_REGION"),
        "AWS_DEFAULT_REGION": env.get("AWS_DEFAULT_REGION"),
        "AWS_PROFILE": env.get("AWS_PROFILE"),
        "AWS_SHARED_CREDENTIALS_FILE": env.get("AWS_SHARED_CREDENTIALS_FILE"),
        "AWS_SHARED_CREDENTIALS_FILE_EXISTS": credentials.exists(),
        "AWS_CONFIG_FILE": env.get("AWS_CONFIG_FILE"),
        "AWS_CONFIG_FILE_EXISTS": config.exists(),
        "AWS_SDK_LOAD_CONFIG": env.get("AWS_SDK_LOAD_CONFIG"),
        "AWS_EC2_METADATA_DISABLED": env.get("AWS_EC2_METADATA_DISABLED"),
        "AWS_RETRY_MODE": env.get("AWS_RETRY_MODE"),
        "AWS_MAX_ATTEMPTS": env.get("AWS_MAX_ATTEMPTS"),
        "ANTHROPIC_MODEL": env.get("ANTHROPIC_MODEL"),
    }


def is_transient_claude_failure(payload: dict) -> bool:
    if payload.get("returncode") in (None, 0):
        return False
    text = "\n".join(
        str(payload.get(key) or "")
        for key in ("stdout", "stderr", "raw")
    )
    return any(marker in text for marker in TRANSIENT_CLAUDE_ERRORS)


def repo_dir(repo: str, repos_dirs: list[Path], ref: str | None = None) -> Path:
    name = repo.replace("/", "__")
    first_existing: Path | None = None
    search_bases: list[Path] = []
    for base in repos_dirs:
        if base not in search_bases:
            search_bases.append(base)
    workspace = ROOT / ".pri-workspace"
    for pattern in ("repos", "repos_*", "*repos*"):
        for base in sorted(workspace.glob(pattern)):
            if base.is_dir() and base not in search_bases:
                search_bases.append(base)

    for base in search_bases:
        candidate = base / name
        if candidate.exists():
            if first_existing is None:
                first_existing = candidate
            if ref:
                check = run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], candidate, timeout=120)
                if check.returncode == 0:
                    return candidate
            else:
                return candidate
    if first_existing is not None and not ref:
        return first_existing
    if first_existing is not None:
        raise FileNotFoundError(f"repo cache found for {repo}, but ref {ref} was absent in searched dirs: {search_bases}")
    raise FileNotFoundError(f"repo not found for {repo} in searched dirs: {search_bases}")


def git_clean(repo: Path) -> None:
    run(["git", "reset", "--hard"], repo, timeout=300)
    run(["git", "clean", "-fdx"], repo, timeout=300)


def create_worktree(repo: Path, worktree: Path, ref: str, branch: str) -> None:
    run(["git", "worktree", "prune"], repo, timeout=120)
    if worktree.exists():
        run(["git", "worktree", "remove", "--force", str(worktree)], repo, timeout=600)
        shutil.rmtree(worktree, ignore_errors=True)
    run(["git", "branch", "-D", branch], repo, timeout=120)
    r = run(["git", "worktree", "add", "-b", branch, str(worktree), ref], repo, timeout=1200)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr[-1000:]}")


def remove_worktree(repo: Path, worktree: Path, branch: str, keep: bool) -> None:
    if keep:
        return
    run(["git", "worktree", "remove", "--force", str(worktree)], repo, timeout=600)
    run(["git", "branch", "-D", branch], repo, timeout=120)
    shutil.rmtree(worktree, ignore_errors=True)


def apply_patch_text(worktree: Path, patch_text: str, label: str) -> dict:
    if not patch_text:
        return {"applied": True, "empty": True}
    proc = subprocess.run(
        ["git", "apply", "-"],
        cwd=str(worktree),
        input=patch_text,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "applied": proc.returncode == 0,
        "returncode": proc.returncode,
        "label": label,
        "stderr": proc.stderr[-2000:],
    }


def apply_patch_file(worktree: Path, rel_path: str, label: str) -> dict:
    patch_path = ROOT / rel_path
    proc = run(["git", "apply", str(patch_path)], worktree, timeout=120)
    return {
        "applied": proc.returncode == 0,
        "returncode": proc.returncode,
        "label": label,
        "patch_path": str(patch_path),
        "stderr": proc.stderr[-2000:],
    }


def git_status_short(worktree: Path) -> str:
    proc = run(["git", "status", "--short"], worktree, timeout=120)
    return proc.stdout if proc.returncode == 0 else ""


def git_diff_text(worktree: Path) -> str:
    proc = run(["git", "diff", "--binary", "HEAD", "--"], worktree, timeout=120)
    return proc.stdout if proc.returncode == 0 else ""


def git_head_parents(worktree: Path) -> list[str]:
    proc = run(["git", "rev-list", "--parents", "-n", "1", "HEAD"], worktree, timeout=120)
    if proc.returncode != 0:
        return []
    parts = proc.stdout.strip().split()
    return parts[1:]


def git_dir(worktree: Path) -> Path | None:
    proc = run(["git", "rev-parse", "--git-dir"], worktree, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path = Path(proc.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    return path.resolve()


def git_common_dir(worktree: Path) -> Path | None:
    proc = run(["git", "rev-parse", "--git-common-dir"], worktree, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        return git_dir(worktree)
    path = Path(proc.stdout.strip())
    if not path.is_absolute():
        path = worktree / path
    return path.resolve()


def install_local_encoding_attribute_override(worktree: Path) -> dict:
    """Disable fragile working-tree-encoding filters in this temporary worktree.

    Some Sphinx fixtures intentionally declare non-UTF-8 encodings. In detached
    benchmark worktrees, `git write-tree` can fail while sealing an injected B
    baseline even though the source tree itself is valid. The override is local
    to `.git/info/attributes` for this disposable worktree and does not change
    repository files or benchmark content.
    """

    gdir = git_common_dir(worktree)
    if gdir is None:
        return {"installed": False, "reason": "git_dir_not_found"}
    info = gdir / "info"
    info.mkdir(parents=True, exist_ok=True)
    attributes = info / "attributes"
    marker = "# PR-INJECTOR local encoding-filter override for temporary RQ2 worktrees"
    lines = [
        marker,
        "tests/roots/test-root/wrongenc.inc -working-tree-encoding",
        "tests/roots/test-warnings/wrongenc.inc -working-tree-encoding",
        "tests/roots/test-pycode/cp_1251_coded.py -working-tree-encoding",
    ]
    existing = attributes.read_text(encoding="utf-8", errors="ignore") if attributes.exists() else ""
    if marker not in existing:
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        attributes.write_text(existing + prefix + "\n".join(lines) + "\n", encoding="utf-8")
    return {"installed": True, "path": str(attributes)}


def seal_b_baseline(worktree: Path, mode: str, case_id: str) -> dict:
    """Make the injected B tree a clean starting point for the agent."""

    if mode == "dirty":
        return {
            "mode": mode,
            "sealed": False,
            "status_before_agent": git_status_short(worktree),
            "diff_size_before_agent": len(git_diff_text(worktree)),
            "head_parents_before_agent": git_head_parents(worktree),
        }

    attr_override = install_local_encoding_attribute_override(worktree)

    add = run(["git", "add", "-A"], worktree, timeout=120)
    if add.returncode != 0:
        return {"mode": mode, "sealed": False, "error": add.stderr[-2000:], "attribute_override": attr_override}

    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "PR-INJECTOR",
            "GIT_AUTHOR_EMAIL": "pr-injector@example.invalid",
            "GIT_COMMITTER_NAME": "PR-INJECTOR",
            "GIT_COMMITTER_EMAIL": "pr-injector@example.invalid",
        }
    )

    if mode == "commit":
        proc = subprocess.run(
            [
                "git",
                "-c",
                "user.name=PR-INJECTOR",
                "-c",
                "user.email=pr-injector@example.invalid",
                "commit",
                "-m",
                f"RQ2 injected baseline {case_id}",
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        commit = run(["git", "rev-parse", "HEAD"], worktree, timeout=120)
        return {
            "mode": mode,
            "sealed": proc.returncode == 0,
            "returncode": proc.returncode,
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "stderr": proc.stderr[-2000:],
            "attribute_override": attr_override,
            "status_before_agent": git_status_short(worktree),
            "diff_size_before_agent": len(git_diff_text(worktree)),
            "head_parents_before_agent": git_head_parents(worktree),
        }

    if mode == "orphan":
        tree = run(["git", "write-tree"], worktree, timeout=120)
        if tree.returncode != 0:
            return {"mode": mode, "sealed": False, "error": tree.stderr[-2000:], "attribute_override": attr_override}
        commit_proc = subprocess.run(
            ["git", "commit-tree", tree.stdout.strip()],
            cwd=str(worktree),
            input=f"RQ2 injected baseline {case_id}\n",
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        if commit_proc.returncode != 0:
            return {"mode": mode, "sealed": False, "error": commit_proc.stderr[-2000:], "attribute_override": attr_override}
        commit = commit_proc.stdout.strip()
        reset = run(["git", "reset", "--hard", commit], worktree, timeout=120)
        return {
            "mode": mode,
            "sealed": reset.returncode == 0,
            "returncode": reset.returncode,
            "commit": commit,
            "stderr": reset.stderr[-2000:],
            "attribute_override": attr_override,
            "status_before_agent": git_status_short(worktree),
            "diff_size_before_agent": len(git_diff_text(worktree)),
            "head_parents_before_agent": git_head_parents(worktree),
        }

    raise ValueError(f"unknown B baseline mode: {mode}")


def limit_tests(tests: list[str], max_count: int) -> list[str]:
    if max_count <= 0:
        return tests
    return tests[:max_count]


def create_venv(worktree: Path) -> str:
    venv = worktree / ".venv"
    py = str(CODEX_BUNDLED_PYTHON) if CODEX_BUNDLED_PYTHON.exists() else None
    py = py or (sys.executable if sys.executable and Path(sys.executable).exists() else None)
    py = py or shutil.which("python3.12") or shutil.which("python3")
    if not py:
        raise RuntimeError("python3 not found")
    created = run([py, "-m", "venv", str(venv)], worktree, timeout=120)
    if created.returncode != 0:
        raise RuntimeError(f"venv creation failed: {created.stderr[-1000:]}")
    vpy = venv / "bin" / "python"
    pip_check = run([str(vpy), "-m", "pip", "--version"], worktree, timeout=60)
    if pip_check.returncode != 0:
        ensurepip = run([str(vpy), "-m", "ensurepip", "--upgrade"], worktree, timeout=240)
        if ensurepip.returncode != 0:
            raise RuntimeError(f"pip unavailable in venv: {ensurepip.stderr[-1000:]}")
    if os.environ.get("PRI_RQ2_BOOTSTRAP_PIP", "0") == "1":
        upgraded = run([str(vpy), "-m", "pip", "install", "-q", "--upgrade", "pip", "setuptools", "wheel"], worktree, timeout=240)
        if upgraded.returncode != 0:
            raise RuntimeError(f"pip bootstrap failed: {upgraded.stderr[-1000:]}")
    return str(vpy)


def install_project(worktree: Path, repo: str, python: str, tests: list[str]) -> None:
    from scripts.verify_swebench_pro import _install_project

    _install_project(str(worktree), repo, 300, python=python, test_files=tests)


def normalize_tests(worktree: Path, repo: str, tests: list[str]) -> list[str]:
    from scripts.verify_swebench_pro import _existing_nodeids

    return _existing_nodeids(worktree, tests, repo)


def run_tests(worktree: Path, repo: str, tests: list[str], timeout: int, python: str) -> dict:
    from scripts.verify_swebench_pro import run_repo_tests

    if not tests:
        return {"returncode": -1, "passed": 0, "failed": 0, "total": 0, "failed_tests": [], "output_tail": "no runnable tests"}
    return run_repo_tests(str(worktree), repo, tests, timeout=timeout, python=python)


def strict_eval_solved(
    fail_to_pass: dict,
    pass_to_pass: dict | None,
    require_pass_to_pass: bool,
) -> tuple[bool, bool]:
    """Return target_solved and strict_solved for one evaluated repository."""

    target_solved = (
        fail_to_pass.get("returncode") == 0
        and int(fail_to_pass.get("total") or 0) > 0
    )
    if require_pass_to_pass:
        p2p_solved = (
            pass_to_pass is not None
            and pass_to_pass.get("returncode") == 0
            and int(pass_to_pass.get("total") or 0) > 0
        )
    else:
        p2p_solved = pass_to_pass is None or pass_to_pass.get("returncode") == 0
    return target_solved, target_solved and p2p_solved


def git_changed_files(worktree: Path) -> list[str]:
    paths: set[str] = set()
    commands = [
        ["git", "diff", "--name-only", "--no-renames", "HEAD", "--"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    for command in commands:
        proc = run(command, worktree, timeout=120)
        if proc.returncode != 0:
            continue
        paths.update(line.strip() for line in proc.stdout.splitlines() if line.strip())
    return sorted(paths)


def is_forbidden_agent_edit(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return any(fnmatch(normalized, pattern) for pattern in FORBIDDEN_AGENT_EDIT_PATTERNS)


def forbidden_agent_edits(paths: list[str]) -> list[str]:
    return sorted({p for p in paths if is_forbidden_agent_edit(p)})


def restore_forbidden_agent_edits(worktree: Path, paths: list[str]) -> dict:
    """Remove forbidden edits after preserving the agent patch for auditing."""

    restored: list[str] = []
    removed_untracked: list[str] = []
    errors: list[dict[str, str | int]] = []
    worktree_root = worktree.resolve()

    for rel_path in sorted(set(paths)):
        target = (worktree / rel_path).resolve()
        try:
            target.relative_to(worktree_root)
        except ValueError:
            errors.append({"path": rel_path, "error": "path escapes worktree"})
            continue

        tracked = run(["git", "ls-files", "--error-unmatch", "--", rel_path], worktree, timeout=120)
        if tracked.returncode == 0:
            restore = run(
                ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", rel_path],
                worktree,
                timeout=120,
            )
            if restore.returncode == 0:
                restored.append(rel_path)
            else:
                errors.append(
                    {
                        "path": rel_path,
                        "returncode": restore.returncode,
                        "error": restore.stderr[-1000:],
                    }
                )
            continue

        run(["git", "reset", "--", rel_path], worktree, timeout=120)
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            removed_untracked.append(rel_path)
        except Exception as exc:
            errors.append({"path": rel_path, "error": str(exc)})

    remaining = forbidden_agent_edits(git_changed_files(worktree))
    return {
        "restored_tracked": restored,
        "removed_untracked": removed_untracked,
        "errors": errors,
        "remaining_forbidden_files": remaining,
        "clean": not remaining and not errors,
    }


def prompt_files(out_dir: Path, case: dict, group: str) -> tuple[Path, Path]:
    system = out_dir / "system.txt"
    task = out_dir / "task.txt"
    system.write_text(
        textwrap.dedent(
            """\
            You are an autonomous coding agent participating in a controlled software-repair evaluation.
            Fix the reported bug by editing the repository source code.
            You are strictly forbidden to modify tests, test fixtures, benchmark metadata, CI configuration, or evaluation files.
            Any change to a forbidden file invalidates the run even if tests pass.
            Keep the change minimal and consistent with the existing project style.
            Do not use network access unless the project tooling itself requires dependency setup.
            Do not run commands or tests that intentionally crash the interpreter, send SIGSEGV, invoke debug-crash/segfault paths, or open macOS crash dialogs; inspect those code paths statically instead.
            When finished, leave the repository with your proposed patch applied.
            """
        ),
        encoding="utf-8",
    )
    task.write_text(
        textwrap.dedent(
            f"""\
            RQ2 case: {case['case_id']}
            Repository: {case['repo']}

            Problem statement:

            {case['A_problem_statement']}
            """
        ),
        encoding="utf-8",
    )
    return system, task


def run_claude_once(repo: Path, system: Path, task: Path, out_json: Path, timeout_s: int,
                    tools: str, env: dict[str, str], attempt: int) -> dict:
    sys_text = system.read_text(encoding="utf-8", errors="ignore")
    task_text = task.read_text(encoding="utf-8", errors="ignore")
    cmd = [
        CLAUDE_BIN,
        "-p",
        "--append-system-prompt",
        sys_text,
        task_text,
        "--output-format",
        "json",
        "--allowedTools",
        tools,
        "bash",
        "--permission-mode",
        "acceptEdits",
    ]
    pre_status = run(["git", "status", "--porcelain=v1", "-uno"], repo, timeout=120)
    pre_diff = run(["git", "diff"], repo, timeout=120)
    started = time.time()
    timed_out = False
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        returncode = int(proc.returncode) if proc.returncode is not None else 0
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(proc.pid)
        returncode = 124
        stdout = ""
        stderr = f"[claude_timeout] timeout_s={timeout_s} cmd=claude -p ...\n"
    post_status = run(["git", "status", "--porcelain=v1", "-uno"], repo, timeout=120)
    post_diff = run(["git", "diff"], repo, timeout=120)
    payload = {
        "cmd": [
            CLAUDE_BIN,
            "-p",
            "--append-system-prompt",
            "<system omitted>",
            "<task omitted>",
            "--output-format",
            "json",
            "--allowedTools",
            tools,
            "bash",
            "--permission-mode",
            "acceptEdits",
        ],
        "cwd": str(repo),
        "returncode": returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timeout_s": timeout_s,
        "timed_out": timed_out,
        "attempt": attempt,
        "elapsed_s": round(time.time() - started, 3),
        "pre_git_status": pre_status.stdout,
        "pre_git_diff": pre_diff.stdout,
        "post_git_status": post_status.stdout,
        "post_git_diff": post_diff.stdout,
        "env": claude_env_audit(env),
    }
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def run_claude(repo: Path, out_dir: Path, case: dict, group: str, timeout_s: int, tools: str,
               aws_region: str, aws_profile: str, model: str, infra_retries: int) -> dict:
    system, task = prompt_files(out_dir, case, group)
    out_json = out_dir / "claude_raw.json"
    env = build_claude_env(aws_region, aws_profile, model)
    started = time.time()
    attempts = []
    payload: dict = {}
    for attempt in range(1, infra_retries + 2):
        payload = run_claude_once(repo, system, task, out_json, timeout_s, tools, env, attempt)
        attempts.append(
            {
                "attempt": attempt,
                "returncode": payload.get("returncode"),
                "timed_out": payload.get("timed_out"),
                "elapsed_s": payload.get("elapsed_s"),
                "transient_failure": is_transient_claude_failure(payload),
            }
        )
        if not is_transient_claude_failure(payload):
            break
        if attempt <= infra_retries:
            delay = min(60 * attempt, 180)
            print(
                f"[RQ2 infra] transient Claude/Bedrock failure on {case['case_id']} {group}; "
                f"retry {attempt}/{infra_retries} after {delay}s",
                flush=True,
            )
            time.sleep(delay)
    else:
        payload = payload or {}

    raw_returncode = payload.get("returncode") if isinstance(payload, dict) else None
    if is_transient_claude_failure(payload):
        raise InfrastructureError(
            f"Claude/Bedrock transient failure after {infra_retries + 1} attempts "
            f"for {case['case_id']} {group}; last_returncode={raw_returncode}"
        )
    return {
        "returncode": raw_returncode,
        "wrapper_returncode": raw_returncode,
        "raw_returncode": raw_returncode,
        "timed_out": payload.get("timed_out") or raw_returncode == 124,
        "elapsed_s": round(time.time() - started, 3),
        "runner": "direct_claude_cli",
        "claude_bin": CLAUDE_BIN,
        "claude_home": str(claude_home()),
        "infra_attempts": attempts,
        "raw_output_path": str(out_json),
        "raw": payload,
    }


def generic_runner_python() -> str:
    override = os.environ.get("RQ2_AGENT_RUNNER_PYTHON")
    candidates = [override, str(CODEX_BUNDLED_PYTHON), sys.executable, "/usr/bin/python3"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.exists() or shutil.which(candidate):
            return candidate
    return sys.executable


def default_runner_for_kind(kind: str) -> Path:
    if kind == "codex":
        return DEFAULT_CODEX_RUNNER
    if kind == "opencode":
        return DEFAULT_OPENCODE_RUNNER
    if kind == "openhands":
        return DEFAULT_OPENHANDS_RUNNER
    raise ValueError(f"no generic runner for agent kind: {kind}")


def run_generic_agent_once(
    repo: Path,
    system: Path,
    task: Path,
    out_json: Path,
    timeout_s: int,
    tools: str,
    runner_script: Path,
    model: str,
    aws_region: str,
    aws_profile: str,
    extra_args: list[str],
    attempt: int,
) -> dict:
    cmd = [
        generic_runner_python(),
        str(runner_script.resolve()),
        "--repo",
        str(repo.resolve()),
        "--system",
        str(system.resolve()),
        "--task",
        str(task.resolve()),
        "--out",
        str(out_json.resolve()),
        "--timeout-s",
        str(timeout_s),
        "--tools",
        tools,
    ]
    if model:
        cmd.extend(["--model", model])
    cmd.extend(extra_args)

    env = os.environ.copy()
    env["AWS_REGION"] = aws_region
    env["AWS_DEFAULT_REGION"] = aws_region
    env["AWS_PROFILE"] = aws_profile
    env["AWS_SDK_LOAD_CONFIG"] = "1"
    env["AWS_EC2_METADATA_DISABLED"] = "true"

    pre_status = run(["git", "status", "--porcelain=v1", "-uno"], repo, timeout=120)
    pre_diff = run(["git", "diff"], repo, timeout=120)
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    timed_out = False
    watchdog_fired = False

    def watchdog_kill() -> None:
        nonlocal watchdog_fired
        watchdog_fired = True
        terminate_process_group(proc.pid)

    watchdog = threading.Timer(timeout_s + 60, watchdog_kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s + 60)
        wrapper_returncode = int(proc.returncode) if proc.returncode is not None else 0
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(proc.pid)
        wrapper_returncode = 124
        stdout = ""
        stderr = f"[generic_agent_timeout] timeout_s={timeout_s} runner={runner_script}\n"
    finally:
        watchdog.cancel()
    if watchdog_fired and wrapper_returncode == 0:
        timed_out = True
        wrapper_returncode = 124
        stderr = (stderr or "") + f"\n[generic_agent_watchdog_timeout] timeout_s={timeout_s} runner={runner_script}\n"

    post_status = run(["git", "status", "--porcelain=v1", "-uno"], repo, timeout=120)
    post_diff = run(["git", "diff"], repo, timeout=120)

    raw_payload: dict = {}
    if out_json.exists():
        try:
            raw_payload = json.loads(out_json.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            raw_payload = {"parse_error": str(exc), "raw_path": str(out_json)}

    returncode = raw_payload.get("returncode", wrapper_returncode)
    if not isinstance(returncode, int):
        returncode = wrapper_returncode
    return {
        "cmd": cmd,
        "cwd": str(repo),
        "returncode": returncode,
        "wrapper_returncode": wrapper_returncode,
        "raw_returncode": raw_payload.get("returncode"),
        "timed_out": timed_out or returncode == 124,
        "elapsed_s": round(time.time() - started, 3),
        "attempt": attempt,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "pre_git_status": pre_status.stdout,
        "pre_git_diff": pre_diff.stdout,
        "post_git_status": post_status.stdout,
        "post_git_diff": post_diff.stdout,
        "raw_output_path": str(out_json),
        "runner": "generic_agent_runner",
        "runner_script": str(runner_script),
        "model": model,
        "raw": raw_payload,
    }


def run_generic_agent(
    repo: Path,
    out_dir: Path,
    case: dict,
    group: str,
    timeout_s: int,
    tools: str,
    runner_script: Path,
    model: str,
    aws_region: str,
    aws_profile: str,
    infra_retries: int,
    extra_args: list[str],
) -> dict:
    system, task = prompt_files(out_dir, case, group)
    out_json = out_dir / "agent_raw.json"
    started = time.time()
    attempts = []
    payload: dict = {}
    retry_reset_available = not git_status_short(repo).strip()
    timeout_retries_used = 0
    for attempt in range(1, infra_retries + 2):
        retry_reset = None
        if attempt > 1 and retry_reset_available:
            reset = run(["git", "reset", "--hard", "HEAD"], repo, timeout=300)
            clean = run(["git", "clean", "-fd"], repo, timeout=300)
            retry_reset = {
                "reset_returncode": reset.returncode,
                "clean_returncode": clean.returncode,
                "status_after": git_status_short(repo),
            }
        if out_json.exists():
            out_json.unlink()
        payload = run_generic_agent_once(
            repo,
            system,
            task,
            out_json,
            timeout_s,
            tools,
            runner_script,
            model,
            aws_region,
            aws_profile,
            extra_args,
            attempt,
        )
        attempts.append(
            {
                "attempt": attempt,
                "returncode": payload.get("returncode"),
                "wrapper_returncode": payload.get("wrapper_returncode"),
                "timed_out": payload.get("timed_out"),
                "elapsed_s": payload.get("elapsed_s"),
                "infra_failure": is_generic_infra_failure(payload),
                "error_summary": agent_error_text(payload),
                "retry_reset": retry_reset,
            }
        )
        if not payload.get("timed_out") and payload.get("returncode") == 0:
            break
        if is_generic_infra_failure(payload) and not is_retryable_generic_infra_failure(payload):
            break
        if payload.get("timed_out"):
            if timeout_retries_used >= 1 or attempt > infra_retries:
                break
            timeout_retries_used += 1
            time.sleep(min(30 * attempt, 90))
        elif is_retryable_generic_infra_failure(payload) and attempt <= infra_retries:
            time.sleep(min(30 * attempt, 90))
        elif attempt <= infra_retries:
            time.sleep(min(10 * attempt, 30))

    if is_generic_infra_failure(payload):
        raise InfrastructureError(
            f"Generic agent infrastructure failure for {case['case_id']} {group}; "
            f"runner={runner_script}; model={model}; last_error={agent_error_text(payload)}"
        )

    return {
        "returncode": payload.get("returncode"),
        "wrapper_returncode": payload.get("wrapper_returncode"),
        "raw_returncode": payload.get("raw_returncode"),
        "timed_out": payload.get("timed_out"),
        "elapsed_s": round(time.time() - started, 3),
        "runner": payload.get("runner"),
        "runner_script": str(runner_script),
        "model": model,
        "infra_attempts": attempts,
        "raw_output_path": str(out_json),
        "raw": payload,
    }


def run_selected_agent(repo: Path, out_dir: Path, case: dict, group: str, args: argparse.Namespace) -> dict:
    if args.agent_kind == "claude":
        return run_claude(
            repo,
            out_dir,
            case,
            group,
            args.agent_timeout_s,
            args.tools,
            args.aws_region,
            args.aws_profile,
            args.bedrock_model_id,
            args.agent_infra_retries,
        )

    runner_script = Path(args.agent_runner_script) if args.agent_runner_script else default_runner_for_kind(args.agent_kind)
    if not runner_script.is_absolute():
        runner_script = ROOT / runner_script
    if not runner_script.exists():
        raise FileNotFoundError(f"agent runner script not found: {runner_script}")
    model = args.agent_model or ""
    return run_generic_agent(
        repo,
        out_dir,
        case,
        group,
        args.agent_timeout_s,
        args.tools,
        runner_script,
        model,
        args.aws_region,
        args.aws_profile,
        args.agent_infra_retries,
        args.agent_extra_arg or [],
    )


def evaluate_case_group(case: dict, group: str, args: argparse.Namespace, repos_dirs: list[Path]) -> dict:
    case_id = case["case_id"]
    repo = case["repo"]
    out_dir = Path(args.output_dir) / case_id / group
    result_path = out_dir / "result.json"

    branch = f"rq2-{case_id.lower()}-{group.lower()}-{os.getpid()}"
    worktree = (ROOT / args.worktrees_dir).resolve() / f"{case_id}-{group}-{repo.replace('/', '__')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    ref = case["A_base_commit"] if group == "A" else case["B_healthy_head"]
    repo_path = repo_dir(repo, repos_dirs, ref)
    injected_apply = None
    b_baseline = None
    test_patch_apply = None
    pre_agent_git_status = ""
    pre_agent_git_diff_size = 0
    pre_agent_head_parents: list[str] = []
    agent = {}
    eval_result = {}
    runnable_tests: list[str] = []
    agent_diff = ""
    changed_files: list[str] = []
    forbidden_files: list[str] = []
    forbidden_restore = None

    if result_path.exists() and not args.force:
        existing = json.loads(result_path.read_text(encoding="utf-8"))
        if agent_completed_successfully(existing):
            return existing

    result = None
    try:
        create_worktree(repo_path, worktree, ref, branch)
        if group == "B":
            injected_apply = apply_patch_file(worktree, case["B_injected_diff"], "B_injected_diff")
            if not injected_apply["applied"]:
                raise RuntimeError(f"failed to apply B injected diff: {injected_apply}")
            b_baseline = seal_b_baseline(worktree, args.b_baseline_mode, case_id)
            if args.b_baseline_mode != "dirty" and not b_baseline.get("sealed"):
                raise RuntimeError(f"failed to seal B baseline: {b_baseline}")

        pre_agent_git_status = git_status_short(worktree)
        pre_agent_git_diff_size = len(git_diff_text(worktree))
        pre_agent_head_parents = git_head_parents(worktree)

        agent = run_selected_agent(worktree, out_dir, case, group, args)
        agent_diff = run(["git", "diff"], worktree, timeout=120).stdout
        changed_files = git_changed_files(worktree)
        forbidden_files = forbidden_agent_edits(changed_files)
        (out_dir / "agent.patch").write_text(agent_diff, encoding="utf-8")

        if args.forbid_forbidden_edits and forbidden_files:
            forbidden_restore = restore_forbidden_agent_edits(worktree, forbidden_files)
            eval_result = {
                "status": "agent_modified_forbidden_files",
                "solved": False,
                "strict_solved": False,
                "target_solved": False,
                "forbidden_files": forbidden_files,
                "changed_files": changed_files,
                "forbidden_patterns": FORBIDDEN_AGENT_EDIT_PATTERNS,
                "forbidden_restore": forbidden_restore,
            }
            result = {
                "case_id": case_id,
                "group": group,
                "repo": repo,
                "source_dataset": case["source_dataset"],
                "A_instance_id": case["A_instance_id"],
                "B_instance_id": case["B_instance_id"],
                "worktree": str(worktree),
                "setup": {
                    "ref": ref,
                    "injected_apply": injected_apply,
                    "b_baseline": b_baseline,
                    "test_patch_apply": None,
                    "pre_agent_git_status": pre_agent_git_status,
                    "pre_agent_git_diff_size": pre_agent_git_diff_size,
                    "pre_agent_head_parents": pre_agent_head_parents,
                },
                "agent": agent,
                "agent_patch_path": str(out_dir / "agent.patch"),
                "agent_patch_size": len(agent_diff),
                "agent_changed_files": changed_files,
                "agent_forbidden_files": forbidden_files,
                "forbidden_restore": forbidden_restore,
                "evaluation": eval_result,
            }
            return result

        if not agent_completed_successfully({"agent": agent}):
            eval_result = {
                "status": "agent_failed_or_timed_out",
                "solved": False,
                "strict_solved": False,
                "target_solved": False,
                "changed_files": changed_files,
                "agent_returncode": agent.get("returncode"),
                "agent_timed_out": agent.get("timed_out"),
                "agent_error_summary": agent_error_text(agent.get("raw") or {}),
            }
            result = {
                "case_id": case_id,
                "group": group,
                "repo": repo,
                "source_dataset": case["source_dataset"],
                "A_instance_id": case["A_instance_id"],
                "B_instance_id": case["B_instance_id"],
                "worktree": str(worktree),
                "setup": {
                    "ref": ref,
                    "injected_apply": injected_apply,
                    "b_baseline": b_baseline,
                    "test_patch_apply": None,
                    "pre_agent_git_status": pre_agent_git_status,
                    "pre_agent_git_diff_size": pre_agent_git_diff_size,
                    "pre_agent_head_parents": pre_agent_head_parents,
                },
                "agent": agent,
                "agent_patch_path": str(out_dir / "agent.patch"),
                "agent_patch_size": len(agent_diff),
                "agent_changed_files": changed_files,
                "agent_forbidden_files": forbidden_files,
                "evaluation": eval_result,
            }
            return result

        if args.agent_only:
            eval_result = {
                "status": "agent_completed_no_local_eval",
                "solved": False,
                "strict_solved": False,
                "target_solved": False,
                "changed_files": changed_files,
            }
            result = {
                "case_id": case_id,
                "group": group,
                "repo": repo,
                "source_dataset": case["source_dataset"],
                "A_instance_id": case["A_instance_id"],
                "B_instance_id": case["B_instance_id"],
                "worktree": str(worktree),
                "setup": {
                    "ref": ref,
                    "injected_apply": injected_apply,
                    "b_baseline": b_baseline,
                    "test_patch_apply": None,
                    "pre_agent_git_status": pre_agent_git_status,
                    "pre_agent_git_diff_size": pre_agent_git_diff_size,
                    "pre_agent_head_parents": pre_agent_head_parents,
                },
                "agent": agent,
                "agent_patch_path": str(out_dir / "agent.patch"),
                "agent_patch_size": len(agent_diff),
                "agent_changed_files": changed_files,
                "agent_forbidden_files": forbidden_files,
                "evaluation": eval_result,
            }
            return result

        if group == "A":
            test_patch_apply = apply_patch_text(worktree, case.get("A_test_patch", ""), "A_test_patch")
            if not test_patch_apply["applied"]:
                eval_result = {
                    "status": "test_patch_apply_failed",
                    "solved": False,
                    "test_patch_apply": test_patch_apply,
                }
            else:
                vpy = create_venv(worktree)
                runnable_tests = normalize_tests(worktree, repo, case.get("A_FAIL_TO_PASS", []))
                p2p_tests = limit_tests(
                    normalize_tests(worktree, repo, case.get("A_PASS_TO_PASS", [])),
                    args.max_pass_to_pass,
                )
                install_project(worktree, repo, vpy, runnable_tests + p2p_tests)
                fail_to_pass = run_tests(worktree, repo, runnable_tests, args.test_timeout_s, vpy)
                pass_to_pass = run_tests(worktree, repo, p2p_tests, args.test_timeout_s, vpy) if p2p_tests else None
                target_solved, strict_solved = strict_eval_solved(
                    fail_to_pass, pass_to_pass, args.require_pass_to_pass
                )
                if int(fail_to_pass.get("total") or 0) == 0:
                    evaluation_status = "harness_target_not_executed"
                elif (
                    args.require_pass_to_pass
                    and p2p_tests
                    and (pass_to_pass is None or int(pass_to_pass.get("total") or 0) == 0)
                ):
                    evaluation_status = "harness_pass_to_pass_not_executed"
                else:
                    evaluation_status = "completed"
                eval_result = {
                    "status": evaluation_status,
                    "solved": strict_solved,
                    "strict_solved": strict_solved,
                    "target_solved": target_solved,
                    "harness_issue": evaluation_status.startswith("harness_"),
                    "fail_to_pass": fail_to_pass,
                    "pass_to_pass": pass_to_pass,
                    "runnable_fail_to_pass": runnable_tests,
                    "runnable_pass_to_pass_count": len(p2p_tests),
                }
        else:
            vpy = create_venv(worktree)
            runnable_tests = normalize_tests(worktree, repo, case.get("B_FAIL_TO_PASS", []))
            b_p2p_source = case.get("B_PASS_TO_PASS_CLEAN") or case.get("B_PASS_TO_PASS", [])
            p2p_tests = limit_tests(
                normalize_tests(worktree, repo, b_p2p_source),
                args.max_pass_to_pass,
            )
            install_project(worktree, repo, vpy, runnable_tests + p2p_tests)
            fail_to_pass = run_tests(worktree, repo, runnable_tests, args.test_timeout_s, vpy)
            pass_to_pass = run_tests(worktree, repo, p2p_tests, args.test_timeout_s, vpy) if p2p_tests else None
            target_solved, strict_solved = strict_eval_solved(
                fail_to_pass, pass_to_pass, args.require_pass_to_pass
            )
            if int(fail_to_pass.get("total") or 0) == 0:
                evaluation_status = "harness_target_not_executed"
            elif (
                args.require_pass_to_pass
                and p2p_tests
                and (pass_to_pass is None or int(pass_to_pass.get("total") or 0) == 0)
            ):
                evaluation_status = "harness_pass_to_pass_not_executed"
            else:
                evaluation_status = "completed"
            eval_result = {
                "status": evaluation_status,
                "solved": strict_solved,
                "strict_solved": strict_solved,
                "target_solved": target_solved,
                "harness_issue": evaluation_status.startswith("harness_"),
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "runnable_fail_to_pass": runnable_tests,
                "runnable_pass_to_pass_count": len(p2p_tests),
            }

        result = {
            "case_id": case_id,
            "group": group,
            "repo": repo,
            "source_dataset": case["source_dataset"],
            "A_instance_id": case["A_instance_id"],
            "B_instance_id": case["B_instance_id"],
            "worktree": str(worktree),
            "setup": {
                "ref": ref,
                "injected_apply": injected_apply,
                "b_baseline": b_baseline,
                "test_patch_apply": test_patch_apply,
                "pre_agent_git_status": pre_agent_git_status,
                "pre_agent_git_diff_size": pre_agent_git_diff_size,
                "pre_agent_head_parents": pre_agent_head_parents,
            },
            "agent": agent,
            "agent_patch_path": str(out_dir / "agent.patch"),
            "agent_patch_size": len(agent_diff),
            "agent_changed_files": changed_files,
            "agent_forbidden_files": forbidden_files,
            "evaluation": eval_result,
        }
    except InfrastructureError as exc:
        result = {
            "case_id": case_id,
            "group": group,
            "repo": repo,
            "source_dataset": case.get("source_dataset"),
            "A_instance_id": case.get("A_instance_id"),
            "B_instance_id": case.get("B_instance_id"),
            "worktree": str(worktree),
            "setup": {
                "ref": ref,
                "injected_apply": injected_apply,
                "b_baseline": b_baseline,
                "test_patch_apply": test_patch_apply,
                "pre_agent_git_status": pre_agent_git_status,
                "pre_agent_git_diff_size": pre_agent_git_diff_size,
                "pre_agent_head_parents": pre_agent_head_parents,
            },
            "agent": agent,
            "agent_patch_path": str(out_dir / "agent.patch"),
            "agent_patch_size": len(agent_diff),
            "agent_changed_files": changed_files,
            "agent_forbidden_files": forbidden_files,
            "error": str(exc),
            "evaluation": {
                "status": "agent_infra_blocked",
                "solved": False,
                "strict_solved": False,
                "target_solved": False,
                "harness_issue": True,
            },
        }
    except Exception as exc:
        result = {
            "case_id": case_id,
            "group": group,
            "repo": repo,
            "source_dataset": case.get("source_dataset"),
            "error": str(exc),
            "evaluation": {"status": "error", "solved": False},
        }
    finally:
        if result is not None:
            write_json(result_path, result)
        remove_worktree(repo_path, worktree, branch, args.keep_worktrees)

    return result


def summarize(output_dir: Path) -> dict:
    results = []
    for p in output_dir.glob("RQ2_*/*/result.json"):
        results.append(json.loads(p.read_text(encoding="utf-8")))
    by_case: dict[str, dict] = {}
    for r in results:
        by_case.setdefault(r["case_id"], {})[r["group"]] = r
    pairs = []
    for case_id, groups in sorted(by_case.items()):
        a = groups.get("A")
        b = groups.get("B")
        if not a or not b:
            continue
        a_solved = bool(a.get("evaluation", {}).get("solved"))
        b_solved = bool(b.get("evaluation", {}).get("solved"))
        pairs.append({"case_id": case_id, "A_solved": a_solved, "B_solved": b_solved, "agreement": a_solved == b_solved})
    summary = {
        "results": len(results),
        "paired_cases": len(pairs),
        "A_runs": sum(1 for r in results if r.get("group") == "A"),
        "B_runs": sum(1 for r in results if r.get("group") == "B"),
        "A_solved": sum(1 for r in results if r.get("group") == "A" and r.get("evaluation", {}).get("solved")),
        "B_solved": sum(1 for r in results if r.get("group") == "B" and r.get("evaluation", {}).get("solved")),
        "agreement": sum(1 for p in pairs if p["agreement"]),
        "pairs": pairs,
    }
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairing", default=str(PAIRING))
    ap.add_argument("--output-dir", default="experiments/rq2_100/claude_bedrock_sonnet46_eval")
    ap.add_argument("--worktrees-dir", default=".pri-workspace/rq2-claude-eval-worktrees")
    ap.add_argument("--repos-dir", action="append", default=[])
    ap.add_argument("--case-id", action="append", default=[])
    ap.add_argument("--group", choices=["A", "B", "both"], default="both")
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--agent-timeout-s", type=int, default=1800)
    ap.add_argument("--agent-infra-retries", type=int, default=2)
    ap.add_argument("--test-timeout-s", type=int, default=300)
    ap.add_argument("--max-pass-to-pass", type=int, default=20)
    ap.add_argument("--b-baseline-mode", choices=["dirty", "commit", "orphan"], default="dirty")
    ap.add_argument("--tools", default="Bash,Edit,Read,Grep,Glob")
    ap.add_argument("--aws-region", default="us-west-2")
    ap.add_argument("--aws-profile", default="default")
    ap.add_argument("--bedrock-model-id", default=DEFAULT_MODEL)
    ap.add_argument("--agent-kind", choices=["claude", "codex", "opencode", "openhands"], default="claude")
    ap.add_argument("--agent-name", default=None)
    ap.add_argument("--agent-runner-script", default="")
    ap.add_argument("--agent-model", default="")
    ap.add_argument("--agent-extra-arg", action="append", default=[])
    ap.add_argument("--forbid-forbidden-edits", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--require-pass-to-pass", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--agent-only", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-worktrees", action="store_true")
    args = ap.parse_args()

    repos_dirs = [Path(p) for p in args.repos_dir] or [
        ROOT / ".pri-workspace" / "demo-repos",
        ROOT / ".pri-workspace" / "swebench-screen-repos",
    ]
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    Path(args.worktrees_dir).mkdir(parents=True, exist_ok=True)

    cases = read_jsonl(Path(args.pairing))
    if args.case_id:
        wanted = set(args.case_id)
        cases = [c for c in cases if c["case_id"] in wanted]
    if args.max_cases is not None:
        cases = cases[: args.max_cases]

    groups = ["A", "B"] if args.group == "both" else [args.group]
    manifest = {
        "model": args.agent_model or args.bedrock_model_id,
        "agent": args.agent_name or ("Claude Code" if args.agent_kind == "claude" else args.agent_kind),
        "agent_kind": args.agent_kind,
        "agent_runner_script": args.agent_runner_script or None,
        "agent_extra_arg": args.agent_extra_arg,
        "provider": "AWS Bedrock" if args.agent_kind in ("claude", "opencode", "openhands") else "Codex CLI",
        "aws_region": args.aws_region,
        "aws_profile": args.aws_profile,
        "runner_python": sys.executable,
        "claude_wrapper_python": claude_wrapper_python(),
        "agent_timeout_s": args.agent_timeout_s,
        "agent_infra_retries": args.agent_infra_retries,
        "test_timeout_s": args.test_timeout_s,
        "max_pass_to_pass": args.max_pass_to_pass,
        "b_baseline_mode": args.b_baseline_mode,
        "forbid_forbidden_edits": args.forbid_forbidden_edits,
        "require_pass_to_pass": args.require_pass_to_pass,
        "forbidden_agent_edit_patterns": FORBIDDEN_AGENT_EDIT_PATTERNS,
        "agent_only": args.agent_only,
        "case_count": len(cases),
        "groups": groups,
    }
    write_json(Path(args.output_dir) / "run_manifest.json", manifest)

    for case in cases:
        for group in groups:
            print(f"[RQ2] {case['case_id']} group={group} repo={case['repo']}", flush=True)
            result = evaluate_case_group(case, group, args, repos_dirs)
            solved = result.get("evaluation", {}).get("solved")
            status = result.get("evaluation", {}).get("status")
            print(f"      status={status} solved={solved}", flush=True)
            summarize(Path(args.output_dir))

    summary = summarize(Path(args.output_dir))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
