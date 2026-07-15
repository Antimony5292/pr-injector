"""Build a repo-balanced external feature construction queue."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

try:
    from feainjector_fidelity import implementation_diff
    from prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl
except ImportError:
    from .feainjector_fidelity import implementation_diff
    from .prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl


def tier(row: dict[str, Any], line_changes: int, source_files: int, hunks: int) -> str | None:
    f2p = len(row.get("FAIL_TO_PASS") or [])
    p2p = len(row.get("PASS_TO_PASS") or [])
    if f2p < 1 or source_files < 1 or hunks < 1 or line_changes < 11:
        return None
    if line_changes <= 200 and p2p >= 8:
        return "tier1_strong_surface"
    if line_changes <= 250 and p2p >= 4:
        return "tier2_usable_surface"
    if line_changes <= 300 and p2p >= 1:
        return "tier3_backfill"
    return None


def round_robin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_repo: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        by_repo[str(row.get("repo") or "")].append(row)
    ordered: list[dict[str, Any]] = []
    repos = sorted(by_repo, key=lambda repo: (-len(by_repo[repo]), repo.lower()))
    while repos:
        next_repos: list[str] = []
        for repo in repos:
            ordered.append(by_repo[repo].popleft())
            if by_repo[repo]:
                next_repos.append(repo)
        repos = next_repos
    return ordered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--exclude-repo", action="append", default=[])
    args = parser.parse_args()

    enriched: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    excluded_repos = {repo.lower() for repo in args.exclude_repo}
    for row in read_jsonl(Path(args.input)):
        profile = patch_profile(implementation_diff(str(row.get("feature_patch") or "")))
        queue_tier = tier(row, profile.line_changes, profile.source_files, profile.hunks)
        item = dict(row)
        item["external_queue_profile"] = {
            "scope": "implementation_code_only",
            "line_changes": profile.line_changes,
            "hunks": profile.hunks,
            "source_files": profile.source_files,
            "symbols": profile.symbols,
            "target_tests": len(row.get("FAIL_TO_PASS") or []),
            "regression_tests": len(row.get("PASS_TO_PASS") or []),
        }
        if str(row.get("repo") or "").lower() in excluded_repos:
            item["external_queue_reject_reason"] = "repo excluded after cached-repo preflight failure"
            rejected.append(item)
            continue
        if queue_tier is None:
            item["external_queue_reject_reason"] = "insufficient implementation complexity or regression surface"
            rejected.append(item)
            continue
        item["external_queue_tier"] = queue_tier
        enriched.append(item)

    tiers = ["tier1_strong_surface", "tier2_usable_surface", "tier3_backfill"]
    ordered: list[dict[str, Any]] = []
    for name in tiers:
        candidates = [row for row in enriched if row["external_queue_tier"] == name]
        candidates.sort(
            key=lambda row: (
                -min(int(row["external_queue_profile"]["regression_tests"]), 32),
                -int(row["external_queue_profile"]["line_changes"]),
                str(row.get("instance_id") or ""),
            )
        )
        ordered.extend(round_robin(candidates))

    selected = ordered[: max(0, args.limit)]
    for rank, row in enumerate(selected, start=1):
        row["external_queue_rank"] = rank

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "external_feature_construction_queue.jsonl", selected)
    write_jsonl(output_dir / "external_feature_construction_rejected.jsonl", rejected)
    summary = {
        "input_rows": len(enriched) + len(rejected),
        "eligible_rows": len(enriched),
        "selected_rows": len(selected),
        "rejected_rows": len(rejected),
        "excluded_repos": sorted(args.exclude_repo),
        "selected_repos": len({str(row.get('repo')) for row in selected}),
        "tier_counts": dict(Counter(str(row.get("external_queue_tier")) for row in selected)),
        "top_repo_counts": dict(Counter(str(row.get("repo")) for row in selected).most_common(20)),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
