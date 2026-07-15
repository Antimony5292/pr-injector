#!/usr/bin/env python3
"""Strictly reverify Pro injections rejected by corrected harness logic."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ModuleNotFoundError:
    from .prinjector_v2_metrics import read_jsonl, write_jsonl


ROOT = Path(__file__).resolve().parents[3]


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or "")


def strict_pass(row: dict[str, Any]) -> bool:
    verification = row.get("verification") or {}
    return bool(
        verification.get("pass_to_fail")
        and verification.get("golden_repair_pass") is True
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and verification.get("p2p_repaired_pass") is True
    )


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                import ast

                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    return []


def harness_false_reject_reason(
    verification_row: dict[str, Any],
    injection: dict[str, Any],
    candidate: dict[str, Any],
) -> str | None:
    verification = verification_row.get("verification") or {}
    if verification.get("reason") == "too_many_target_tests":
        return "overlapping_or_parameterized_target_budget"

    injected_targets = _as_list(injection.get("fail_to_pass"))
    if not verification.get("pass_to_fail") and any("::" not in test for test in injected_targets):
        return "broad_parent_target_selector"

    broken_p2p = set(_as_list(verification.get("p2p_buggy_failed_tests")))
    official_targets = set(
        _as_list(candidate.get("fail_to_pass") or candidate.get("FAIL_TO_PASS"))
    )
    if broken_p2p and broken_p2p.issubset(official_targets):
        return "official_targets_mislabeled_as_adjacent_p2p"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--repos-dir", type=Path, default=Path(".pri-workspace/repos"))
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    candidates = {row_id(row): row for row in read_jsonl(args.candidate_file) if row_id(row)}
    injections: dict[str, dict[str, Any]] = {}
    for path in sorted(args.run_dir.glob("shard_new_l1l2_*/verified_injection_results.jsonl")):
        if "harness_retry" in str(path):
            continue
        for row in read_jsonl(path):
            if row.get("success") and row_id(row):
                injections[row_id(row)] = row

    verification_rows: list[dict[str, Any]] = []
    for path in sorted(args.run_dir.glob("shard_new_l1l2_*/verified_verification_results.jsonl")):
        if "harness_retry" in str(path):
            continue
        verification_rows.extend(read_jsonl(path))

    passed_ids = {row_id(row) for row in verification_rows if strict_pass(row)}
    selected: dict[str, tuple[dict[str, Any], str]] = {}
    for verification_row in verification_rows:
        iid = row_id(verification_row)
        if not iid or iid in passed_ids or iid not in injections or iid not in candidates:
            continue
        reason = harness_false_reject_reason(
            verification_row, injections[iid], candidates[iid]
        )
        if reason:
            selected[iid] = (verification_row, reason)

    output_dir = args.run_dir / "shard_new_l1l2_harness_retry"
    output_dir.mkdir(parents=True, exist_ok=True)
    injection_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    for iid, (_, reason) in sorted(selected.items()):
        injection = dict(injections[iid])
        injection["harness_reverification_reason"] = reason
        injection_rows.append(injection)
        candidate_rows.append(candidates[iid])

    injection_path = output_dir / "verified_injection_results.jsonl"
    candidate_path = output_dir / "candidate_pool.jsonl"
    verification_path = output_dir / "verified_verification_results.jsonl"
    strict_pass_path = output_dir / "strict_passes.jsonl"
    write_jsonl(injection_path, injection_rows)
    write_jsonl(candidate_path, candidate_rows)
    summary = {
        "run_dir": str(args.run_dir),
        "successful_injections": len(injections),
        "verification_rows": len(verification_rows),
        "strict_pass_ids": len(passed_ids),
        "harness_reverification_rows": len(injection_rows),
        "by_reason": {
            reason: sum(1 for _, selected_reason in selected.values() if selected_reason == reason)
            for reason in sorted({selected_reason for _, selected_reason in selected.values()})
        },
    }
    (output_dir / "harness_reverification_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.prepare_only or not injection_rows:
        return

    worktrees_dir = ROOT / ".pri-workspace/v2_pro_harness_reverification_worktrees"
    cmd = [
        sys.executable,
        str(ROOT / "scripts/verify_swebench_pro.py"),
        "--injection-results", str(injection_path),
        "--sampled-data", str(candidate_path),
        "--output", str(verification_path),
        "--repos-dir", str(args.repos_dir),
        "--worktrees-dir", str(worktrees_dir),
        "--timeout", "300",
        "--check-pass-to-pass",
        "--clean-pass-to-pass",
        "--require-clean-pass-to-pass",
        "--max-pass-to-pass", "32",
        "--max-adjacent-pass-to-pass", "12",
        "--check-golden-repair",
        "--max-target-tests", "6",
        "--force",
    ]
    returncode = subprocess.run(cmd, cwd=ROOT).returncode
    strict_rows = [
        row for row in read_jsonl(verification_path) if strict_pass(row)
    ]
    write_jsonl(strict_pass_path, strict_rows)
    print(f"Harness strict passes: {len(strict_rows)}/{len(injection_rows)}")
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
