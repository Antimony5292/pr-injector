"""Build a fresh-first PR-INJECTOR v2 wave2 pool.

This pool is intentionally separate from the retry-heavy builders. The latest
Pro/Verified wave showed that repeatedly queueing old retry rows has low yield;
wave2 starts from raw Pro/Verified candidates, excludes selected/attempted IDs,
applies a small complexity floor, and round-robins across dataset/repo buckets.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl


DEFAULT_DATASETS = {
    "princeton-nlp/SWE-bench_Verified",
    "ScaleAI/SWE-bench_Pro",
}


def row_id(row: dict[str, Any]) -> str:
    return str(
        row.get("source_instance_id")
        or row.get("A_instance_id")
        or row.get("B_source_instance_id")
        or row.get("instance_id")
        or ""
    )


def dataset_of(row: dict[str, Any]) -> str:
    return str(row.get("source_dataset") or row.get("A_dataset") or "")


def repo_of(row: dict[str, Any]) -> str:
    return str(row.get("repo") or "")


def load_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid:
                ids.add(iid)
    return ids


def load_run_ids(run_dirs: list[Path]) -> set[str]:
    ids: set[str] = set()
    for run_dir in run_dirs:
        if not run_dir.exists():
            continue
        for path in run_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl"):
            ids |= load_ids([path])
        for path in run_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl"):
            ids |= load_ids([path])
    return ids


def passes_floor(row: dict[str, Any], min_line_changes: int, min_hunks: int, min_files: int) -> bool:
    profile = patch_profile(str(row.get("patch") or ""))
    return (
        profile.line_changes >= min_line_changes
        and profile.hunks >= min_hunks
        and profile.source_files >= min_files
    )


def rank_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    profile = patch_profile(str(row.get("patch") or ""))
    return (
        -profile.source_files,
        -profile.hunks,
        -profile.line_changes,
        row_id(row),
    )


def round_robin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        buckets[(dataset_of(row), repo_of(row))].append(row)
    for key, bucket in list(buckets.items()):
        buckets[key] = deque(sorted(bucket, key=rank_key))

    keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key[0], key[1]))
    ordered: list[dict[str, Any]] = []
    while keys:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            bucket = buckets[key]
            if bucket:
                ordered.append(bucket.popleft())
            if bucket:
                next_keys.append(key)
        keys = next_keys
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--exclude-file", action="append", default=[])
    parser.add_argument("--exclude-run-dir", action="append", default=[])
    parser.add_argument("--exclude-repo", action="append", default=[])
    parser.add_argument("--include-dataset", action="append", default=[])
    parser.add_argument("--min-line-changes", type=int, default=10)
    parser.add_argument("--min-hunks", type=int, default=2)
    parser.add_argument("--min-files", type=int, default=1)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    include_datasets = set(args.include_dataset) if args.include_dataset else set(DEFAULT_DATASETS)
    exclude_repos = set(args.exclude_repo)
    excluded_ids = load_ids([Path(path) for path in args.exclude_file])
    excluded_ids |= load_run_ids([Path(path) for path in args.exclude_run_dir])

    candidates_by_id: dict[str, dict[str, Any]] = {}
    for path in [Path(path) for path in args.candidate_file]:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in candidates_by_id:
                candidates_by_id[iid] = row

    rejection_counts: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    for iid, row in candidates_by_id.items():
        if iid in excluded_ids:
            rejection_counts["excluded_id"] += 1
            continue
        if dataset_of(row) not in include_datasets:
            rejection_counts["excluded_dataset"] += 1
            continue
        if repo_of(row) in exclude_repos:
            rejection_counts["excluded_repo"] += 1
            continue
        if not passes_floor(row, args.min_line_changes, args.min_hunks, args.min_files):
            rejection_counts["below_complexity_floor"] += 1
            continue
        copied = dict(row)
        copied["v2_construction_source"] = "wave2_fresh_unattempted"
        rows.append(copied)

    ordered = round_robin(rows)
    if args.limit:
        ordered = ordered[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "v2_wave2_fresh_candidates.jsonl"
    write_jsonl(out_path, ordered)

    profiles = [patch_profile(str(row.get("patch") or "")) for row in ordered]
    summary = {
        "candidate_input_rows": len(candidates_by_id),
        "queued": len(ordered),
        "output": str(out_path),
        "include_datasets": sorted(include_datasets),
        "exclude_repos": sorted(exclude_repos),
        "excluded_ids": len(excluded_ids),
        "rejection_counts": dict(rejection_counts),
        "queued_by_dataset": dict(Counter(dataset_of(row) for row in ordered)),
        "queued_by_repo_top30": dict(Counter(repo_of(row) for row in ordered).most_common(30)),
        "complexity_floor": {
            "min_line_changes": args.min_line_changes,
            "min_hunks": args.min_hunks,
            "min_files": args.min_files,
        },
        "queued_patch_profile": {
            "avg_line_changes": round(sum(p.line_changes for p in profiles) / max(len(profiles), 1), 2),
            "avg_hunks": round(sum(p.hunks for p in profiles) / max(len(profiles), 1), 2),
            "avg_source_files": round(sum(p.source_files for p in profiles) / max(len(profiles), 1), 2),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
