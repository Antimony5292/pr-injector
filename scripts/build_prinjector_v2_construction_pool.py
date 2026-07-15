"""Build a quota-aware PR-INJECTOR v2 construction pool.

The pool is for new B500 construction, not post-hoc selection. It starts from
already accepted rows, computes dataset/repo deficits, then queues retry and
fresh candidates with enough oversampling to survive construction failures.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import complexity_bin, patch_profile, read_jsonl, write_jsonl


DEFAULT_QUOTAS = {
    "princeton-nlp/SWE-bench": 250,
    "princeton-nlp/SWE-bench_Verified": 125,
    "ScaleAI/SWE-bench_Pro": 125,
}

SKIP_DIRS = {
    ".git",
    "__pycache__",
    "diffs",
    "injected_diffs",
    "golden_patches",
    "logs",
    "node_modules",
}


def row_id(row: dict[str, Any]) -> str:
    return str(
        row.get("A_instance_id")
        or row.get("source_instance_id")
        or row.get("B_source_instance_id")
        or row.get("instance_id")
        or ""
    )


def dataset_of(row: dict[str, Any]) -> str:
    return str(row.get("source_dataset") or row.get("A_dataset") or "")


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


def load_candidates(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in out:
                out[iid] = row
    return out


def load_processed_ids(roots: list[Path]) -> set[str]:
    ids: set[str] = set()
    for root in roots:
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
            for filename in filenames:
                if not filename.endswith("injection_results.jsonl"):
                    continue
                for row in read_jsonl(Path(dirpath) / filename):
                    iid = row_id(row)
                    if iid:
                        ids.add(iid)
    return ids


def ranked_fresh_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[int, int, int, str]:
        profile = patch_profile(str(row.get("patch") or ""))
        return (
            -profile.source_files,
            -profile.hunks,
            -profile.line_changes,
            row_id(row),
        )

    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        profile = patch_profile(str(row.get("patch") or ""))
        buckets[(dataset_of(row), str(row.get("repo") or ""), complexity_bin(profile.line_changes))].append(row)
    for bucket_key in list(buckets):
        buckets[bucket_key] = sorted(buckets[bucket_key], key=key)

    ordered: list[dict[str, Any]] = []
    keys = sorted(buckets, key=lambda k: (k[0], k[1], k[2]))
    while keys:
        next_keys: list[tuple[str, str, str]] = []
        for bucket_key in keys:
            rows = buckets[bucket_key]
            if rows:
                ordered.append(rows.pop(0))
            if rows:
                next_keys.append(bucket_key)
        keys = next_keys
    return ordered


def passes_complexity_floor(row: dict[str, Any], min_line_changes: int, min_hunks: int, min_files: int) -> bool:
    profile = patch_profile(str(row.get("patch") or ""))
    return (
        profile.line_changes >= min_line_changes
        and profile.hunks >= min_hunks
        and profile.source_files >= min_files
    )


def mark(row: dict[str, Any], source: str) -> dict[str, Any]:
    out = dict(row)
    out["v2_construction_source"] = source
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected-existing", required=True)
    parser.add_argument("--retry-candidates", required=True)
    parser.add_argument("--candidate-root", action="append", required=True)
    parser.add_argument("--exclude-id-file", action="append", default=[])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-size", type=int, default=500)
    parser.add_argument("--repo-cap", type=int, default=50)
    parser.add_argument("--oversample", type=float, default=2.5)
    parser.add_argument("--fresh-cap", type=int, default=900)
    parser.add_argument("--include-processed-fresh", action="store_true")
    parser.add_argument("--exclude-repo", action="append", default=[])
    parser.add_argument("--include-dataset", action="append", default=[])
    parser.add_argument("--min-fresh-line-changes", type=int, default=0)
    parser.add_argument("--min-fresh-hunks", type=int, default=0)
    parser.add_argument("--min-fresh-files", type=int, default=0)
    parser.add_argument("--allow-repo-cap-replacement", action="store_true")
    args = parser.parse_args()

    selected = read_jsonl(Path(args.selected_existing))
    retry = read_jsonl(Path(args.retry_candidates))
    explicit_exclude_ids = {
        row_id(row)
        for path in args.exclude_id_file
        for row in read_jsonl(Path(path))
        if row_id(row)
    }
    candidate_roots = [Path(path) for path in args.candidate_root]
    candidate_paths = candidate_paths_from_roots(candidate_roots)
    candidates = load_candidates(candidate_paths)
    processed_ids = set() if args.include_processed_fresh else load_processed_ids(candidate_roots)
    excluded_repos = set(args.exclude_repo)
    included_datasets = set(args.include_dataset) if args.include_dataset else set(DEFAULT_QUOTAS)

    selected_ids = {row_id(row) for row in selected if row_id(row)}
    retry_ids = {row_id(row) for row in retry if row_id(row)}
    selected_by_dataset = Counter(dataset_of(row) for row in selected)
    selected_by_repo = Counter(str(row.get("repo") or "") for row in selected)

    deficits = {
        dataset: max(0, quota - selected_by_dataset.get(dataset, 0))
        for dataset, quota in DEFAULT_QUOTAS.items()
    }

    queued: list[dict[str, Any]] = []
    supplemental_retry: list[dict[str, Any]] = []
    queue_ids: set[str] = set()
    projected_dataset = Counter(selected_by_dataset)
    projected_repo = Counter(selected_by_repo)

    for row in retry:
        iid = row_id(row)
        dataset = dataset_of(row)
        repo = str(row.get("repo") or "")
        if dataset not in included_datasets:
            continue
        if not iid or iid in selected_ids:
            continue
        if deficits.get(dataset, 0) <= 0 or projected_repo[repo] >= args.repo_cap:
            supplemental_retry.append(mark(row, "v2_retry_supplemental_not_main_quota"))
            continue
        if repo in excluded_repos:
            supplemental_retry.append(mark(row, "v2_retry_supplemental_excluded_repo"))
            continue
        queued.append(mark(row, "v2_retry_gate_fail"))
        queue_ids.add(iid)
        projected_dataset[dataset] += 1
        projected_repo[repo] += 1

    # Oversample fresh candidates per dataset based on remaining target deficit.
    remaining_deficits = {
        dataset: max(0, DEFAULT_QUOTAS[dataset] - projected_dataset.get(dataset, 0))
        for dataset in DEFAULT_QUOTAS
    }
    fresh_targets = {
        dataset: int(max(remaining_deficits[dataset], round(remaining_deficits[dataset] * args.oversample)))
        for dataset in DEFAULT_QUOTAS
    }
    fresh_counts: Counter[str] = Counter()
    replacement_counts: Counter[str] = Counter()
    fresh_pool = [
        row
        for iid, row in candidates.items()
        if iid
        and iid not in selected_ids
        and iid not in retry_ids
        and iid not in queue_ids
        and iid not in explicit_exclude_ids
        and (args.include_processed_fresh or iid not in processed_ids)
        and dataset_of(row) in included_datasets
        and str(row.get("repo") or "") not in excluded_repos
        and passes_complexity_floor(
            row,
            args.min_fresh_line_changes,
            args.min_fresh_hunks,
            args.min_fresh_files,
        )
    ]
    # Prioritize datasets with the largest current deficits. Otherwise the
    # broad SWE-bench pool can fill shared repo caps before scarce
    # Verified/Pro rows get a chance to enter the queue.
    dataset_order = sorted(
        DEFAULT_QUOTAS,
        key=lambda dataset: (
            -remaining_deficits.get(dataset, 0),
            selected_by_dataset.get(dataset, 0),
            dataset,
        ),
    )
    fresh_by_dataset = {
        dataset: ranked_fresh_candidates([row for row in fresh_pool if dataset_of(row) == dataset])
        for dataset in dataset_order
    }
    for dataset in dataset_order:
        for row in fresh_by_dataset[dataset]:
            if fresh_counts[dataset] >= fresh_targets.get(dataset, 0):
                break
            if sum(fresh_counts.values()) >= args.fresh_cap:
                break
            repo = str(row.get("repo") or "")
            if projected_repo[repo] >= args.repo_cap:
                if not args.allow_repo_cap_replacement or remaining_deficits.get(dataset, 0) <= 0:
                    continue
                iid = row_id(row)
                queued.append(mark(row, "fresh_quota_oversample_repo_replacement"))
                queue_ids.add(iid)
                fresh_counts[dataset] += 1
                replacement_counts[dataset] += 1
                projected_dataset[dataset] += 1
                continue
            iid = row_id(row)
            queued.append(mark(row, "fresh_quota_oversample"))
            queue_ids.add(iid)
            fresh_counts[dataset] += 1
            projected_dataset[dataset] += 1
            projected_repo[repo] += 1
        if sum(fresh_counts.values()) >= args.fresh_cap:
            break

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "v2_main_construction_candidates.jsonl", queued)
    write_jsonl(output_dir / "v2_supplemental_retry_candidates.jsonl", supplemental_retry)
    write_jsonl(output_dir / "v2_locked_existing_accepted.jsonl", selected)

    summary = {
        "target_size": args.target_size,
        "repo_cap": args.repo_cap,
        "dataset_quotas": DEFAULT_QUOTAS,
        "selected_existing": len(selected),
        "selected_by_dataset": dict(selected_by_dataset),
        "selected_by_repo_top": dict(selected_by_repo.most_common(30)),
        "deficits_before_queue": deficits,
        "retry_input": len(retry),
        "retry_queued_for_main": sum(1 for row in queued if row.get("v2_construction_source") == "v2_retry_gate_fail"),
        "retry_supplemental": len(supplemental_retry),
        "fresh_targets": fresh_targets,
        "fresh_dataset_order": dataset_order,
        "fresh_queued": dict(fresh_counts),
        "fresh_repo_replacement_queued": dict(replacement_counts),
        "main_queue_rows": len(queued),
        "main_queue_by_dataset": dict(Counter(dataset_of(row) for row in queued)),
        "main_queue_by_source": dict(Counter(row.get("v2_construction_source") for row in queued)),
        "projected_if_all_succeed_by_dataset": dict(projected_dataset),
        "projected_repo_top": dict(projected_repo.most_common(30)),
        "candidate_files": [str(path) for path in candidate_paths],
        "processed_ids_excluded_from_fresh": len(processed_ids),
        "explicit_exclude_ids": len(explicit_exclude_ids),
        "excluded_repos": sorted(excluded_repos),
        "included_datasets": sorted(included_datasets),
        "fresh_complexity_floor": {
            "min_line_changes": args.min_fresh_line_changes,
            "min_hunks": args.min_fresh_hunks,
            "min_files": args.min_fresh_files,
        },
    }
    (output_dir / "v2_construction_pool_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
