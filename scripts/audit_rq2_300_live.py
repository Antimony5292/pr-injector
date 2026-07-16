#!/usr/bin/env python3
"""Summarize live RQ2-300 injection verification artifacts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def is_strict(row: dict) -> bool:
    verification = row.get("verification") or row
    clean_p2p = verification.get("clean_pass_to_pass") or row.get("B_PASS_TO_PASS_CLEAN") or []
    return (
        bool(verification.get("pass_to_fail") or verification.get("p2f_confirmed"))
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and bool(verification.get("golden_repair_pass"))
        and bool(verification.get("p2p_repaired_pass", verification.get("golden_repair_p2p_clean", True)))
        and int(verification.get("p2p_repaired_failed") or 0) == 0
        and len(clean_p2p) >= 1
    )


def summarize_file(path: Path) -> dict:
    rows = []
    if path.exists():
        with path.open() as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
    repos = Counter(row.get("repo", "?") for row in rows)
    levels = Counter(row.get("injection_level") or row.get("level", "?") for row in rows)
    failures = Counter()
    for row in rows:
        if not is_strict(row):
            verification = row.get("verification") or row
            if not (verification.get("healthy_pass") or verification.get("healthy_clean")):
                failures["healthy_target_failed"] += 1
            elif not (verification.get("pass_to_fail") or verification.get("p2f_confirmed")):
                failures["p2f_miss"] += 1
            elif int(verification.get("p2p_buggy_failed") or 0) != 0:
                failures["p2p_regression"] += 1
            elif not verification.get("golden_repair_pass"):
                failures["golden_repair_failed"] += 1
            elif not verification.get("p2p_repaired_pass", verification.get("golden_repair_p2p_clean", True)):
                failures["repaired_p2p_failed"] += 1
            elif not (verification.get("clean_pass_to_pass") or row.get("B_PASS_TO_PASS_CLEAN") or []):
                failures["too_few_clean_p2p"] += 1
            else:
                failures[row.get("failure_reason") or verification.get("verdict") or "unknown"] += 1
    return {
        "path": str(path),
        "rows": len(rows),
        "p2f": sum(1 for row in rows if (row.get("verification") or row).get("pass_to_fail") or (row.get("verification") or row).get("p2f_confirmed")),
        "strict": sum(1 for row in rows if is_strict(row)),
        "p2p": sum(1 for row in rows if int((row.get("verification") or row).get("p2p_buggy_failed") or 0) == 0 and ((row.get("verification") or row).get("clean_pass_to_pass") or row.get("B_PASS_TO_PASS_CLEAN") or [])),
        "golden": sum(1 for row in rows if (row.get("verification") or row).get("golden_repair_pass")),
        "top_repos": repos.most_common(6),
        "levels": levels.most_common(),
        "failures": failures.most_common(8),
    }


def summarize_injection(path: Path) -> dict:
    rows = read_jsonl(path)
    successes = [row for row in rows if row.get("success")]
    failures = Counter(row.get("failure_reason") or row.get("injection_level") or "unknown" for row in rows if not row.get("success"))
    levels = Counter(row.get("injection_level", "?") for row in successes)
    repos = Counter(row.get("repo", "?") for row in successes)
    return {
        "path": str(path),
        "rows": len(rows),
        "success": len(successes),
        "success_top_repos": repos.most_common(8),
        "success_levels": levels.most_common(),
        "failures": failures.most_common(8),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--injections", action="store_true")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    if args.injections:
        total_rows = total_success = 0
        for path in args.files:
            summary = summarize_injection(path)
            total_rows += summary["rows"]
            total_success += summary["success"]
            print(json.dumps(summary, ensure_ascii=False))
        print(json.dumps({"TOTAL": True, "rows": total_rows, "success": total_success}, ensure_ascii=False))
        return 0

    total_rows = total_p2f = total_strict = 0
    strict_repos = Counter()
    for path in args.files:
        summary = summarize_file(path)
        total_rows += summary["rows"]
        total_p2f += summary["p2f"]
        total_strict += summary["strict"]
        if path.exists():
            with path.open() as f:
                for line in f:
                    if line.strip():
                        row = json.loads(line)
                        if is_strict(row):
                            strict_repos[row.get("repo", "?")] += 1
        print(json.dumps(summary, ensure_ascii=False))
    print(
        json.dumps(
            {
                "TOTAL": True,
                "rows": total_rows,
                "p2f": total_p2f,
                "strict": total_strict,
                "strict_top_repos": strict_repos.most_common(12),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
