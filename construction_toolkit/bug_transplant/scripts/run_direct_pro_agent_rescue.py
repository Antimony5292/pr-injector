#!/usr/bin/env python3
"""Direct Codex-agent Pro rescue, independent of PR-INJECTOR patch generation.

The agent edits an isolated modern-HEAD worktree directly. This runner only
collects the resulting source diff and applies the shared fidelity gate; the
normal strict verifier remains authoritative for P2F, P2P, and gold restore.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

from inject_swebench_pro import (
    _codex_agent_forbidden_path,
    cleanup_worktree_branch,
    coerce_list,
    git,
    git_text,
    is_usable_git_repo,
    resolve_healthy_revision,
)
from prinjector_v2_metrics import (
    FidelityGateConfig,
    evaluate_patch_pair_fidelity,
    patch_profile,
    read_jsonl,
    write_jsonl,
)
from verify_swebench_pro import (
    _collectable_tests,
    _create_venv,
    _install_project,
    _test_runner_available,
    run_repo_tests,
)


ROOT = Path(__file__).resolve().parents[3]
AGENT_RUNNER = ROOT / "construction_toolkit" / "integrations" / "agent_maestro" / "run_codex_headless.py"
REPO_LOCKS: dict[str, Lock] = {}


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or "")


def workspace_path(path: Path) -> str:
    return str(path.resolve().relative_to(ROOT))


def safe_slug(value: str) -> str:
    digest = hashlib.sha1(value.encode()).hexdigest()[:10]
    return "".join(ch if ch.isalnum() else "-" for ch in value)[:80] + "-" + digest


@contextmanager
def repo_process_lock(repo: str):
    """Serialize direct-rescue worktree lifecycle across runner processes."""
    lock_dir = ROOT / ".pri-workspace" / "locks" / "direct-pro-rescue"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{safe_slug(repo)}.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def diff_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            path = parts[3].removeprefix("b/")
            if path not in files:
                files.append(path)
    return files


def build_prompt(row: dict[str, Any], feedback: str) -> str:
    patch = str(row.get("patch") or "")
    profile = patch_profile(patch)
    target = coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    protected = coerce_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))[:40]
    retry = f"\nPrevious attempt feedback:\n{feedback[:2400]}\n" if feedback else ""
    return f"""You are directly constructing one high-quality Pro bug-transplant benchmark case.

The historical patch below is the official HEALTHY FIX. Inspect the complete modern repository,
understand the fixed behavior and current call chain, then edit the modern implementation to
reintroduce the same semantic defect. This is benchmark construction, not bug fixing.

Instance: {row_id(row)}
Repository: {row.get('repo')}
Problem statement:
{str(row.get('problem_statement') or '')[:5000]}

Target tests that must pass on clean modern HEAD and fail after your edit:
{json.dumps(target, ensure_ascii=False)}

Protected adjacent/P2P behavior that must remain passing:
{json.dumps(protected, ensure_ascii=False)}

Historical implementation-patch profile: files={profile.source_files}, hunks={profile.hunks},
line_changes={profile.line_changes}. Preserve a comparable semantic and complexity surface.

Historical official fix patch:
```diff
{patch[:24000]}
```
{retry}
Requirements:
- inspect modern source and follow moved/renamed abstractions before editing;
- edit implementation source only; never edit tests, fixtures, snapshots, docs, CI, dependency
  manifests, generated files, or lockfiles;
