"""Build a PR-INJECTOR v2 complexity/fidelity audit for paired A/B cases.

This script is a post-processing audit. It does not modify benchmark assets or
run agents. Its output is meant to drive the next construction iteration:

  - per-case A/B complexity profiles
  - v2 fidelity gate pass/fail labels
  - repo/dataset/level distributions
  - accepted/rejected case lists for stratified re-sampling
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import (
    FidelityGateConfig,
    complexity_bin,
    evaluate_fidelity,
    patch_profile,
    read_jsonl,
    resolve_text,
    summarize_counter,
    write_jsonl,
)


def list_value(row: dict[str, Any], *names: str) -> list[Any]:
    for name in names:
        value = row.get(name)
        if isinstance(value, list):
            return value
    return []


def scalar(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p25": None, "median": None, "p75": None, "max": None}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p25": ordered[int((len(ordered) - 1) * 0.25)],
        "median": statistics.median(ordered),
        "p75": ordered[int((len(ordered) - 1) * 0.75)],
        "max": ordered[-1],
    }


def build_case_row(pair: dict[str, Any], final_dir: Path, config: FidelityGateConfig) -> dict[str, Any]:
    a_patch = resolve_text(scalar(pair, "A_patch", "B_historical_source_patch"), final_dir)
    b_patch = resolve_text(scalar(pair, "B_injected_diff"), final_dir)
    if not b_patch:
        b_patch = resolve_text(scalar(pair, "B_golden_patch"), final_dir)

    a_profile = patch_profile(a_patch)
    b_profile = patch_profile(b_patch)
    a_ftp = list_value(pair, "A_FAIL_TO_PASS", "fail_to_pass", "FAIL_TO_PASS")
    b_ftp = list_value(pair, "B_FAIL_TO_PASS", "FAIL_TO_PASS")
    a_ptp = list_value(pair, "A_PASS_TO_PASS", "pass_to_pass", "PASS_TO_PASS")
    b_ptp = list_value(pair, "B_PASS_TO_PASS_CLEAN", "B_PASS_TO_PASS", "PASS_TO_PASS")
    legacy_tags = list_value(pair, "B_fidelity_tags")
    injection_level = str(scalar(pair, "B_injection_level", "injection_level"))

    gate = evaluate_fidelity(
        a_profile=a_profile,
        b_profile=b_profile,
        a_fail_to_pass_count=len(a_ftp),
        b_fail_to_pass_count=len(b_ftp),
        a_pass_to_pass_count=len(a_ptp),
        b_pass_to_pass_count=len(b_ptp),
        injection_level=injection_level,
        existing_tags=[],
        config=config,
    )

    row: dict[str, Any] = {
        "case_id": scalar(pair, "case_id"),
        "source_dataset": scalar(pair, "source_dataset", "A_dataset"),
        "repo": scalar(pair, "repo", "A_repo", "B_repo"),
        "A_instance_id": scalar(pair, "A_instance_id", "B_source_instance_id"),
        "B_instance_id": scalar(pair, "B_instance_id"),
        "B_injection_level": injection_level,
        "A_FAIL_TO_PASS_count": len(a_ftp),
        "B_FAIL_TO_PASS_count": len(b_ftp),
        "A_PASS_TO_PASS_count": len(a_ptp),
        "B_PASS_TO_PASS_count": len(b_ptp),
        "A_complexity_bin": complexity_bin(a_profile.line_changes),
        "B_complexity_bin": complexity_bin(b_profile.line_changes),
        "v2_score": gate["score"],
        "v2_pass_gate": gate["pass_gate"],
        "v2_tags": gate["tags"],
        "v2_reasons": gate["reasons"],
        "v2_ratios": gate["ratios"],
        "legacy_fidelity_tags": legacy_tags,
        "legacy_fidelity_reasons": list_value(pair, "B_fidelity_reasons"),
    }
    row.update(a_profile.flat("A_patch"))
    row.update(b_profile.flat("B_patch"))
    return row


def summarize(rows: list[dict[str, Any]], config: FidelityGateConfig) -> dict[str, Any]:
    tags = Counter(tag for row in rows for tag in row["v2_tags"])
    repos = Counter(str(row["repo"]) for row in rows)
    datasets = Counter(str(row["source_dataset"]) for row in rows)
    levels = Counter(str(row["B_injection_level"]) for row in rows)
    pass_rows = [row for row in rows if row["v2_pass_gate"]]
    fail_rows = [row for row in rows if not row["v2_pass_gate"]]
    repo_share = {repo: count / len(rows) for repo, count in repos.items()} if rows else {}
    repo_over_cap = {
        repo: {"count": repos[repo], "share": round(share, 4)}
        for repo, share in sorted(repo_share.items(), key=lambda item: item[1], reverse=True)
        if share > config.max_repo_share
    }

    by_dataset_level: dict[str, dict[str, int]] = defaultdict(dict)
    for (dataset, level), count in Counter((row["source_dataset"], row["B_injection_level"]) for row in rows).items():
        by_dataset_level[str(dataset)][str(level)] = count

    return {
        "total": len(rows),
        "v2_gate_pass": len(pass_rows),
        "v2_gate_fail": len(fail_rows),
        "v2_gate_pass_rate": round(len(pass_rows) / len(rows), 4) if rows else 0,
        "config": config.__dict__,
        "by_source_dataset": summarize_counter(datasets),
        "by_repo_top": summarize_counter(repos, top=30),
        "repo_over_cap": repo_over_cap,
        "by_injection_level": summarize_counter(levels),
        "by_dataset_level": by_dataset_level,
        "tag_counts": summarize_counter(tags),
        "score_quantiles": quantiles([float(row["v2_score"]) for row in rows]),
        "A_line_change_quantiles": quantiles([float(row["A_patch_line_changes"]) for row in rows]),
        "B_line_change_quantiles": quantiles([float(row["B_patch_line_changes"]) for row in rows]),
        "line_ratio_quantiles": quantiles([
            float(row["v2_ratios"]["line_changes"])
            for row in rows
            if isinstance(row["v2_ratios"]["line_changes"], (int, float))
        ]),
        "gate_fail_by_repo_top": summarize_counter(Counter(str(row["repo"]) for row in fail_rows), top=30),
        "gate_fail_by_dataset": summarize_counter(Counter(str(row["source_dataset"]) for row in fail_rows)),
        "gate_fail_by_level": summarize_counter(Counter(str(row["B_injection_level"]) for row in fail_rows)),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "case_id",
        "source_dataset",
        "repo",
        "A_instance_id",
        "B_instance_id",
        "B_injection_level",
        "v2_score",
        "v2_pass_gate",
        "v2_tags",
        "A_patch_files",
        "B_patch_files",
        "A_patch_hunks",
        "B_patch_hunks",
        "A_patch_line_changes",
        "B_patch_line_changes",
        "A_FAIL_TO_PASS_count",
        "B_FAIL_TO_PASS_count",
        "A_PASS_TO_PASS_count",
        "B_PASS_TO_PASS_count",
        "A_complexity_bin",
        "B_complexity_bin",
        "v2_ratios",
        "v2_reasons",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out = {key: row.get(key, "") for key in fields}
            out["v2_tags"] = "|".join(row.get("v2_tags", []))
            out["v2_reasons"] = "|".join(row.get("v2_reasons", []))
            out["v2_ratios"] = json.dumps(row.get("v2_ratios", {}), ensure_ascii=False)
            writer.writerow(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--final-dir", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--min-score", type=float, default=0.65)
    parser.add_argument("--min-line-ratio", type=float, default=0.50)
    parser.add_argument("--max-line-ratio", type=float, default=2.50)
    parser.add_argument("--min-hunk-ratio", type=float, default=0.50)
    parser.add_argument("--min-file-ratio", type=float, default=0.50)
    parser.add_argument("--min-regression-ratio", type=float, default=0.25)
    parser.add_argument("--max-repo-share", type=float, default=0.20)
    args = parser.parse_args()

    pairing = Path(args.pairing).resolve()
    final_dir = Path(args.final_dir).resolve() if args.final_dir else pairing.parent
    output_dir = Path(args.output_dir).resolve()
    config = FidelityGateConfig(
        min_score=args.min_score,
        min_line_ratio=args.min_line_ratio,
        max_line_ratio=args.max_line_ratio,
        min_hunk_ratio=args.min_hunk_ratio,
        min_file_ratio=args.min_file_ratio,
        min_regression_ratio=args.min_regression_ratio,
        max_repo_share=args.max_repo_share,
    )

    pairs = read_jsonl(pairing)
    rows = [build_case_row(pair, final_dir, config) for pair in pairs]
    accepted = [row for row in rows if row["v2_pass_gate"]]
    rejected = [row for row in rows if not row["v2_pass_gate"]]
    summary = summarize(rows, config)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "v2_case_audit.jsonl", rows)
    write_jsonl(output_dir / "v2_gate_accepted.jsonl", accepted)
    write_jsonl(output_dir / "v2_gate_rejected.jsonl", rejected)
    write_csv(output_dir / "v2_case_audit.csv", rows)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
