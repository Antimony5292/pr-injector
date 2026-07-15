#!/usr/bin/env python3
"""Rescue feature construction failures by letting Codex edit modern HEAD directly."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from construct_feainjector_modern_poc import DEFAULT_REPO_CACHE, ROOT, run, write_feature_patches_for_paths
from construct_feainjector_modern_semantic_model import ensure_worktree_with_optional_clone
from feainjector_fidelity import feature_fidelity, feature_fidelity_feedback, implementation_diff, is_implementation_path
from prinjector_v2_metrics import patch_profile, read_jsonl, resolve_text, write_jsonl


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("instance_id") or "")


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


def build_prompt(row: dict[str, Any], prior_failure: str, feedback: str) -> str:
    source_patch = implementation_diff(str(row.get("feature_patch") or ""))
    profile = patch_profile(source_patch)
    retry = f"\nPrevious direct-edit feedback:\n{feedback[:2400]}\n" if feedback else ""
    return f"""Construct one high-quality feature-missing benchmark state by editing this modern repository directly.

The historical diff below is the developer's FEATURE ADDITION. Inspect modern HEAD, locate the
same feature semantics after code evolution, and remove or disable those semantics. This is task
construction, not feature implementation.

Instance: {row_id(row)}
Repository: {row.get('repo')}
Problem statement:
{str(row.get('problem_statement') or '')[:5000]}

Feature tests that must pass on healthy modern HEAD and fail after your edit:
{json.dumps(row.get('FAIL_TO_PASS') or [], ensure_ascii=False)[:5000]}

Protected adjacent/P2P tests that must remain passing:
{json.dumps(row.get('PASS_TO_PASS') or [], ensure_ascii=False)[:5000]}

Historical implementation profile: files={profile.source_files}, hunks={profile.hunks},
line_changes={profile.line_changes}.
Previous semantic-construction failure:
{prior_failure[:2400]}
{retry}
Historical feature-addition implementation patch:
```diff
{source_patch[:24000]}
```

Requirements:
- inspect the complete modern implementation and follow renamed/moved symbols and call chains;
- edit implementation source only; never edit tests, docs, examples, generated files, dependency
  manifests, lockfiles, CI, fixtures, snapshots, or configuration;
- remove the same feature behavior while keeping the repository buildable and public APIs coherent;
- preserve unrelated behavior and every protected P2P constraint;
- retain a complexity surface comparable to the historical feature when that behavior still spans
  multiple methods/files; do not collapse a multi-step feature into a tiny local stub;
