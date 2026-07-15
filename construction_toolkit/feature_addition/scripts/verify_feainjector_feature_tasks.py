"""Verify FeaBench-derived feature-missing benchmark candidates.

For each constructed feature-missing task, this verifier checks:

1. Healthy modern HEAD passes feature tests and adjacent/P2P tests.
2. Applying the feature-missing patch makes feature tests fail.
3. The feature-missing state does not break adjacent/P2P tests.
4. Applying the gold feature-restore patch makes feature tests and P2P pass.

The output is append-only JSONL and is safe to resume with --resume.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from construct_feainjector_modern_poc import DEFAULT_REPO_CACHE, ROOT, repo_cache_path
    from prinjector_v2_metrics import read_jsonl, write_jsonl
    from verify_swebench_pro import (
        _collectable_tests,
        _create_venv,
        _existing_nodeids,
        _install_project,
        _test_runner_available,
        run_repo_tests,
    )
except ImportError:
    from .construct_feainjector_modern_poc import DEFAULT_REPO_CACHE, ROOT, repo_cache_path
    from .prinjector_v2_metrics import read_jsonl, write_jsonl
    from .verify_swebench_pro import (
        _collectable_tests,
        _create_venv,
        _existing_nodeids,
        _install_project,
        _test_runner_available,
        run_repo_tests,
    )


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("instance_id") or "")


_REPO_LOCKS: dict[str, threading.Lock] = {}
_REPO_LOCKS_GUARD = threading.Lock()

FEATURE_TEST_NODEID_OVERRIDES: dict[str, list[str]] = {
    # Historical test_init_voxel_mask evolved into the modern add_roi behavior
    # test while preserving the same voxel-mask feature contract.
    "neurodatawithoutborders__pynwb-716": [
        "tests/unit/test_ophys.py::PlaneSegmentationConstructor::test_add_voxel_mask"
    ],
    "toblerity__shapely-361": [
        "shapely/tests/geometry/test_geometry_base.py::test_constructive_properties[oriented_envelope]",
        "shapely/tests/geometry/test_geometry_base.py::test_constructive_properties[minimum_rotated_rectangle]",
    ],
}

P2P_TEST_NODEID_OVERRIDES: dict[str, list[str]] = {
    "toblerity__shapely-361": [
        "shapely/tests/geometry/test_geometry_base.py::test_reverse",
        "shapely/tests/geometry/test_geometry_base.py::test_contains_properly",
        "shapely/tests/geometry/test_geometry_base.py::test_constructive_methods[normalize]",
    ],
}


def repo_lock(repo: str) -> threading.Lock:
    with _REPO_LOCKS_GUARD:
        lock = _REPO_LOCKS.get(repo)
        if lock is None:
            lock = threading.Lock()
            _REPO_LOCKS[repo] = lock
        return lock


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keep = dict(result)
    tail = str(keep.get("output_tail") or "")
    keep["output_tail"] = tail[-1200:]
    return keep


def run_git(args: list[str], cwd: Path, timeout: int = 300, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr[-1000:] or proc.stdout[-1000:])
    return proc


def ensure_repo_cache(
    repo: str,
    repo_cache_roots: list[Path],
    *,
    clone_missing_repos: bool,
    timeout: int,
) -> Path | None:
    source = repo_cache_path(repo, repo_cache_roots)
    if source is not None:
        return source
    if not clone_missing_repos or not repo_cache_roots:
        return None

    with repo_lock(repo):
        source = repo_cache_path(repo, repo_cache_roots)
        if source is not None:
            return source

        root = repo_cache_roots[0]
        root.mkdir(parents=True, exist_ok=True)
        repo_dir = root / repo.replace("/", "__")
        tmp_dir = root / f".tmp-{repo.replace('/', '__')}-{os.getpid()}-{threading.get_ident()}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

        proc = subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", str(tmp_dir)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        if proc.returncode != 0:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            message = (proc.stderr or proc.stdout or "")[-1200:]
            raise RuntimeError(f"repo_clone_failed: {message}")
        if repo_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        else:
            tmp_dir.rename(repo_dir)
        return repo_dir


def ensure_commit_available(repo_dir: Path, commit: str, timeout: int) -> None:
    if not commit:
        return
    exists = run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_dir, timeout=60)
    if exists.returncode == 0:
        return
    fetched = run_git(["fetch", "origin", commit], cwd=repo_dir, timeout=timeout)
    if fetched.returncode != 0:
        raise RuntimeError(f"fetch_modern_head_failed: {fetched.stderr[-1000:] or fetched.stdout[-1000:]}")


def apply_patch_file(worktree: Path, patch_path: Path, timeout: int) -> dict[str, Any]:
    if not patch_path.exists():
        return {"ok": False, "reason": "patch_missing", "patch": str(patch_path)}
    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    check = subprocess.run(
        ["git", "apply", "--check", "--recount"],
        cwd=str(worktree),
        input=patch_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    apply_args = ["git", "apply", "--recount"]
    if check.returncode != 0:
        check = subprocess.run(
            ["git", "apply", "--check", "--recount", "--ignore-space-change"],
            cwd=str(worktree),
            input=patch_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        apply_args = ["git", "apply", "--recount", "--ignore-space-change"]
    if check.returncode != 0:
        return {
            "ok": False,
            "reason": "patch_apply_check_failed",
            "patch": str(patch_path),
            "stderr": check.stderr[-1200:],
        }
    applied = subprocess.run(
        apply_args,
        cwd=str(worktree),
        input=patch_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if applied.returncode != 0:
        return {
            "ok": False,
            "reason": "patch_apply_failed",
            "patch": str(patch_path),
            "stderr": applied.stderr[-1200:],
        }
    return {"ok": True, "patch": str(patch_path)}


def prepare_worktree(
    row: dict[str, Any],
    output_dir: Path,
    repo_cache_roots: list[Path],
    *,
    keep_worktree: bool,
    clone_missing_repos: bool,
    clone_timeout: int,
) -> Path:
    iid = row_id(row)
    repo = str(row.get("repo") or "")
    worktree = (output_dir / "worktrees" / iid).resolve()
    if worktree.exists() and not keep_worktree:
        shutil.rmtree(worktree, ignore_errors=True)
    modern_head = str(row.get("modern_head") or "").strip()
    source = ensure_repo_cache(
        repo,
        repo_cache_roots,
        clone_missing_repos=clone_missing_repos,
        timeout=clone_timeout,
    )
    if source is None:
        roots = ", ".join(str(root) for root in repo_cache_roots)
        raise FileNotFoundError(f"missing cached repo for {repo} under: {roots}")
    source = source.resolve()
    ensure_commit_available(source, modern_head, timeout=clone_timeout)
    if not worktree.exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--shared", str(source), str(worktree)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600,
            check=True,
        )
    if modern_head:
        checkout = run_git(["checkout", "--detach", modern_head], cwd=worktree, timeout=300)
        if checkout.returncode != 0:
            # The local cache may not have this object yet.
            run_git(["fetch", "origin", modern_head], cwd=worktree, timeout=600)
            checkout = run_git(["checkout", "--detach", modern_head], cwd=worktree, timeout=300)
        if checkout.returncode != 0:
            raise RuntimeError(f"checkout_modern_head_failed: {checkout.stderr[-1000:]}")
    run_git(["reset", "--hard"], cwd=worktree, timeout=300, check=True)
    run_git(["clean", "-fdx"], cwd=worktree, timeout=300, check=False)
    return worktree


def result_passed(result: dict[str, Any]) -> bool:
    return int(result.get("returncode") or 0) == 0 and int(result.get("total") or 0) > 0


def feature_failed(result: dict[str, Any]) -> bool:
    if int(result.get("returncode") or 0) == 0:
        return False
    output = str(result.get("output_tail") or "").lower()
    no_tests_markers = [
        "no tests ran",
        "no tests collected",
        "not found:",
        "file or directory not found",
        "empty suite",
    ]
    return not any(marker in output for marker in no_tests_markers)


def select_tests(raw: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for test in raw:
        if test and test not in out:
            out.append(test)
        if limit > 0 and len(out) >= limit:
            break
    return out


def normalize_relative_nodeids(nodeids: list[str], anchors: list[str]) -> list[str]:
    """Bind ``::Class::test`` nodeids to their surrounding test module."""
    anchor_files = [
        nodeid.split("::", 1)[0]
        for nodeid in [*anchors, *nodeids]
        if "::" in nodeid and not nodeid.startswith("::")
    ]
    current_file = anchor_files[0] if anchor_files else ""
    normalized: list[str] = []
    for nodeid in nodeids:
        candidate = nodeid
        if nodeid.startswith("::") and current_file:
            candidate = f"{current_file}{nodeid}"
        elif "::" in nodeid and not nodeid.startswith("::"):
            current_file = nodeid.split("::", 1)[0]
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


def verify_one(row: dict[str, Any], args: argparse.Namespace, repo_cache_roots: list[Path]) -> dict[str, Any]:
    started = time.monotonic()
    iid = row_id(row)
    repo = str(row.get("repo") or "")
    output_dir = Path(args.output_dir).resolve()
    base_result: dict[str, Any] = {
        "instance_id": iid,
        "repo": repo,
        "status": "started",
        "strict_verified": False,
        "feature_missing_patch": row.get("feature_missing_patch"),
        "gold_feature_restore_patch": row.get("gold_feature_restore_patch"),
        "modern_head": row.get("modern_head"),
        "strategy": row.get("strategy"),
        "raw_feature_test_count": len(row.get("feature_tests") or []),
        "raw_pass_to_pass_count": len(row.get("pass_to_pass") or []),
    }
    try:
        worktree = prepare_worktree(
            row,
            output_dir,
            repo_cache_roots,
            keep_worktree=bool(args.keep_worktrees),
            clone_missing_repos=bool(args.clone_missing_repos),
            clone_timeout=args.clone_timeout,
        )
        base_result["worktree"] = str(worktree.relative_to(ROOT))
        feature_tests_raw = select_tests(list(row.get("feature_tests") or []), args.max_feature_tests)
        p2p_tests_raw = select_tests(list(row.get("pass_to_pass") or []), args.max_pass_to_pass)
        feature_override = FEATURE_TEST_NODEID_OVERRIDES.get(iid)
        if feature_override:
            base_result["historical_feature_tests"] = feature_tests_raw
            base_result["feature_test_override_reason"] = "audited_modern_semantic_nodeid_mapping"
            feature_tests_raw = list(feature_override)
        p2p_override = P2P_TEST_NODEID_OVERRIDES.get(iid)
        if p2p_override:
            base_result["historical_pass_to_pass"] = p2p_tests_raw
            base_result["pass_to_pass_override_reason"] = "audited_modern_adjacent_nodeid_mapping"
            p2p_tests_raw = list(p2p_override)
        if not feature_tests_raw:
            return {**base_result, "status": "skipped", "reason": "no_feature_tests"}
        if not p2p_tests_raw:
            return {**base_result, "status": "skipped", "reason": "no_pass_to_pass_tests"}
        if repo != "django/django":
            anchors = [*feature_tests_raw, *p2p_tests_raw]
            feature_tests_raw = normalize_relative_nodeids(feature_tests_raw, anchors)
            p2p_tests_raw = normalize_relative_nodeids(p2p_tests_raw, anchors)
        base_result["normalized_feature_tests_raw"] = feature_tests_raw
        base_result["normalized_pass_to_pass_raw"] = p2p_tests_raw

        if args.shared_venv:
            shared_root = ROOT / ".pri-workspace" / "shared_venvs"
            shared_root.mkdir(parents=True, exist_ok=True)
            os.environ["PRI_SHARED_REPO_VENV"] = "1"
            os.environ["PRI_SHARED_REPO_VENV_TAG"] = args.shared_venv_tag

        python = _create_venv(str(worktree), repo=repo)
        base_result["python"] = python
        if not python:
            return {**base_result, "status": "skipped", "reason": "python_unavailable"}

        install_tests = feature_tests_raw + p2p_tests_raw
        installed = _install_project(
            str(worktree),
            repo,
            timeout=args.install_timeout,
            python=python,
            test_files=install_tests,
        )
        base_result["install_project_ok"] = installed
        if not _test_runner_available(str(worktree), repo, python):
            return {**base_result, "status": "skipped", "reason": "test_runner_unavailable"}

        if repo == "django/django":
            # Multi-SWE style rows expose unittest display names such as
            # ``test_x (app.test_module.Case)`` and can also include free-form
            # test descriptions. Django's runner accepts neither form. Reuse
            # PR-INJECTOR's conservative source lookup to retain only exact,
            # runnable dotted labels on the modern checkout.
            feature_tests = _existing_nodeids(worktree, feature_tests_raw, repo=repo)
            p2p_tests = _existing_nodeids(worktree, p2p_tests_raw, repo=repo)
        else:
            feature_tests = _collectable_tests(
                str(worktree),
                repo,
                feature_tests_raw,
                python,
                timeout=args.collect_timeout,
            )
            p2p_tests = _collectable_tests(
                str(worktree),
                repo,
                p2p_tests_raw,
                python,
                timeout=args.collect_timeout,
                allow_static_fallback=False,
            )
            feature_tests = normalize_relative_nodeids(feature_tests, feature_tests_raw)
            p2p_tests = normalize_relative_nodeids(
                p2p_tests,
                [*feature_tests, *p2p_tests_raw],
            )
        base_result["feature_tests"] = feature_tests
        base_result["pass_to_pass"] = p2p_tests
        base_result["collectable_feature_test_count"] = len(feature_tests)
        base_result["collectable_pass_to_pass_count"] = len(p2p_tests)
        if not feature_tests:
            return {**base_result, "status": "skipped", "reason": "no_collectable_feature_tests"}
        if not p2p_tests:
            return {**base_result, "status": "skipped", "reason": "no_collectable_pass_to_pass_tests"}

        healthy_feature = run_repo_tests(str(worktree), repo, feature_tests, timeout=args.timeout, python=python)
        healthy_p2p = run_repo_tests(str(worktree), repo, p2p_tests, timeout=args.timeout, python=python)
        base_result["healthy_feature"] = compact_result(healthy_feature)
        base_result["healthy_p2p"] = compact_result(healthy_p2p)
        healthy_feature_pass = result_passed(healthy_feature)
        healthy_p2p_pass = result_passed(healthy_p2p)
        if not healthy_feature_pass:
            return {**base_result, "status": "completed", "reason": "healthy_feature_not_pass"}
        if not healthy_p2p_pass:
            return {**base_result, "status": "completed", "reason": "healthy_p2p_not_pass"}

        missing_patch = (ROOT / str(row.get("feature_missing_patch") or "")).resolve()
        missing_apply = apply_patch_file(worktree, missing_patch, timeout=args.patch_timeout)
        base_result["feature_missing_apply"] = missing_apply
        if not missing_apply.get("ok"):
            return {**base_result, "status": "completed", "reason": str(missing_apply.get("reason"))}

        missing_feature = run_repo_tests(str(worktree), repo, feature_tests, timeout=args.timeout, python=python)
        missing_p2p = run_repo_tests(str(worktree), repo, p2p_tests, timeout=args.timeout, python=python)
        base_result["missing_feature"] = compact_result(missing_feature)
        base_result["missing_p2p"] = compact_result(missing_p2p)
        feature_p2f = feature_failed(missing_feature)
        missing_p2p_pass = result_passed(missing_p2p)
        if not feature_p2f:
            return {**base_result, "status": "completed", "reason": "feature_p2f_miss"}
        if not missing_p2p_pass:
            return {**base_result, "status": "completed", "reason": "feature_missing_p2p_regression"}

        restore_patch = (ROOT / str(row.get("gold_feature_restore_patch") or "")).resolve()
        restore_apply = apply_patch_file(worktree, restore_patch, timeout=args.patch_timeout)
        base_result["gold_feature_restore_apply"] = restore_apply
        if not restore_apply.get("ok"):
            return {**base_result, "status": "completed", "reason": f"gold_restore_{restore_apply.get('reason')}"}

        restored_feature = run_repo_tests(str(worktree), repo, feature_tests, timeout=args.timeout, python=python)
        restored_p2p = run_repo_tests(str(worktree), repo, p2p_tests, timeout=args.timeout, python=python)
        base_result["restored_feature"] = compact_result(restored_feature)
        base_result["restored_p2p"] = compact_result(restored_p2p)
        restored_feature_pass = result_passed(restored_feature)
        restored_p2p_pass = result_passed(restored_p2p)
        strict_verified = (
            healthy_feature_pass
            and healthy_p2p_pass
            and feature_p2f
            and missing_p2p_pass
            and restored_feature_pass
            and restored_p2p_pass
        )
        return {
            **base_result,
            "status": "completed",
            "strict_verified": strict_verified,
            "healthy_feature_pass": healthy_feature_pass,
            "healthy_p2p_pass": healthy_p2p_pass,
            "feature_p2f": feature_p2f,
            "missing_p2p_pass": missing_p2p_pass,
            "gold_feature_pass": restored_feature_pass,
            "gold_p2p_pass": restored_p2p_pass,
            "reason": None if strict_verified else "strict_gate_failed",
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired:
        return {**base_result, "status": "skipped", "reason": "timeout", "duration_seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        message = str(exc)[-1200:]
        reason = "verifier_exception"
        if message.startswith("repo_clone_failed:"):
            reason = "repo_clone_failed"
        elif message.startswith("missing cached repo"):
            reason = "repo_cache_missing"
        elif message.startswith("fetch_modern_head_failed:"):
            reason = "fetch_modern_head_failed"
        return {
            **base_result,
            "status": "skipped",
            "reason": reason,
            "exception": message,
            "duration_seconds": round(time.monotonic() - started, 3),
        }


def append_jsonl(path: Path, row: dict[str, Any], lock: threading.Lock) -> None:
    text = json.dumps(row, ensure_ascii=False) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(text)
            f.flush()


def load_done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    done: set[str] = set()
    for row in read_jsonl(path):
        iid = row_id(row)
        if iid:
            done.add(iid)
    return done


def write_summary(output_dir: Path, results_path: Path) -> None:
    rows = read_jsonl(results_path) if results_path.exists() else []
    summary = {
        "rows": len(rows),
        "status_counts": dict(Counter(str(row.get("status")) for row in rows)),
        "reason_counts": dict(Counter(str(row.get("reason")) for row in rows if row.get("reason"))),
        "strict_verified": sum(1 for row in rows if row.get("strict_verified") is True),
        "repo_counts": dict(Counter(str(row.get("repo")) for row in rows)),
    }
    (output_dir / "feature_verification_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo-cache-root", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--filter", action="append", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--install-timeout", type=int, default=600)
    parser.add_argument("--collect-timeout", type=int, default=90)
    parser.add_argument("--patch-timeout", type=int, default=120)
    parser.add_argument("--clone-timeout", type=int, default=900)
    parser.add_argument("--max-feature-tests", type=int, default=8)
    parser.add_argument("--max-pass-to-pass", type=int, default=16)
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument("--clone-missing-repos", action="store_true")
    parser.add_argument("--shared-venv", action="store_true")
    parser.add_argument("--shared-venv-tag", default="feainjector-verify-20260708")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_cache_roots = [Path(path).resolve() for path in args.repo_cache_root] or [DEFAULT_REPO_CACHE.resolve()]
    rows = read_jsonl(Path(args.manifest))
    if args.filter:
        wanted = set(args.filter)
        rows = [row for row in rows if row_id(row) in wanted]
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    results_path = output_dir / "feature_verification_results.jsonl"
    if not args.resume and results_path.exists():
        results_path.unlink()
    done = load_done_ids(results_path) if args.resume else set()
    pending = [row for row in rows if row_id(row) not in done]

    config = {
        "manifest": str(Path(args.manifest).resolve()),
        "output_dir": str(output_dir),
        "repo_cache_roots": [str(path) for path in repo_cache_roots],
        "rows": len(rows),
        "pending": len(pending),
        "resume": args.resume,
        "workers": args.workers,
        "timeout": args.timeout,
        "install_timeout": args.install_timeout,
        "collect_timeout": args.collect_timeout,
        "clone_timeout": args.clone_timeout,
        "max_feature_tests": args.max_feature_tests,
        "max_pass_to_pass": args.max_pass_to_pass,
        "clone_missing_repos": args.clone_missing_repos,
        "shared_venv": args.shared_venv,
        "shared_venv_tag": args.shared_venv_tag,
    }
    (output_dir / "feature_verification_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lock = threading.Lock()
    print(json.dumps(config, ensure_ascii=False, indent=2), flush=True)
    if args.workers <= 1:
        for row in pending:
            result = verify_one(row, args, repo_cache_roots)
            append_jsonl(results_path, result, lock)
            write_summary(output_dir, results_path)
            print(json.dumps({"instance_id": row_id(row), "status": result.get("status"), "reason": result.get("reason"), "strict_verified": result.get("strict_verified")}, ensure_ascii=False), flush=True)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_id = {executor.submit(verify_one, row, args, repo_cache_roots): row_id(row) for row in pending}
            for future in concurrent.futures.as_completed(future_to_id):
                result = future.result()
                append_jsonl(results_path, result, lock)
                write_summary(output_dir, results_path)
                print(json.dumps({"instance_id": result.get("instance_id"), "status": result.get("status"), "reason": result.get("reason"), "strict_verified": result.get("strict_verified")}, ensure_ascii=False), flush=True)
    write_summary(output_dir, results_path)


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
