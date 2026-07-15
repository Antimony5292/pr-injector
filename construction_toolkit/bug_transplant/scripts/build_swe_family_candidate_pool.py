"""Build a PR-INJECTOR candidate pool from SWE-bench-family datasets.

The current PR-INJECTOR construction harness is Python/SWE-bench-shaped. This
adapter therefore separates datasets into:

* main-compatible rows: usable by the current A-to-modern-B transplantation loop;
* optional Lite rows: available for smoke/debug only, not default B500 growth;
* deferred rows: useful future sources, but requiring multimodal or multilingual
  harness work before they should enter B500 construction.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable

try:
    from prinjector_v2_metrics import complexity_bin, patch_profile, read_jsonl, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import complexity_bin, patch_profile, read_jsonl, write_jsonl


DEFAULT_DATASETS = [
    "SWE-bench-Live/SWE-bench-Live:test",
    "princeton-nlp/SWE-bench_Verified:test",
    "ScaleAI/SWE-bench_Pro:test",
    "SWE-bench/SWE-bench_Lite:test",
]

DEFAULT_PREFER_REPOS = [
    "ansible/ansible",
    "psf/requests",
    "pallets/flask",
    "internetarchive/openlibrary",
    "sympy/sympy",
    "matplotlib/matplotlib",
]

MAIN_COMPATIBLE_DATASETS = {
    "SWE-bench-Live/SWE-bench-Live",
    "princeton-nlp/SWE-bench_Verified",
    "ScaleAI/SWE-bench_Pro",
    "SWE-bench/SWE-bench_Lite",
}


def parse_dataset_spec(value: str) -> tuple[str, str]:
    if ":" not in value:
        return value, "test"
    dataset, split = value.rsplit(":", 1)
    return dataset, split or "test"


def coerce_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [text]
    return [str(value)]


def row_id(row: dict[str, Any]) -> str:
    return str(
        row.get("source_instance_id")
        or row.get("instance_id")
        or row.get("id")
        or ""
    )


def repo_of(row: dict[str, Any]) -> str:
    return str(row.get("repo") or row.get("repository") or row.get("repo_name") or "")


def dataset_of(row: dict[str, Any]) -> str:
    return str(row.get("source_dataset") or "")


def load_rows(dataset: str, split: str) -> Iterable[dict[str, Any]]:
    path = Path(dataset)
    if path.exists():
        yield from read_jsonl(path)
        return
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Python package 'datasets' is required to fetch Hugging Face datasets. "
            "Use .venv/bin/python or install datasets in the selected environment."
        ) from exc
    try:
        ds = load_dataset(dataset, split=split)
    except Exception:
        if split != "auto":
            raise
        ds_map = load_dataset(dataset)
        preferred = next((name for name in ("test", "validation", "dev", "train") if name in ds_map), None)
        if preferred is None:
            preferred = next(iter(ds_map.keys()))
        ds = ds_map[preferred]
    for row in ds:
        yield dict(row)


def normalize_row(raw: dict[str, Any], source_dataset: str, split: str) -> dict[str, Any]:
    iid = str(raw.get("instance_id") or raw.get("id") or "")
    fail_to_pass = coerce_list(raw.get("fail_to_pass") or raw.get("FAIL_TO_PASS"))
    pass_to_pass = coerce_list(raw.get("pass_to_pass") or raw.get("PASS_TO_PASS"))
    patch = str(raw.get("patch") or raw.get("gold_patch") or raw.get("fix_patch") or "")
    test_patch = str(raw.get("test_patch") or raw.get("tests_patch") or "")
    repo = repo_of(raw)
    language = str(raw.get("language") or raw.get("programming_language") or "").lower()
    profile = patch_profile(patch)
    row = dict(raw)
    row.update(
        {
            "instance_id": iid,
            "source_instance_id": iid,
            "source_dataset": source_dataset,
            "source_split": split,
            "repo": repo,
            "base_commit": str(raw.get("base_commit") or raw.get("commit") or raw.get("base_sha") or ""),
            "patch": patch,
            "test_patch": test_patch,
            "problem_statement": str(raw.get("problem_statement") or raw.get("issue") or raw.get("description") or ""),
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "FAIL_TO_PASS": fail_to_pass,
            "PASS_TO_PASS": pass_to_pass,
            "language": language,
            "swe_family_patch_profile": {
                "line_changes": profile.line_changes,
                "hunks": profile.hunks,
                "source_files": profile.source_files,
                "complexity_bin": complexity_bin(profile.line_changes),
            },
        }
    )
    row["candidate_compatibility_status"] = compatibility_status(row)
    return row


def compatibility_status(row: dict[str, Any]) -> str:
    dataset = dataset_of(row)
    language = str(row.get("language") or "").lower()
    if not row_id(row):
        return "missing_instance_id"
    if not repo_of(row):
        return "missing_repo"
    if "Multimodal" in dataset:
        return "deferred_multimodal_visual_js_harness"
    if "Multilingual" in dataset or dataset.endswith("/MultiLang"):
        return "deferred_multilingual_non_python_harness"
    if language and language not in {"python", "py"}:
        return "deferred_non_python_harness"
    if not str(row.get("patch") or "").strip():
        return "missing_gold_patch"
    if not patch_has_python_source(str(row.get("patch") or "")):
        return "deferred_non_python_patch_harness"
    if dataset in MAIN_COMPATIBLE_DATASETS:
        return "main_compatible"
    return "needs_manual_compatibility_review"


def patch_has_python_source(patch: str) -> bool:
    for line in patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[6:].strip()
        if path == "/dev/null":
            continue
        lowered = path.lower()
        if "/tests/" in lowered or lowered.startswith("tests/"):
            continue
        name = lowered.rsplit("/", 1)[-1]
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        if lowered.endswith(".py"):
            return True
    return False


def load_selected(path: Path | None) -> tuple[set[str], Counter[str]]:
    if not path:
        return set(), Counter()
    rows = read_jsonl(path)
    return {row_id(row) for row in rows if row_id(row)}, Counter(repo_of(row) for row in rows)


def row_score(row: dict[str, Any], selected_by_repo: Counter[str], prefer_repos: set[str]) -> float:
    dataset_priority = {
        "SWE-bench-Live/SWE-bench-Live": 7.0,
        "ScaleAI/SWE-bench_Pro": 6.0,
        "princeton-nlp/SWE-bench_Verified": 5.0,
        "SWE-bench/SWE-bench_Lite": 1.0,
    }.get(dataset_of(row), 0.0)
    repo = repo_of(row)
    underused_bonus = max(0.0, (12 - selected_by_repo.get(repo, 0)) / 4.0)
    prefer_bonus = 2.5 if repo in prefer_repos else 0.0
    profile = row.get("swe_family_patch_profile") or {}
    line_changes = int(profile.get("line_changes") or 0)
    hunks = int(profile.get("hunks") or 0)
    source_files = int(profile.get("source_files") or 0)
    shape_bonus = 0.0
    if line_changes >= 11:
        shape_bonus += 0.8
    if line_changes >= 31:
        shape_bonus += 0.8
    if hunks >= 2:
        shape_bonus += 0.6
    if source_files >= 2:
        shape_bonus += 0.6
    if line_changes > 300 or hunks > 40 or source_files > 10:
        shape_bonus -= 0.8
    return round(dataset_priority + underused_bonus + prefer_bonus + shape_bonus, 4)


def round_robin(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        buckets[(dataset_of(row), repo_of(row))].append(row)
    for key, bucket in list(buckets.items()):
        buckets[key] = deque(sorted(bucket, key=lambda row: -float(row.get("swe_family_candidate_score") or 0.0)))
    keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key[0], key[1]))
    out: list[dict[str, Any]] = []
    while keys and (not limit or len(out) < limit):
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            if not limit or len(out) < limit:
                out.append(buckets[key].popleft())
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", action="append", default=[])
    parser.add_argument("--selected-existing")
    parser.add_argument("--prefer-repo", action="append", default=[])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-rows-per-dataset", type=int, default=0)
    parser.add_argument("--main-limit", type=int, default=360)
    parser.add_argument("--include-lite-main", action="store_true")
    args = parser.parse_args()

    dataset_specs = args.dataset or DEFAULT_DATASETS
    selected_ids, selected_by_repo = load_selected(Path(args.selected_existing) if args.selected_existing else None)
    prefer_repos = set(DEFAULT_PREFER_REPOS) | set(args.prefer_repo)

    all_rows: list[dict[str, Any]] = []
    load_failures: list[dict[str, str]] = []
    for spec in dataset_specs:
        dataset, split = parse_dataset_spec(spec)
        try:
            rows = []
            for idx, raw in enumerate(load_rows(dataset, split), start=1):
                if args.max_rows_per_dataset and idx > args.max_rows_per_dataset:
                    break
                rows.append(normalize_row(raw, dataset, split))
        except Exception as exc:
            load_failures.append({"dataset": dataset, "split": split, "error": str(exc)[:500]})
            continue
        all_rows.extend(rows)

    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in all_rows:
        key = (dataset_of(row), row_id(row))
        if row_id(row) and key not in deduped:
            deduped[key] = row
    all_rows = list(deduped.values())

    for row in all_rows:
        row["swe_family_selected_existing"] = row_id(row) in selected_ids
        row["swe_family_selected_repo_count"] = selected_by_repo.get(repo_of(row), 0)
        row["swe_family_candidate_score"] = row_score(row, selected_by_repo, prefer_repos)

    main_statuses = {"main_compatible"}
    if args.include_lite_main:
        main_statuses.add("optional_lite_overlap_lower_complexity")
    main_candidates = [
        row
        for row in all_rows
        if row.get("candidate_compatibility_status") in main_statuses
        and not row.get("swe_family_selected_existing")
    ]
    main_ordered = round_robin(main_candidates, args.main_limit)
    deferred = [row for row in all_rows if str(row.get("candidate_compatibility_status", "")).startswith("deferred")]
    optional_lite = [
        row
        for row in all_rows
        if row.get("candidate_compatibility_status") == "optional_lite_overlap_lower_complexity"
    ]
    rejected = [
        row
        for row in all_rows
        if row.get("candidate_compatibility_status")
        not in {"main_compatible", "optional_lite_overlap_lower_complexity"}
        and not str(row.get("candidate_compatibility_status", "")).startswith("deferred")
    ]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "candidate_pool_all.jsonl", all_rows)
    write_jsonl(output_dir / "candidate_pool_main_compatible.jsonl", main_ordered)
    write_jsonl(output_dir / "candidate_pool_deferred.jsonl", deferred)
    write_jsonl(output_dir / "candidate_pool_lite_optional.jsonl", optional_lite)
    write_jsonl(output_dir / "candidate_pool_rejected_or_review.jsonl", rejected)

    summary = {
        "dataset_specs": dataset_specs,
        "load_failures": load_failures,
        "all_rows": len(all_rows),
        "selected_existing_rows": len(selected_ids),
        "main_compatible_queued": len(main_ordered),
        "main_limit": args.main_limit,
        "include_lite_main": args.include_lite_main,
        "prefer_repos": sorted(prefer_repos),
        "by_dataset": dict(Counter(dataset_of(row) for row in all_rows)),
        "by_compatibility_status": dict(Counter(str(row.get("candidate_compatibility_status")) for row in all_rows)),
        "main_by_dataset": dict(Counter(dataset_of(row) for row in main_ordered)),
        "main_by_repo_top50": dict(Counter(repo_of(row) for row in main_ordered).most_common(50)),
        "deferred_by_status": dict(Counter(str(row.get("candidate_compatibility_status")) for row in deferred)),
        "outputs": {
            "all": str(output_dir / "candidate_pool_all.jsonl"),
            "main_compatible": str(output_dir / "candidate_pool_main_compatible.jsonl"),
            "deferred": str(output_dir / "candidate_pool_deferred.jsonl"),
            "lite_optional": str(output_dir / "candidate_pool_lite_optional.jsonl"),
            "rejected_or_review": str(output_dir / "candidate_pool_rejected_or_review.jsonl"),
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