- do not add test-specific branches, broad exceptions, dead code, or hard-coded outputs;
- do not install dependencies, commit, push, or run broad test suites;
- leave only the intended feature-removal source edits in the working tree.
"""


def run_agent(worktree: Path, prompt: str, call_dir: Path, timeout: int) -> dict[str, Any]:
    call_dir.mkdir(parents=True, exist_ok=True)
    system = call_dir / "system.txt"
    task = call_dir / "task.txt"
    result = call_dir / "runner.json"
    system.write_text(
        "Act as a semantic benchmark-construction engineer. Edit the isolated repository directly "
        "and obey every scope, fidelity, and protected-behavior constraint.",
        encoding="utf-8",
    )
    task.write_text(prompt, encoding="utf-8")
    proc = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(
                ROOT
                / "construction_toolkit"
                / "integrations"
                / "agent_maestro"
                / "run_codex_headless.py"
            ),
            "--repo", str(worktree),
            "--system", str(system),
            "--task", str(task),
            "--out", str(result),
            "--timeout-s", str(timeout),
            "--profile", "agent-maestro",
            "--model", "gpt-5.3-codex",
            "--reasoning-effort", "high",
            "--runner-note",
            "Direct feature rescue: edit implementation only and leave the feature-missing diff in the worktree.",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout + 90,
    )
    payload: dict[str, Any] = {}
    if result.exists():
        try:
            payload = json.loads(result.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {
        "returncode": proc.returncode,
        "stderr": proc.stderr[-1600:],
        "runner": payload,
        "runner_result": str(result.relative_to(ROOT)),
    }


def process_case(
    row: dict[str, Any],
    prior_failure: str,
    output_dir: Path,
    repo_cache_roots: list[Path],
    max_attempts: int,
    timeout: int,
) -> dict[str, Any]:
    iid = row_id(row)
    base: dict[str, Any] = {
        "instance_id": iid,
        "repo": row.get("repo"),
        "status": "direct_feature_agent_failed",
        "strategy": "direct_codex_remove_modern_feature",
    }
    try:
        worktree = ensure_worktree_with_optional_clone(
            row,
            output_dir,
            repo_cache_roots,
            checkout_modern_head=True,
            clone_missing_repos=True,
        )
        feedback = ""
        attempts: list[dict[str, Any]] = []
        source_profile = patch_profile(implementation_diff(str(row.get("feature_patch") or "")))
        for attempt in range(1, max_attempts + 1):
            run(["git", "reset", "--hard", "HEAD"], cwd=worktree)
            run(["git", "clean", "-fd"], cwd=worktree, check=False)
            call_dir = output_dir / "agent_calls" / iid / f"attempt_{attempt:02d}"
            agent = run_agent(
                worktree,
                build_prompt(row, prior_failure, feedback),
                call_dir,
                timeout,
            )
            diff = run(["git", "diff", "--binary", "HEAD", "--"], cwd=worktree).stdout
            touched = diff_files(diff)
            attempt_row: dict[str, Any] = {
                "attempt": attempt,
                "agent": agent,
                "touched_files": touched,
                "diff_bytes": len(diff.encode()),
            }
            forbidden = [path for path in touched if not is_implementation_path(path)]
            if agent["returncode"] != 0 or not diff.strip():
                feedback = f"agent returned {agent['returncode']} and produced no usable source diff"
                attempt_row["rejected_reason"] = feedback
                attempts.append(attempt_row)
                continue
            if forbidden:
                feedback = f"non-implementation paths were edited: {forbidden}"
                attempt_row["rejected_reason"] = feedback
                attempts.append(attempt_row)
                continue

            restore = run(["git", "diff", "-R", "--", *touched], cwd=worktree).stdout
            fidelity = feature_fidelity(
                str(row.get("feature_patch") or ""),
                restore,
                source_target_tests=len(row.get("FAIL_TO_PASS") or []),
                modern_target_tests=len(row.get("FAIL_TO_PASS") or []),
                source_regression_tests=len(row.get("PASS_TO_PASS") or []),
                modern_regression_tests=len(row.get("PASS_TO_PASS") or []),
            )
            attempt_row["feature_fidelity_gate"] = fidelity
            if not fidelity.get("passed"):
                feedback = feature_fidelity_feedback(fidelity)
                attempt_row["rejected_reason"] = feedback
                attempts.append(attempt_row)
                continue

            patch_paths = write_feature_patches_for_paths(
                row, worktree, output_dir, [Path(path) for path in touched]
            )
            modern_profile = patch_profile(diff)
            attempt_row["accepted"] = True
            attempts.append(attempt_row)
            return {
                **base,
                "status": "constructed_feature_missing",
                "worktree": str(worktree.relative_to(ROOT)),
                "modern_head": run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip(),
                **patch_paths,
                "feature_tests": row.get("FAIL_TO_PASS") or [],
                "pass_to_pass": row.get("PASS_TO_PASS") or [],
                "verification_status": "not_run",
                "touched_files": touched,
                "feature_fidelity_gate": fidelity,
                "feature_complexity_profile": {
                    "A_line_changes": source_profile.line_changes,
                    "A_hunks": source_profile.hunks,
                    "A_source_files": source_profile.source_files,
                    "B_line_changes": modern_profile.line_changes,
                    "B_hunks": modern_profile.hunks,
                    "B_source_files": modern_profile.source_files,
                    "line_change_ratio": round(
                        modern_profile.line_changes / source_profile.line_changes, 4
                    ) if source_profile.line_changes else None,
                },
                "direct_agent_rescue": {
                    "generator": "codex_exec_agent_maestro",
                    "model": "gpt-5.3-codex",
                    "profile": "agent-maestro",
                    "bypassed_text_diff_generation": True,
                    "prior_failure": prior_failure,
                    "attempts": attempts,
                },
            }
        return {
            **base,
            "reason": feedback or "direct_feature_attempts_exhausted",
            "direct_agent_rescue": {"attempts": attempts},
        }
    except Exception as exc:
        return {**base, "reason": f"direct_feature_exception: {str(exc)[-1200:]}"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--failed-results", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repo-cache-root", action="append", default=[])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_cache_roots = [Path(path).resolve() for path in args.repo_cache_root] or [DEFAULT_REPO_CACHE.resolve()]
    manifest = {row_id(row): row for row in read_jsonl(args.manifest)}
    failures = [
        row for row in read_jsonl(args.failed_results)
        if row.get("status") in {
            "semantic_model_patch_apply_failed",
            "semantic_model_fidelity_gate_failed",
            "semantic_model_no_diff",
            "semantic_model_empty_patch",
        }
    ]
    jobs = [
        (manifest[row_id(failure)], str(failure.get("reason") or ""))
        for failure in failures
        if row_id(failure) in manifest
    ][: args.limit]

    results_by_index: dict[int, dict[str, Any]] = {}
    results_path = output_dir / "modern_semantic_construction_results.jsonl"
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(
                process_case,
                row,
                failure,
                output_dir,
                repo_cache_roots,
                args.max_attempts,
                args.timeout,
            ): index
            for index, (row, failure) in enumerate(jobs)
        }
        for future in as_completed(futures):
            results_by_index[futures[future]] = future.result()
            write_jsonl(results_path, [results_by_index[i] for i in sorted(results_by_index)])

    results = [results_by_index[i] for i in sorted(results_by_index)]
    constructed = [row for row in results if row.get("status") == "constructed_feature_missing"]
    write_jsonl(output_dir / "constructed_feature_tasks.jsonl", constructed)
    summary = {
        "jobs": len(jobs),
        "results": len(results),
        "constructed": len(constructed),
        "status_counts": dict(Counter(str(row.get("status")) for row in results)),
        "reason_counts": dict(Counter(str(row.get("reason")) for row in results if row.get("reason"))),
        "model": "gpt-5.3-codex",
        "direct_edit": True,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
