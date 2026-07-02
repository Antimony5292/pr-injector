"""Materialize v2 retry candidates by joining retry feedback to source rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import read_jsonl, write_jsonl


def candidate_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or "")


def build_candidate_index(paths: list[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = candidate_id(row)
            if iid and iid not in index:
                index[iid] = row
    return index


def candidate_paths_from_roots(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            paths.append(root)
            continue
        paths.extend(sorted(root.glob("candidate_pool*.jsonl")))
        paths.extend(sorted(root.glob("*candidates*.jsonl")))
        for child in sorted(path for path in root.iterdir() if path.is_dir()):
            paths.extend(sorted(child.glob("candidate_pool*.jsonl")))
            paths.extend(sorted(child.glob("*candidates*.jsonl")))
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def materialize_row(retry: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    row = dict(candidate)
    row["v2_retry_case_id"] = retry.get("case_id")
    row["v2_previous_B_instance_id"] = retry.get("B_instance_id")
    row["v2_previous_injection_level"] = retry.get("previous_injection_level")
    row["v2_retry_action"] = retry.get("recommended_action")
    row["v2_retry_feedback_prompt"] = retry.get("feedback_prompt")
    row["v2_retry_tags"] = retry.get("v2_tags") or []
    row["v2_retry_reasons"] = retry.get("v2_reasons") or []
    row["v2_retry_target_profile"] = retry.get("target_profile") or {}
    row["v2_retry_current_b_profile"] = retry.get("current_b_profile") or {}
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry-manifest", required=True)
    parser.add_argument("--candidate-file", action="append", default=[])
    parser.add_argument("--candidate-root", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--missing-output", default="")
    args = parser.parse_args()

    retry_rows = read_jsonl(Path(args.retry_manifest))
    candidate_paths = [Path(path) for path in args.candidate_file]
    candidate_paths.extend(candidate_paths_from_roots([Path(path) for path in args.candidate_root]))
    if not candidate_paths:
        raise SystemExit("provide at least one --candidate-file or --candidate-root")
    candidates = build_candidate_index(candidate_paths)

    materialized: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen: set[str] = set()
    for retry in retry_rows:
        iid = str(retry.get("A_instance_id") or retry.get("source_instance_id") or "")
        candidate = candidates.get(iid)
        if not candidate:
            missing.append(retry)
            continue
        if iid in seen:
            continue
        seen.add(iid)
        materialized.append(materialize_row(retry, candidate))

    write_jsonl(Path(args.output), materialized)
    if args.missing_output:
        write_jsonl(Path(args.missing_output), missing)

    summary = {
        "retry_rows": len(retry_rows),
        "candidate_rows": len(candidates),
        "materialized_rows": len(materialized),
        "missing_rows": len(missing),
        "candidate_files": [str(path) for path in candidate_paths],
        "output": args.output,
        "missing_output": args.missing_output,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
