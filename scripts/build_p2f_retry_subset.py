"""Build a candidate subset for semantic retry from P2F-miss verifications."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--verification-results", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max", type=int, default=None)
    args = parser.parse_args()

    candidates = {
        row["instance_id"]: row
        for row in read_jsonl(Path(args.candidates))
        if row.get("instance_id")
    }
    rows: list[dict] = []
    for row in read_jsonl(Path(args.verification_results)):
        verification = row.get("verification") or {}
        iid = row.get("instance_id")
        if (
            iid in candidates
            and verification.get("status") == "completed"
            and verification.get("pass_to_fail") is False
        ):
            retry_row = dict(candidates[iid])
            retry_row["p2f_retry_source_verification"] = row
            rows.append(retry_row)

    if args.max is not None:
        rows = rows[: args.max]
    write_jsonl(Path(args.output), rows)
    print(f"p2f_miss_retry_candidates: {len(rows)}")
    print(f"output: {args.output}")


if __name__ == "__main__":
    main()