- recreate the historical behavioral defect, not a syntax/import/build failure;
- do not add test-specific branches, hard-coded target values, broad exceptions, or dead code;
- preserve protected adjacent behavior and modern public APIs;
- do not install dependencies, commit, push, or run a broad test suite;
- leave only the intended source edits in the working tree. The Git diff is the only artifact used.
"""


def run_agent(worktree: Path, prompt: str, call_dir: Path, timeout: int) -> dict[str, Any]:
    call_dir.mkdir(parents=True, exist_ok=True)
    system = call_dir / "system.txt"
    task = call_dir / "task.txt"
    result = call_dir / "runner.json"
    system.write_text(
        "Act as a semantic benchmark-construction engineer. Edit the isolated repository directly "
        "and obey every scope and quality constraint. Do not wait for user input.",
        encoding="utf-8",
    )
    task.write_text(prompt, encoding="utf-8")
    cmd = [
        str(ROOT / ".venv" / "bin" / "python"),
        str(AGENT_RUNNER),
        "--repo", str(worktree),
        "--system", str(system),
        "--task", str(task),
        "--out", str(result),
        "--timeout-s", str(timeout),
        "--profile", "agent-maestro",
        "--model", "gpt-5.3-codex",
        "--reasoning-effort", "high",
        "--runner-note",
        "Direct Pro rescue: inspect and edit implementation source only; leave the requested regression diff in the worktree.",
    ]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout + 90)
    payload: dict[str, Any] = {}
    if result.exists():
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    return {
        "returncode": proc.returncode,
        "stderr": proc.stderr[-2000:],
        "runner": payload,
        "runner_result": workspace_path(result),
    }


def process_case(
    row: dict[str, Any],
    output_dir: Path,
    repos_dir: Path,
    worktrees_dir: Path,
    max_attempts: int,
    timeout: int,
    gate_config: FidelityGateConfig,
    healthy_preflight: bool,
) -> dict[str, Any]:
    iid = row_id(row)
    repo = str(row.get("repo") or "")
    result: dict[str, Any] = {
        "instance_id": iid,
        "source_instance_id": iid,
        "repo": repo,
        "source_dataset": row.get("source_dataset") or "ScaleAI/SWE-bench_Pro",
        "base_commit": row.get("base_commit"),
        "injection_level": "Direct_Codex_Agent_Manual_Rescue",
        "success": False,
        "failure_reason": None,
    }
    repo_dir = repos_dir / repo.replace("/", "__")
    if not is_usable_git_repo(repo_dir):
        result["failure_reason"] = "direct_agent_repo_cache_unavailable"
        return result

    with REPO_LOCKS.setdefault(repo, Lock()), repo_process_lock(repo):
        slug = safe_slug(iid)
        worktree = worktrees_dir / slug
        run_slug = hashlib.sha1(str(output_dir).encode()).hexdigest()[:10]
        branch = f"direct-pro-rescue-{run_slug}-{slug[-20:]}"
        try:
            healthy_sha, healthy_ref, healthy_source = resolve_healthy_revision(
                repo_dir, row, str(row.get("base_commit") or "")
            )
            cleanup_worktree_branch(repo_dir, branch, worktree)
            add = git("worktree", "add", "-b", branch, str(worktree), healthy_sha, cwd=str(repo_dir))
            if add.returncode != 0:
                result["failure_reason"] = "direct_agent_worktree_create_failed"
                result["worktree_error"] = add.stderr.decode(errors="replace")[-1000:]
                return result

            target = coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
            protected = coerce_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
            if healthy_preflight:
                os.environ.setdefault("PRI_SHARED_REPO_VENV", "1")
                os.environ.setdefault("PRI_SHARED_REPO_VENV_TAG", "direct-pro-preflight-20260714")
                python = _create_venv(str(worktree), repo=repo)
                if not python:
                    result["failure_reason"] = "direct_agent_preflight_python_unavailable"
                    return result
                installed = _install_project(
                    str(worktree),
                    repo,
                    timeout=min(max(timeout, 300), 900),
                    python=python,
                    test_files=target,
                )
                if not installed or not _test_runner_available(str(worktree), repo, python):
                    result["failure_reason"] = "direct_agent_preflight_environment_unavailable"
                    return result
                collectable = _collectable_tests(
                    str(worktree),
                    repo,
                    target,
                    python,
                    timeout=min(timeout, 180),
                    allow_static_fallback=False,
                )
                if not collectable:
                    result["failure_reason"] = "direct_agent_preflight_target_not_collectable"
                    return result
                collectable = collectable[:8]
                healthy = run_repo_tests(
                    str(worktree), repo, collectable, timeout=min(timeout, 300), python=python
                )
                result["direct_agent_healthy_preflight"] = {
                    "target_tests": collectable,
                    "returncode": healthy.get("returncode"),
                    "passed": healthy.get("passed"),
                    "failed": healthy.get("failed"),
                    "total": healthy.get("total"),
                    "output_tail": str(healthy.get("output_tail") or "")[-1200:],
                }
                if int(healthy.get("returncode") or 0) != 0 or int(healthy.get("total") or 0) <= 0:
                    result["failure_reason"] = "direct_agent_preflight_healthy_target_failed"
                    return result

            feedback = ""
            attempts: list[dict[str, Any]] = []
            for attempt in range(1, max_attempts + 1):
                git("reset", "--hard", "HEAD", cwd=str(worktree))
                git("clean", "-fd", cwd=str(worktree))
                call_dir = output_dir / "agent_calls" / slug / f"attempt_{attempt:02d}"
                agent = run_agent(worktree, build_prompt(row, feedback), call_dir, timeout)
                if not worktree.is_dir():
                    feedback = "isolated worktree disappeared during the agent call; recreate and retry"
                    attempts.append(
                        {
                            "attempt": attempt,
                            "agent": agent,
                            "changed_files": [],
                            "diff_bytes": 0,
                            "rejected_reason": feedback,
                            "infrastructure_retry": True,
                        }
                    )
                    cleanup_worktree_branch(repo_dir, branch, worktree)
                    add = git("worktree", "add", "-b", branch, str(worktree), healthy_sha, cwd=str(repo_dir))
                    if add.returncode != 0:
                        result["failure_reason"] = "direct_agent_worktree_recreate_failed"
                        result["worktree_error"] = add.stderr.decode(errors="replace")[-1000:]
                        result["direct_agent_rescue"] = {"attempts": attempts}
                        return result
                    continue
                diff = git_text("diff", "--binary", "HEAD", "--", cwd=str(worktree))
                changed = diff_files(diff)
                forbidden = [path for path in changed if _codex_agent_forbidden_path(path)]
                attempt_record: dict[str, Any] = {
                    "attempt": attempt,
                    "agent": agent,
                    "changed_files": changed,
                    "diff_bytes": len(diff.encode()),
                }
                if agent["returncode"] != 0 or not diff.strip():
                    feedback = f"agent produced no usable diff; returncode={agent['returncode']} stderr={agent['stderr'][-800:]}"
                    attempt_record["rejected_reason"] = feedback
                    attempts.append(attempt_record)
                    continue
                if forbidden:
                    feedback = f"forbidden non-implementation paths were edited: {forbidden}"
                    attempt_record["rejected_reason"] = feedback
                    attempts.append(attempt_record)
                    continue

                gate = evaluate_patch_pair_fidelity(
                    a_patch=str(row.get("patch") or ""),
                    b_patch=diff,
                    a_fail_to_pass=target,
                    b_fail_to_pass=target,
                    a_pass_to_pass=protected,
                    b_pass_to_pass=protected,
                    injection_level="Direct_Codex_Agent_Manual_Rescue",
                    config=gate_config,
                )
                gate["stage"] = "direct_pro_agent_generation"
                attempt_record["v2_fidelity_gate"] = gate
                if not gate.get("pass_gate"):
                    feedback = (
                        "generated regression failed fidelity gate: "
                        f"score={gate.get('score')} reasons={gate.get('reasons')} ratios={gate.get('ratios')}"
                    )
                    attempt_record["rejected_reason"] = feedback
                    attempts.append(attempt_record)
                    continue

                patch_dir = output_dir / "injected_diffs"
                patch_dir.mkdir(parents=True, exist_ok=True)
                patch_path = patch_dir / f"{slug}.diff"
                patch_path.write_text(diff, encoding="utf-8")
                attempt_record["accepted"] = True
                attempts.append(attempt_record)
                result.update(
                    {
                        "success": True,
                        "healthy_head": healthy_sha,
                        "healthy_head_ref": healthy_ref,
                        "healthy_head_ref_source": healthy_source,
                        "injected_diff": workspace_path(patch_path),
                        "fail_to_pass": target,
                        "pass_to_pass": protected,
                        "B_PASS_TO_PASS_CLEAN": protected,
                        "v2_fidelity_gate": gate,
                        "v2_fidelity_gate_pass": True,
                        "direct_agent_rescue": {
                            "generator": "codex_exec_agent_maestro",
                            "model": "gpt-5.3-codex",
                            "profile": "agent-maestro",
                            "bypassed_prinjector_generation": True,
                            "attempts": attempts,
                        },
                    }
                )
                return result

            result["failure_reason"] = feedback or "direct_agent_attempts_exhausted"
            result["direct_agent_rescue"] = {
                "generator": "codex_exec_agent_maestro",
                "model": "gpt-5.3-codex",
                "profile": "agent-maestro",
                "bypassed_prinjector_generation": True,
                "attempts": attempts,
            }
            return result
        except Exception as exc:
            result["failure_reason"] = f"direct_agent_exception: {str(exc)[:1000]}"
            result["exception_traceback"] = traceback.format_exc()[-4000:]
            return result
        finally:
            cleanup_worktree_branch(repo_dir, branch, worktree)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--selected-file", required=True, type=Path)
    parser.add_argument("--exclude-results", action="append", default=[], type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repos-dir", default=ROOT / ".pri-workspace" / "repos", type=Path)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--v2-min-score", type=float, default=0.65)
    parser.add_argument("--skip-healthy-preflight", action="store_true")
    args = parser.parse_args()

    # Git interprets relative worktree paths against the repository cache cwd,
    # while the agent runner interprets them against ROOT. Resolve every shared
    # path once so both processes address the same isolated checkout.
    args.candidate_file = args.candidate_file.resolve()
    args.selected_file = args.selected_file.resolve()
    args.exclude_results = [path.resolve() for path in args.exclude_results]
    args.output_dir = args.output_dir.resolve()
    args.repos_dir = args.repos_dir.resolve()

    selected_ids = {row_id(row) for row in read_jsonl(args.selected_file)}
    excluded_ids: set[str] = set()
    for path in args.exclude_results:
        if not path.exists():
            continue
        for row in read_jsonl(path):
            iid = row_id(row)
            failure = str(row.get("failure_reason") or "")
            retryable_infrastructure = (
                failure.startswith("direct_agent_exception:")
                or failure.startswith("direct_agent_worktree_")
            )
            if iid and not retryable_infrastructure:
                excluded_ids.add(iid)
    eligible_candidates = [
        row for row in read_jsonl(args.candidate_file)
        if row_id(row) and row_id(row) not in selected_ids and row_id(row) not in excluded_ids
    ]
    candidates = eligible_candidates[args.offset : args.offset + args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "direct_generation_summary.json"
    completion_path = args.output_dir / ".run_complete"
    summary_path.unlink(missing_ok=True)
    completion_path.unlink(missing_ok=True)
    worktrees_dir = args.output_dir / "worktrees"
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    gate_config = FidelityGateConfig(min_score=args.v2_min_score)
    results_by_index: dict[int, dict[str, Any]] = {}
    output = args.output_dir / "verified_injection_results.jsonl"

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_case,
                row,
                args.output_dir,
                args.repos_dir,
                worktrees_dir,
                args.max_attempts,
                args.timeout,
                gate_config,
                not args.skip_healthy_preflight,
            ): index
            for index, row in enumerate(candidates)
        }
        for future in as_completed(futures):
            index = futures[future]
            results_by_index[index] = future.result()
            write_jsonl(output, [results_by_index[idx] for idx in sorted(results_by_index)])

    results = [results_by_index[idx] for idx in sorted(results_by_index)]
    summary = {
        "candidates": len(candidates),
        "eligible_candidates": len(eligible_candidates),
        "offset": args.offset,
        "excluded_previous_results": len(excluded_ids),
        "results": len(results),
        "successful_generation": sum(row.get("success") is True for row in results),
        "failed_generation": sum(row.get("success") is not True for row in results),
        "by_repo": dict(Counter(str(row.get("repo")) for row in results)),
        "failure_reasons": dict(Counter(str(row.get("failure_reason")) for row in results if not row.get("success"))),
        "model": "gpt-5.3-codex",
        "profile": "agent-maestro",
        "bypassed_prinjector_generation": True,
        "output": workspace_path(output),
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    completion_path.write_text("complete\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
