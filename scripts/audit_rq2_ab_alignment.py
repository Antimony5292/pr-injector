"""Audit RQ2 A/B alignment and common B-vs-A drift signals."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-case", required=True)
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    per_case = read_jsonl(Path(args.per_case))
    pairing = {row["case_id"]: row for row in read_jsonl(Path(args.pairing))}

    rows = []
    for row in per_case:
        pair = pairing.get(row["case_id"], {})
        b_ftp = pair.get("B_FAIL_TO_PASS") or []
        b_ptp = pair.get("B_PASS_TO_PASS_CLEAN") or pair.get("B_PASS_TO_PASS") or []
        a_ftp = pair.get("A_FAIL_TO_PASS") or []
        a_ptp = pair.get("A_PASS_TO_PASS") or []
        rows.append({
            **row,
            "A_FAIL_TO_PASS_COUNT": len(a_ftp),
            "A_PASS_TO_PASS_COUNT": len(a_ptp),
            "B_FAIL_TO_PASS_COUNT": len(b_ftp),
            "B_PASS_TO_PASS_COUNT": len(b_ptp),
            "B_single_target": len(b_ftp) == 1,
            "B_low_regression": len(b_ptp) <= 5,
        })

    quadrants = Counter(row.get("quadrant") for row in rows)
    summary = {
        "total": len(rows),
        "A_solved": sum(bool(row.get("A_solved")) for row in rows),
        "B_solved": sum(bool(row.get("B_solved")) for row in rows),
        "quadrants": dict(quadrants),
        "B_reason_counts": dict(Counter(row.get("B_reason") for row in rows)),
        "A0_B1_single_target": sum(
            row["quadrant"] == "A0_B1" and row["B_single_target"] for row in rows
        ),
        "A0_B1_total": quadrants.get("A0_B1", 0),
        "A0_B1_low_regression": sum(
            row["quadrant"] == "A0_B1" and row["B_low_regression"] for row in rows
        ),
        "by_quadrant_repo": {
            str(key): value
            for key, value in Counter((row.get("quadrant"), row.get("repo")) for row in rows).items()
        },
        "avg_counts_by_quadrant": {},
    }

    for quadrant in sorted(quadrants):
        qrows = [row for row in rows if row.get("quadrant") == quadrant]
        if not qrows:
            continue
        summary["avg_counts_by_quadrant"][quadrant] = {
            "A_FAIL_TO_PASS": sum(row["A_FAIL_TO_PASS_COUNT"] for row in qrows) / len(qrows),
            "A_PASS_TO_PASS": sum(row["A_PASS_TO_PASS_COUNT"] for row in qrows) / len(qrows),
            "B_FAIL_TO_PASS": sum(row["B_FAIL_TO_PASS_COUNT"] for row in qrows) / len(qrows),
            "B_PASS_TO_PASS": sum(row["B_PASS_TO_PASS_COUNT"] for row in qrows) / len(qrows),
        }

    text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
