"""Audit progress for the strict conservative RQ2 B construction run."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def summarize_injection(rows: list[dict]) -> dict:
    failures = Counter(str(row.get("failure_reason")) for row in rows if not row.get("success"))
    level3_rows = [
        row for row in rows
        if str(row.get("injection_level", "")).startswith("Level_3")
    ]
    return {
        "attempted": len(rows),
        "success": sum(bool(row.get("success")) for row in rows),
        "levels": dict(Counter(row.get("injection_level") for row in rows)),
        "level3_rows": len(level3_rows),
        "l1_l2_only_clean": len(level3_rows) == 0,
        "top_failures": failures.most_common(12),
    }


def summarize_verification(rows: list[dict]) -> dict:
    verifications = [row.get("verification") or {} for row in rows]
    strict_ok = [
        row for row in rows
        if (row.get("verification") or {}).get("pass_to_fail")
        and (row.get("verification") or {}).get("golden_repair_pass") is True
        and (row.get("verification") or {}).get("p2p_repaired_pass") is True
        and int((row.get("verification") or {}).get("p2p_buggy_failed") or 0) == 0
    ]
    return {
        "verified_rows": len(rows),
        "p2f_confirmed": sum(bool(v.get("pass_to_fail")) for v in verifications),
        "strict_candidate_count": len(strict_ok),
        "status": dict(Counter(v.get("status") for v in verifications)),
        "top_reasons": Counter(v.get("reason") for v in verifications).most_common(12),
        "top_levels": dict(Counter(row.get("injection_level") for row in strict_ok)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-dir",
        default="experiments/rq2_100/strict_conservative_fullpool_20260603",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    payload = {
        "run_dir": str(run_dir),
        "pro_injection": summarize_injection(read_jsonl(run_dir / "pro_injection_results.jsonl")),
        "pro_verification": summarize_verification(read_jsonl(run_dir / "pro_verification_results.jsonl")),
        "verified_injection": summarize_injection(read_jsonl(run_dir / "verified_injection_results.jsonl")),
        "verified_verification": summarize_verification(read_jsonl(run_dir / "verified_verification_results.jsonl")),
    }
    payload["ready"] = {
        "pro_50": payload["pro_verification"]["strict_candidate_count"] >= 50,
        "verified_50": payload["verified_verification"]["strict_candidate_count"] >= 50,
        "no_level3_rows": (
            payload["pro_injection"]["level3_rows"] == 0
            and payload["verified_injection"]["level3_rows"] == 0
        ),
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
