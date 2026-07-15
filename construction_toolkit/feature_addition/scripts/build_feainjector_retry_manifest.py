"""Build a deduplicated Feature Addition retry manifest.

The retry lane joins construction failures back to their original source rows
and excludes cases that have already passed strict verification.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import read_jsonl, write_jsonl


DEFAULT_RETRY_STATUSES = (
    "semantic_model_patch_apply_failed",
    "semantic_model_fidelity_gate_failed",
    "semantic_model_no_diff",
    "semantic_model_rejected",
    "semantic_model_empty_patch",
    "semantic_model_exception",
)


def row_id(row: dict) -> str:
    return str(row.get("instance_id") or row.get("case_id") or row.get("source_instance_id") or "")


def is_strict(row: dict) -> bool:
    verification = row.get("verification") or row
    explicit = verification.get("strict_verified")
    if explicit is None:
        explicit = verification.get("strict_pass")
    return explicit is True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--construction-results", required=True, type=Path)
    parser.add_argument("--strict-results", action="append", default=[], type=Path)
    parser.add_argument("--verification-results", action="append", default=[], type=Path)
    parser.add_argument(
        "--retry-verification-reason",
        action="append",
        default=[],
        help="When set, retry only non-strict verification rows with these reason values",
    )
    parser.add_argument("--retry-status", action="append", default=[])
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    source_rows = read_jsonl(args.source_manifest)
    source_by_id = {row_id(row): row for row in source_rows if row_id(row)}
    results = read_jsonl(args.construction_results)
    strict_ids = {
        row_id(row)
        for path in args.strict_results
        for row in read_jsonl(path)
        if row_id(row) and is_strict(row)
    }
    retry_statuses = set(args.retry_status or DEFAULT_RETRY_STATUSES)

    verification_failures: dict[str, dict] = {}
    retry_verification_reasons = set(args.retry_verification_reason)
    for path in args.verification_results:
        for row in read_jsonl(path):
            iid = row_id(row)
            reason = str(
                row.get("failure_reason")
                or row.get("reason")
                or row.get("status")
                or "verification_not_strict"
            )
            if retry_verification_reasons and reason not in retry_verification_reasons:
                continue
            if iid and iid not in strict_ids and not is_strict(row):
                verification_failures[iid] = row

    selected: list[dict] = []
    missing_source = 0
    seen: set[str] = set()
    status_counts: Counter[str] = Counter()
    for result in results:
        iid = row_id(result)
        status = str(result.get("status") or "")
        if not iid or iid in seen or iid in strict_ids or status not in retry_statuses:
            continue
        source = source_by_id.get(iid)
        if source is None:
            missing_source += 1
            continue
        copied = dict(source)
        copied["feature_retry_previous_status"] = status
        copied["feature_retry_previous_reason"] = str(result.get("reason") or "")[:2000]
        selected.append(copied)
        status_counts[status] += 1
        seen.add(iid)

    for iid, verification in verification_failures.items():
        if iid in seen or iid in strict_ids:
            continue
        source = source_by_id.get(iid)
        if source is None:
            missing_source += 1
            continue
        status = str(
            verification.get("failure_reason")
            or verification.get("reason")
            or verification.get("status")
            or "verification_not_strict"
        )
        copied = dict(source)
        copied["feature_retry_previous_status"] = f"verification:{status}"
        copied["feature_retry_previous_reason"] = str(
            verification.get("reason")
            or verification.get("verification_blocker")
            or verification.get("failure_reason")
            or status
        )[:2000]
        selected.append(copied)
        status_counts[f"verification:{status}"] += 1
        seen.add(iid)

    selected.sort(
        key=lambda row: (
            0 if row.get("feature_retry_previous_status") == "semantic_model_fidelity_gate_failed" else 1,
            int((row.get("external_queue_profile") or {}).get("line_changes") or 0),
            row_id(row),
        )
    )
    if args.limit > 0:
        selected = selected[: args.limit]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / "feature_retry_manifest.jsonl"
    write_jsonl(output, selected)
    summary = {
        "source_rows": len(source_rows),
        "construction_rows": len(results),
        "strict_ids": len(strict_ids),
        "retry_rows": len(selected),
        "retry_status_counts": dict(status_counts),
        "retry_verification_reasons": sorted(retry_verification_reasons),
        "missing_source_rows": missing_source,
        "output": str(output),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
