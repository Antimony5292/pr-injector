#!/usr/bin/env python3
"""Freeze a deterministic, repo-balanced subset of strict FeaInjector cases."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_strict_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open() as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            instance_id = row.get("instance_id")
            if not instance_id:
                raise ValueError(f"missing instance_id at {path}:{line_number}")
            if instance_id in seen:
                raise ValueError(f"duplicate instance_id: {instance_id}")
            if row.get("strict_verified") is not True:
                raise ValueError(f"non-strict row: {instance_id}")
            required = (
                "healthy_feature_pass",
                "healthy_p2p_pass",
                "feature_p2f",
                "missing_p2p_pass",
                "gold_feature_pass",
                "gold_p2p_pass",
            )
            failed = [field for field in required if row.get(field) is not True]
            if failed:
                raise ValueError(f"incomplete strict evidence for {instance_id}: {failed}")
            seen.add(instance_id)
            rows.append(row)
    return rows


def _freeze(rows: list[dict[str, Any]], target_size: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if len(rows) < target_size:
        raise ValueError(f"only {len(rows)} strict rows; target is {target_size}")

    selected = sorted(rows, key=lambda row: (row["repo"], row["instance_id"]))
    holdout: list[dict[str, Any]] = []
    while len(selected) > target_size:
        repo_counts = Counter(row["repo"] for row in selected)
        max_count = max(repo_counts.values())
        overrepresented_repo = sorted(
            repo for repo, count in repo_counts.items() if count == max_count
        )[-1]
        drop_index = max(
            index
            for index, row in enumerate(selected)
            if row["repo"] == overrepresented_repo
        )
        dropped = selected.pop(drop_index)
        holdout.append(
            {
                "instance_id": dropped["instance_id"],
                "repo": dropped["repo"],
                "reason": "surplus_strict_case_from_most_represented_repo",
            }
        )
    return selected, holdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target-size", type=int, default=70)
    args = parser.parse_args()

    rows = _read_strict_rows(args.input)
    selected, holdout = _freeze(rows, args.target_size)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    output = args.output_dir / "strict_feature_addition_cases.jsonl"
    with output.open("w") as handle:
        for row in selected:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    repo_counts = Counter(row["repo"] for row in selected)
    strategy_counts = Counter(row.get("strategy", "unknown") for row in selected)
    summary = {
        "status": "frozen",
        "target_size": args.target_size,
        "eligible_strict_rows": len(rows),
        "selected_strict_rows": len(selected),
        "holdout_strict_rows": holdout,
        "selection_policy": "drop lexicographically last case from the most represented repo until target size",
        "input": str(args.input),
        "input_sha256": _sha256(args.input),
        "output": str(output),
        "output_sha256": _sha256(output),
        "repo_counts": dict(sorted(repo_counts.items())),
        "strategy_counts": dict(sorted(strategy_counts.items())),
    }
    summary_path = args.output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
