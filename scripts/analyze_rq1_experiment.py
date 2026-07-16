#!/usr/bin/env python3
"""Summarize PR-INJECTOR RQ1 construction experiments.

RQ1 is about benchmark construction quality, not agent repair.  This script
joins the candidate pool, injection rows, and verification rows, then reports
both mechanical injection success and strict behavioral success.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def safe_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "sum": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(values),
        "sum": round(sum(values), 4),
        "mean": round(statistics.mean(values), 4),
        "median": round(statistics.median(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
    }


def short_reason(text: Any) -> str:
    reason = str(text or "unknown")
    if "preflight_failed:" in reason:
        return reason.split("preflight_failed:", 1)[1].strip().splitlines()[0][:120]
    if "Level 3:" in reason:
        return "Level 3: " + reason.split("Level 3:", 1)[1].strip().splitlines()[0][:120]
    if "Level 2:" in reason:
        return "Level 2: " + reason.split("Level 2:", 1)[1].strip().splitlines()[0][:120]
    return reason.strip().splitlines()[0][:120] or "unknown"


def strict_verified(verification: dict[str, Any]) -> bool:
    if not verification:
        return False
    if verification.get("status") != "completed":
        return False
    if not verification.get("healthy_pass"):
        return False
    if not verification.get("pass_to_fail"):
        return False
    if not verification.get("golden_repair_pass"):
        return False
    if verification.get("pass_to_pass_checked"):
        if verification.get("no_regression") is not True:
            return False
        if verification.get("p2p_repaired_pass") is False:
            return False
        if int(verification.get("clean_pass_to_pass_count") or 0) <= 0:
            return False
    return True


def estimated_test_runs(verification: dict[str, Any]) -> int:
    if not verification:
        return 0
    runs = 0
    if verification.get("healthy_pass") is not None:
        runs += 1
    if verification.get("target_tests_failed") is not None:
        runs += 1
    if verification.get("pass_to_pass_checked"):
        runs += int(verification.get("pass_to_pass_test_count") or 0)
        runs += 1
    if verification.get("golden_repair_checked"):
        runs += 1
    if verification.get("p2p_repaired_pass") is not None:
        runs += 1
    return runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize RQ1 construction results")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--injection-results", required=True)
    parser.add_argument("--verification-results", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    candidates = read_jsonl(Path(args.candidates))
    injections = {row.get("instance_id"): row for row in read_jsonl(Path(args.injection_results))}
    verifications = {row.get("instance_id"): row for row in read_jsonl(Path(args.verification_results))}

    total = len(candidates)
    injection_rows = [injections.get(row.get("instance_id")) for row in candidates]
    verification_rows = [verifications.get(row.get("instance_id")) for row in candidates]

    by_dataset: Counter[str] = Counter()
    by_repo: Counter[str] = Counter()
    injection_by_level: Counter[str] = Counter()
    strict_by_level: Counter[str] = Counter()
    failure_reasons: Counter[str] = Counter()
    verification_failure_reasons: Counter[str] = Counter()
    injection_seconds_by_level: dict[str, list[float]] = defaultdict(list)
    verification_seconds_by_level: dict[str, list[float]] = defaultdict(list)
    test_runs_by_level: dict[str, list[float]] = defaultdict(list)
    l3_prompt_tokens = 0
    l3_completion_tokens = 0
    l3_attempts: list[float] = []

    injection_success = 0
    strict_success = 0
    for candidate, injection, verification_row in zip(candidates, injection_rows, verification_rows):
        by_dataset[str(candidate.get("source_dataset") or candidate.get("dataset") or "unknown")] += 1
        by_repo[str(candidate.get("repo") or "unknown")] += 1
        if not injection:
            failure_reasons["not_run"] += 1
            continue

        level = str(injection.get("injection_level") or "unknown")
        injection_by_level[level] += 1
        duration = injection.get("injection_duration_seconds")
        if isinstance(duration, (int, float)):
            injection_seconds_by_level[level].append(float(duration))
        l3 = injection.get("l3_metadata") or {}
        l3_prompt_tokens += int(l3.get("prompt_tokens") or 0)
        l3_completion_tokens += int(l3.get("completion_tokens") or 0)
        if isinstance(l3.get("attempt"), (int, float)):
            l3_attempts.append(float(l3["attempt"]))

        if injection.get("success"):
            injection_success += 1
        else:
            failure_reasons[short_reason(injection.get("failure_reason"))] += 1
            continue

        verification = (verification_row or {}).get("verification") or {}
        v_duration = verification.get("duration_seconds")
        if isinstance(v_duration, (int, float)):
            verification_seconds_by_level[level].append(float(v_duration))
        test_runs_by_level[level].append(float(estimated_test_runs(verification)))

        if strict_verified(verification):
            strict_success += 1
            strict_by_level[level] += 1
        else:
            if not verification_row:
                verification_failure_reasons["not_verified"] += 1
            elif verification.get("status") != "completed":
                verification_failure_reasons[str(verification.get("status") or "verification_not_completed")] += 1
            elif not verification.get("healthy_pass"):
                verification_failure_reasons["healthy_target_failed"] += 1
            elif not verification.get("pass_to_fail"):
                verification_failure_reasons["p2f_miss"] += 1
            elif not verification.get("golden_repair_pass"):
                verification_failure_reasons["golden_repair_failed"] += 1
            elif verification.get("pass_to_pass_checked") and verification.get("no_regression") is not True:
                verification_failure_reasons["p2p_regression"] += 1
            elif int(verification.get("clean_pass_to_pass_count") or 0) <= 0:
                verification_failure_reasons["no_clean_p2p"] += 1
            else:
                verification_failure_reasons["strict_gate_failed"] += 1

    summary = {
        "candidate_count": total,
        "injection_rows": sum(1 for row in injection_rows if row),
        "verification_rows": sum(1 for row in verification_rows if row),
        "injection_success": injection_success,
        "injection_success_rate": round(injection_success / total, 4) if total else None,
        "strict_verified_success": strict_success,
        "strict_verified_success_rate": round(strict_success / total, 4) if total else None,
        "by_dataset": dict(by_dataset.most_common()),
        "by_repo": dict(by_repo.most_common()),
        "injection_by_level": dict(injection_by_level.most_common()),
        "strict_success_by_level": dict(strict_by_level.most_common()),
        "injection_failure_reasons": dict(failure_reasons.most_common(30)),
        "verification_failure_reasons": dict(verification_failure_reasons.most_common(30)),
        "injection_duration_seconds_by_level": {
            level: safe_stats(values) for level, values in sorted(injection_seconds_by_level.items())
        },
        "verification_duration_seconds_by_level": {
            level: safe_stats(values) for level, values in sorted(verification_seconds_by_level.items())
        },
        "estimated_test_runs_by_level": {
            level: safe_stats(values) for level, values in sorted(test_runs_by_level.items())
        },
        "l3_usage": {
            "prompt_tokens": l3_prompt_tokens,
            "completion_tokens": l3_completion_tokens,
            "total_tokens": l3_prompt_tokens + l3_completion_tokens,
            "attempts": safe_stats(l3_attempts),
        },
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
