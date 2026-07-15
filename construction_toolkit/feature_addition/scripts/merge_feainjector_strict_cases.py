"""Merge deduplicated strict Feature Addition verification results."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


BUGFIX_SECTION_RE = re.compile(
    r"^\s{0,3}\+?\s*#{1,4}\s*(?:bug\s*fix(?:es)?|fix(?:es)?|bugfix(?:es)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
EXPLICIT_BUG_TYPES = {"bug", "bugfix", "bug-fix", "bug-report"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def row_id(row: dict) -> str:
    return str(row.get("instance_id") or row.get("case_id") or row.get("source_instance_id") or "")


def is_strict(row: dict) -> bool:
    verification = row.get("verification") or row
    explicit = verification.get("strict_verified")
    if explicit is None:
        explicit = verification.get("strict_pass")
    if explicit is not None:
        return explicit is True
    return (
        verification.get("healthy_feature_pass") is True
        and verification.get("feature_missing_fail") is True
        and verification.get("gold_restore_feature_pass") is True
        and int(verification.get("feature_missing_p2p_failed") or 0) == 0
        and verification.get("gold_restore_p2p_pass") is True
    )


def source_feature_rejection(row: dict | None) -> str | None:
    if row is None:
        return "source_metadata_missing"
    task_type = str(row.get("source_task_type") or row.get("task_type") or "").strip().lower().replace("_", "-")
    signal = row.get("feature_signal") or {}
    if task_type in EXPLICIT_BUG_TYPES or signal.get("explicit_bug_task") is True:
        return "source_explicit_bug_task"
    patch = str(row.get("feature_patch") or row.get("patch") or "")
    if BUGFIX_SECTION_RE.search(patch):
        return "source_bugfix_section"
    if row.get("feature_gate_pass") is False:
        return "source_feature_gate_failed"
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verification-results", action="append", required=True)
    parser.add_argument("--source-manifest", action="append", default=[])
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    source_rows: dict[str, dict] = {}
    for value in args.source_manifest:
        for row in read_jsonl(Path(value)):
            iid = row_id(row)
            if iid:
                source_rows[iid] = row

    merged: dict[str, dict] = {}
    rejected = Counter()
    sources = Counter()
    for value in args.verification_results:
        path = Path(value)
        for row in read_jsonl(path):
            iid = row_id(row)
            if not iid:
                rejected["missing_instance_id"] += 1
                continue
            if not is_strict(row):
                rejected["not_strict"] += 1
                continue
            if source_rows:
                source_rejection = source_feature_rejection(source_rows.get(iid))
                if source_rejection:
                    rejected[source_rejection] += 1
                    continue
            copied = dict(row)
            copied["strict_merge_source"] = str(path)
            merged.setdefault(iid, copied)
            sources[str(path)] += 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "strict_feature_addition_cases.jsonl"
    with output.open("w", encoding="utf-8") as handle:
        for iid in sorted(merged):
            handle.write(json.dumps(merged[iid], ensure_ascii=False) + "\n")
    summary = {
        "strict_unique": len(merged),
        "strict_rows_by_source_before_dedup": dict(sources),
        "source_manifests": args.source_manifest,
        "rejected": dict(rejected),
        "output": str(output),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
