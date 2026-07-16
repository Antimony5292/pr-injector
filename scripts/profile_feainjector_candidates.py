"""Profile FeaBench feature-addition candidates for FEA-INJECTOR pilots."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

try:
    from prinjector_v2_metrics import complexity_bin, count_items, patch_profile, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import complexity_bin, count_items, patch_profile, write_jsonl


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def component_count(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, dict):
        return sum(component_count(item) for item in value.values())
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, dict) and "components" in item:
                total += component_count(item.get("components"))
            else:
                total += 1
        return total
    return 1


def component_file_count(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, dict):
        return sum(1 for components in value.values() if component_count(components) > 0)
    if isinstance(value, list):
        return sum(
            1
            for item in value
            if isinstance(item, dict) and component_count(item.get("components")) > 0
        )
    return 0


def gate_candidate(row: dict[str, Any], source_profile: dict[str, Any], test_profile: dict[str, Any]) -> dict[str, Any]:
    fail_to_pass_count = count_items(row.get("FAIL_TO_PASS") or row.get("fail_to_pass"))
    pass_to_pass_count = count_items(row.get("PASS_TO_PASS") or row.get("pass_to_pass"))
    new_component_count = component_count(row.get("new_components"))
    tags: set[str] = set()
    reasons: list[str] = []

    if source_profile["source_files"] <= 0:
        tags.add("no_source_patch")
        reasons.append("gold feature patch has no source-code file")
    if source_profile["line_changes"] < 5:
        tags.add("tiny_feature_patch")
        reasons.append("gold feature patch is too small for a feature-addition pilot")
    if test_profile["test_files"] <= 0:
        tags.add("missing_feature_tests")
        reasons.append("test patch does not add or modify test files")
    if fail_to_pass_count <= 0:
        tags.add("missing_fail_to_pass")
        reasons.append("candidate has no FAIL_TO_PASS tests")
    if pass_to_pass_count < 5:
        tags.add("low_regression_surface")
        reasons.append("candidate has too few PASS_TO_PASS tests for regression-sensitive evaluation")
    if new_component_count <= 0:
        tags.add("no_new_component_signal")
        reasons.append("FeaBench metadata does not identify a new function/class/component")

    pass_gate = not tags.intersection(
        {
            "no_source_patch",
            "tiny_feature_patch",
            "missing_feature_tests",
            "missing_fail_to_pass",
            "low_regression_surface",
        }
    )
    if pass_gate and not tags:
        tags.add("feature_pilot_ready")
    elif pass_gate:
        tags.add("feature_pilot_ready_with_metadata_warning")

    return {
        "pass_gate": pass_gate,
        "tags": sorted(tags),
        "reasons": reasons,
        "fail_to_pass_count": fail_to_pass_count,
        "pass_to_pass_count": pass_to_pass_count,
        "new_component_count": new_component_count,
        "new_component_file_count": component_file_count(row.get("new_components")),
        "hard_negative_candidate": pass_to_pass_count >= 20 and test_profile["line_changes"] >= 10,
    }


def profiled_row(row: dict[str, Any]) -> dict[str, Any]:
    source_profile_obj = patch_profile(str(row.get("patch") or ""))
    test_profile_obj = patch_profile(str(row.get("test_patch") or ""))
    source_profile = asdict(source_profile_obj)
    test_profile = asdict(test_profile_obj)
    gate = gate_candidate(row, source_profile, test_profile)
    out = {
        "instance_id": row.get("instance_id"),
        "repo": row.get("repo"),
        "pull_number": row.get("pull_number"),
        "base_commit": row.get("base_commit"),
        "version": row.get("version"),
        "feature_source_files": source_profile["source_files"],
        "feature_test_files_in_gold_patch": source_profile["test_files"],
        "feature_patch_files": source_profile["files"],
        "feature_patch_hunks": source_profile["hunks"],
        "feature_patch_added": source_profile["added"],
        "feature_patch_removed": source_profile["removed"],
        "feature_patch_line_changes": source_profile["line_changes"],
        "feature_patch_symbols": source_profile["symbols"],
        "feature_patch_complexity_bin": complexity_bin(source_profile["line_changes"]),
        "test_patch_files": test_profile["files"],
        "test_patch_test_files": test_profile["test_files"],
        "test_patch_hunks": test_profile["hunks"],
        "test_patch_added": test_profile["added"],
        "test_patch_removed": test_profile["removed"],
        "test_patch_line_changes": test_profile["line_changes"],
        "fail_to_pass_count": gate["fail_to_pass_count"],
        "pass_to_pass_count": gate["pass_to_pass_count"],
        "new_component_count": gate["new_component_count"],
        "new_component_file_count": gate["new_component_file_count"],
        "feature_gate_pass": gate["pass_gate"],
        "feature_gate_tags": gate["tags"],
        "feature_gate_reasons": gate["reasons"],
        "hard_negative_candidate": gate["hard_negative_candidate"],
        "source_patch_profile": source_profile,
        "test_patch_profile": test_profile,
    }
    return out


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    materialized = list(rows)
    if not materialized:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in materialized:
        for key in row:
            if key not in keys and not isinstance(row[key], (dict, list)):
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in keys} for row in materialized)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-jsonl", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    rows = [profiled_row(row) for row in read_jsonl(Path(args.input_jsonl))]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "feainjector_candidate_profiles.jsonl", rows)
    write_csv(output_dir / "feainjector_candidate_profiles.csv", rows)
    summary = {
        "rows": len(rows),
        "gate_pass": sum(1 for row in rows if row["feature_gate_pass"]),
        "hard_negative_candidates": sum(1 for row in rows if row["hard_negative_candidate"]),
        "by_repo": dict(Counter(str(row.get("repo")) for row in rows).most_common(30)),
        "by_complexity_bin": dict(Counter(str(row.get("feature_patch_complexity_bin")) for row in rows)),
        "gate_tags": dict(Counter(tag for row in rows for tag in row.get("feature_gate_tags", []))),
        "next_gate_for_modern_B": (
            "For each modern B feature variant: transplanted feature tests must fail before the "
            "gold/ported implementation, pass after it, keep PASS_TO_PASS green, and match this "
            "source feature patch profile by files/hunks/line changes/new components."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
