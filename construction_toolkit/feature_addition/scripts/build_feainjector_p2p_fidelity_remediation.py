#!/usr/bin/env python3
"""Build strict-case verifier inputs with an A-proportional P2P surface."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("instance_id") or row.get("source_instance_id") or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--strict-cases", type=Path, required=True)
    parser.add_argument("--fidelity-failures", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-regression-ratio", type=float, default=0.25)
    parser.add_argument("--exclude-id", action="append", default=[])
    args = parser.parse_args()

    source = {row_id(row): row for row in read_jsonl(args.source_manifest)}
    strict = {row_id(row): row for row in read_jsonl(args.strict_cases)}
    failures = {row_id(row): row for row in read_jsonl(args.fidelity_failures)}
    excluded = set(args.exclude_id)
    rows: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []

    for instance_id in sorted(failures):
        if instance_id in excluded:
            audit.append({"instance_id": instance_id, "status": "excluded_structural_fidelity_failure"})
            continue
        source_row = source.get(instance_id)
        strict_row = strict.get(instance_id)
        if not source_row or not strict_row:
            raise ValueError(f"missing source or strict row for {instance_id}")
        source_p2p = list(source_row.get("PASS_TO_PASS") or source_row.get("pass_to_pass") or [])
        required = math.ceil(len(source_p2p) * args.min_regression_ratio)
        if not source_p2p or required == 0:
            raise ValueError(f"no source P2P surface for {instance_id}")
        remediated = dict(strict_row)
        remediated["pass_to_pass"] = source_p2p
        remediated["fidelity_remediation"] = {
            "reason": "expand_modern_p2p_to_match_source_regression_surface",
            "source_p2p_count": len(source_p2p),
            "required_modern_p2p_count": required,
            "min_regression_ratio": args.min_regression_ratio,
        }
        rows.append(remediated)
        audit.append(
            {
                "instance_id": instance_id,
                "status": "queued",
                "source_p2p_count": len(source_p2p),
                "required_modern_p2p_count": required,
            }
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "p2p_fidelity_remediation_manifest.jsonl"
    with output.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    summary = {
        "queued": len(rows),
        "excluded": len(audit) - len(rows),
        "min_regression_ratio": args.min_regression_ratio,
        "max_required_modern_p2p_count": max(
            (row.get("required_modern_p2p_count", 0) for row in audit),
            default=0,
        ),
        "output": str(output),
        "audit": audit,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
