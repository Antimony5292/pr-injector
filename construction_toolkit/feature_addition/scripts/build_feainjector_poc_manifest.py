"""Select FeaBench feature-addition cases for modern B-feature POC work."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import read_jsonl, write_jsonl


def index_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("instance_id")): row for row in rows if row.get("instance_id")}


def score(row: dict[str, Any]) -> tuple[int, int, int, int, str]:
    complexity_rank = {
        "xlarge_101_plus": 3,
        "large_31_100": 2,
        "medium_11_30": 1,
        "small_1_10": 0,
    }.get(str(row.get("feature_patch_complexity_bin") or ""), 0)
    return (
        1 if row.get("hard_negative_candidate") else 0,
        complexity_rank,
        int(row.get("pass_to_pass_count") or 0),
        int(row.get("new_component_count") or 0),
        str(row.get("instance_id") or ""),
    )


def select_diverse(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    repo_counts: Counter[str] = Counter()
    for row in sorted(rows, key=score, reverse=True):
        repo = str(row.get("repo") or "")
        if repo_counts[repo] >= 1 and len({item.get("repo") for item in selected}) < limit:
            continue
        selected.append(row)
        repo_counts[repo] += 1
        if len(selected) >= limit:
            return selected
    for row in sorted(rows, key=score, reverse=True):
        if row in selected:
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def poc_row(profile: dict[str, Any], full: dict[str, Any]) -> dict[str, Any]:
    fail_to_pass = full.get("FAIL_TO_PASS") or full.get("fail_to_pass") or []
    pass_to_pass = full.get("PASS_TO_PASS") or full.get("pass_to_pass") or []
    return {
        "instance_id": profile.get("instance_id"),
        "repo": profile.get("repo"),
        "pull_number": profile.get("pull_number"),
        "source_benchmark": "FEA-Bench",
        "task_family": "feature_addition",
        "source_base_commit": full.get("base_commit") or profile.get("base_commit"),
        "modern_target_revision_policy": "repo_default_branch_HEAD_at_construction_time",
        "feature_patch": full.get("patch") or "",
        "feature_test_patch": full.get("test_patch") or "",
        "problem_statement": full.get("problem_statement") or (full.get("problem_info") or {}).get("pr_title") or "",
        "new_components": full.get("new_components") or {},
        "FAIL_TO_PASS": fail_to_pass,
        "PASS_TO_PASS": pass_to_pass,
        "feature_patch_complexity_bin": profile.get("feature_patch_complexity_bin"),
        "feature_patch_line_changes": profile.get("feature_patch_line_changes"),
        "feature_patch_hunks": profile.get("feature_patch_hunks"),
        "feature_source_files": profile.get("feature_source_files"),
        "test_patch_line_changes": profile.get("test_patch_line_changes"),
        "fail_to_pass_count": profile.get("fail_to_pass_count"),
        "pass_to_pass_count": profile.get("pass_to_pass_count"),
        "hard_negative_candidate": profile.get("hard_negative_candidate"),
        "feature_gate_tags": profile.get("feature_gate_tags") or [],
        "construction_contract": {
            "B_feature_missing_state": (
                "Start from modern HEAD without applying the gold feature implementation. "
                "Port only the feature tests and any minimal test fixtures needed for execution."
            ),
            "pass_to_fail_gate": "Feature tests must fail on modern HEAD before implementation.",
            "gold_feature_gate": "Ported gold feature implementation must make feature tests pass.",
            "regression_gate": "PASS_TO_PASS and adjacent tests must remain green before and after gold implementation.",
            "complexity_gate": (
                "Ported implementation should preserve the source feature footprint within the same "
                "complexity bin when feasible; otherwise record drift and reason."
            ),
        },
        "expected_outputs": {
            "B_feature_task_json": "SWE-bench-like feature-addition instance with tests visible as task spec.",
            "gold_feature_patch": "Modern HEAD implementation patch withheld from the solving agent.",
            "verification_record": "pre_feature_fail, post_gold_pass, p2p_clean, p2p_after_gold, complexity_match",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--full-rows", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    profiles = read_jsonl(Path(args.profiles))
    full_index = index_by_id(read_jsonl(Path(args.full_rows)))
    eligible = [
        row
        for row in profiles
        if row.get("feature_gate_pass") and full_index.get(str(row.get("instance_id")))
    ]
    selected_profiles = select_diverse(eligible, args.limit)
    selected = [
        poc_row(profile, full_index[str(profile["instance_id"])])
        for profile in selected_profiles
    ]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "poc_candidates.jsonl", selected)
    summary = {
        "eligible_gate_pass": len(eligible),
        "selected": len(selected),
        "selected_ids": [row["instance_id"] for row in selected],
        "by_repo": dict(Counter(str(row.get("repo")) for row in selected)),
        "by_complexity_bin": dict(Counter(str(row.get("feature_patch_complexity_bin")) for row in selected)),
        "hard_negative_candidates": sum(1 for row in selected if row.get("hard_negative_candidate")),
        "next_step": (
            "For each row, create a modern-HEAD worktree, port feature tests first, "
            "verify fail-before-implementation, then port the gold feature patch and "
            "verify pass-after-implementation plus P2P stability."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
