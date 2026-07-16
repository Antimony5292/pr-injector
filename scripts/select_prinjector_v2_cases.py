"""Select a balanced PR-INJECTOR v2 benchmark set from an audit JSONL file.

The selector is intentionally conservative:
  - only v2 fidelity-gate passing rows are eligible by default
  - per-repo caps are enforced before filling remaining slots
  - dataset quotas are explicit and auditable
  - complexity-bin balance is favored within each dataset
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import read_jsonl, write_jsonl


DEFAULT_DATASET_WEIGHTS = {
    "princeton-nlp/SWE-bench": 0.50,
    "princeton-nlp/SWE-bench_Verified": 0.25,
    "ScaleAI/SWE-bench_Pro": 0.25,
}


def parse_quota(values: list[str], target_size: int) -> dict[str, int]:
    if not values:
        quotas = {name: int(target_size * weight) for name, weight in DEFAULT_DATASET_WEIGHTS.items()}
        missing = target_size - sum(quotas.values())
        for name in sorted(quotas, key=quotas.get, reverse=True):
            if missing <= 0:
                break
            quotas[name] += 1
            missing -= 1
        return quotas

    quotas: dict[str, int] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"Invalid --dataset-quota {value!r}; expected DATASET=COUNT")
        name, count = value.split("=", 1)
        quotas[name] = int(count)
    if sum(quotas.values()) != target_size:
        raise SystemExit(
            f"Dataset quotas sum to {sum(quotas.values())}, expected target size {target_size}"
        )
    return quotas


def row_sort_key(row: dict[str, Any]) -> tuple[float, int, int, str]:
    """Prefer high-fidelity, non-tiny, wider-test-surface rows."""

    tiny_penalty = 1 if row.get("B_complexity_bin") == "tiny_0_2" else 0
    p2p = int(row.get("B_PASS_TO_PASS_count") or 0)
    ftp = int(row.get("B_FAIL_TO_PASS_count") or 0)
    return (-float(row.get("v2_score") or 0), tiny_penalty, -p2p - ftp, str(row.get("case_id")))


def select_for_dataset(
    rows: list[dict[str, Any]],
    quota: int,
    repo_cap: int,
    repo_counts: Counter[str],
    rng: random.Random,
) -> list[dict[str, Any]]:
    by_bin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_bin[str(row.get("A_complexity_bin") or "unknown")].append(row)
    for bin_rows in by_bin.values():
        rng.shuffle(bin_rows)
        bin_rows.sort(key=row_sort_key)

    selected: list[dict[str, Any]] = []
    bins = sorted(by_bin, key=lambda key: len(by_bin[key]), reverse=True)

    # Round-robin over complexity bins first, then greedily fill leftovers.
    while len(selected) < quota and any(by_bin.values()):
        made_progress = False
        for bin_name in bins:
            bucket = by_bin[bin_name]
            while bucket:
                row = bucket.pop(0)
                repo = str(row.get("repo"))
                if repo_counts[repo] >= repo_cap:
                    continue
                selected.append(row)
                repo_counts[repo] += 1
                made_progress = True
                break
            if len(selected) >= quota:
                break
        if not made_progress:
            break

    return selected


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "rank",
        "case_id",
        "source_dataset",
        "repo",
        "A_instance_id",
        "B_instance_id",
        "B_injection_level",
        "v2_score",
        "A_complexity_bin",
        "B_complexity_bin",
        "A_patch_line_changes",
        "B_patch_line_changes",
        "B_PASS_TO_PASS_count",
        "v2_tags",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            out = {key: row.get(key, "") for key in fields}
            out["rank"] = index
            out["v2_tags"] = "|".join(row.get("v2_tags", []))
            writer.writerow(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-size", type=int, default=500)
    parser.add_argument("--dataset-quota", action="append", default=[])
    parser.add_argument("--max-repo-share", type=float, default=0.10)
    parser.add_argument("--include-gate-failures", action="store_true")
    parser.add_argument("--seed", type=int, default=20260625)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    quotas = parse_quota(args.dataset_quota, args.target_size)
    repo_cap = max(1, int(args.target_size * args.max_repo_share))
    rows = read_jsonl(Path(args.audit))
    eligible = [
        row for row in rows
        if args.include_gate_failures or bool(row.get("v2_pass_gate"))
    ]

    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_dataset[str(row.get("source_dataset"))].append(row)
    for dataset_rows in by_dataset.values():
        rng.shuffle(dataset_rows)
        dataset_rows.sort(key=row_sort_key)

    selected: list[dict[str, Any]] = []
    repo_counts: Counter[str] = Counter()
    quota_shortfalls: dict[str, dict[str, int]] = {}
    dataset_order = sorted(
        quotas,
        key=lambda dataset: (
            len(by_dataset.get(dataset, [])) / max(quotas[dataset], 1),
            len(by_dataset.get(dataset, [])),
        ),
    )
    for dataset in dataset_order:
        quota = quotas[dataset]
        dataset_selected = select_for_dataset(
            by_dataset.get(dataset, []),
            quota,
            repo_cap,
            repo_counts,
            rng,
        )
        selected.extend(dataset_selected)
        if len(dataset_selected) < quota:
            quota_shortfalls[dataset] = {
                "requested": quota,
                "selected": len(dataset_selected),
                "shortfall": quota - len(dataset_selected),
                "eligible": len(by_dataset.get(dataset, [])),
            }

    if len(selected) < args.target_size:
        selected_ids = {row["case_id"] for row in selected}
        leftovers = [
            row for row in eligible
            if row.get("case_id") not in selected_ids and repo_counts[str(row.get("repo"))] < repo_cap
        ]
        leftovers.sort(key=row_sort_key)
        for row in leftovers:
            if len(selected) >= args.target_size:
                break
            repo = str(row.get("repo"))
            if repo_counts[repo] >= repo_cap:
                continue
            selected.append(row)
            repo_counts[repo] += 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "selected_cases.jsonl", selected)
    write_csv(output_dir / "selected_cases.csv", selected)
    summary = {
        "target_size": args.target_size,
        "selected": len(selected),
        "seed": args.seed,
        "repo_cap": repo_cap,
        "dataset_quotas": quotas,
        "quota_shortfalls": quota_shortfalls,
        "eligible": len(eligible),
        "by_source_dataset": dict(Counter(str(row.get("source_dataset")) for row in selected).most_common()),
        "by_repo_top": dict(Counter(str(row.get("repo")) for row in selected).most_common(30)),
        "by_A_complexity_bin": dict(Counter(str(row.get("A_complexity_bin")) for row in selected).most_common()),
        "by_B_complexity_bin": dict(Counter(str(row.get("B_complexity_bin")) for row in selected).most_common()),
        "by_injection_level": dict(Counter(str(row.get("B_injection_level")) for row in selected).most_common()),
    }
    (output_dir / "selection_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
