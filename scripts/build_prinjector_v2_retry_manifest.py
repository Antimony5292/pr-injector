"""Build a retry manifest from PR-INJECTOR v2 fidelity-gate failures.

The manifest is designed for the next construction loop. It translates audit
tags into concrete retry actions and feedback text that can be passed to L3
semantic injection or target/P2P remapping code.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import read_jsonl, write_jsonl


RETRYABLE_TAGS = {
    "localized_simplified",
    "hard_to_easy_collapse",
    "hunk_simplified",
    "file_scope_simplified",
    "low_regression_surface",
    "level2_simplification_risk",
    "l3_needs_feedback_loop",
}


def action_for(row: dict[str, Any]) -> str:
    tags = set(row.get("v2_tags") or [])
    level = str(row.get("B_injection_level") or "")
    if "low_regression_surface" in tags and not tags.intersection({
        "localized_simplified",
        "file_scope_simplified",
        "hard_to_easy_collapse",
    }):
        return "expand_regression_surface"
    if level.startswith("Level_2"):
        return "retry_with_l3_feedback"
    if level.startswith("Level_3"):
        return "retry_l3_with_stronger_fidelity_constraints"
    return "manual_review_or_resample"


def feedback_for(row: dict[str, Any]) -> str:
    tags = set(row.get("v2_tags") or [])
    parts = [
        "Preserve the historical bug semantics while matching the original A-side patch footprint.",
        (
            f"A patch: files={row.get('A_patch_source_files') or row.get('A_patch_files')}, "
            f"hunks={row.get('A_patch_hunks')}, line_changes={row.get('A_patch_line_changes')}, "
            f"symbols={row.get('A_patch_symbols')}."
        ),
        (
            f"Current B patch: files={row.get('B_patch_source_files') or row.get('B_patch_files')}, "
            f"hunks={row.get('B_patch_hunks')}, line_changes={row.get('B_patch_line_changes')}, "
            f"symbols={row.get('B_patch_symbols')}."
        ),
    ]
    if "localized_simplified" in tags or "hard_to_easy_collapse" in tags:
        parts.append(
            "The generated B bug is too localized. Increase the behavioral footprint without adding unrelated noise."
        )
    if "hunk_simplified" in tags:
        parts.append("The B bug uses too few hunks. Preserve the original multi-hunk structure where semantically valid.")
    if "file_scope_simplified" in tags:
        parts.append("The B bug touches too few source files. Preserve cross-file API or call-site effects when present.")
    if "low_regression_surface" in tags:
        parts.append("Expand adjacent/PASS_TO_PASS coverage around touched modules before accepting the case.")
    if "level2_simplification_risk" in tags:
        parts.append("Avoid whole-function Level 2 transplantation if it collapses or drifts from modern APIs.")
    if "l3_needs_feedback_loop" in tags:
        parts.append("Previous L3 output did not pass the fidelity gate; retry with explicit complexity constraints.")
    return " ".join(parts)


def retry_row(row: dict[str, Any]) -> dict[str, Any]:
    tags = sorted(set(row.get("v2_tags") or []))
    return {
        "case_id": row.get("case_id"),
        "source_dataset": row.get("source_dataset"),
        "repo": row.get("repo"),
        "A_instance_id": row.get("A_instance_id"),
        "B_instance_id": row.get("B_instance_id"),
        "previous_injection_level": row.get("B_injection_level"),
        "v2_score": row.get("v2_score"),
        "v2_tags": tags,
        "v2_reasons": row.get("v2_reasons") or [],
        "recommended_action": action_for(row),
        "feedback_prompt": feedback_for(row),
        "target_profile": {
            "files": row.get("A_patch_source_files") or row.get("A_patch_files"),
            "hunks": row.get("A_patch_hunks"),
            "line_changes": row.get("A_patch_line_changes"),
            "symbols": row.get("A_patch_symbols"),
            "fail_to_pass_count": row.get("A_FAIL_TO_PASS_count"),
            "pass_to_pass_count": row.get("A_PASS_TO_PASS_count"),
        },
        "current_b_profile": {
            "files": row.get("B_patch_source_files") or row.get("B_patch_files"),
            "hunks": row.get("B_patch_hunks"),
            "line_changes": row.get("B_patch_line_changes"),
            "symbols": row.get("B_patch_symbols"),
            "fail_to_pass_count": row.get("B_FAIL_TO_PASS_count"),
            "pass_to_pass_count": row.get("B_PASS_TO_PASS_count"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--retryable-only", action="store_true")
    args = parser.parse_args()

    rows = read_jsonl(Path(args.audit))
    failed = [row for row in rows if not row.get("v2_pass_gate")]
    retryable = [
        row for row in failed
        if not args.retryable_only or set(row.get("v2_tags") or []).intersection(RETRYABLE_TAGS)
    ]
    manifest = [retry_row(row) for row in retryable]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "v2_retry_manifest.jsonl", manifest)
    summary = {
        "failed_gate_rows": len(failed),
        "retry_manifest_rows": len(manifest),
        "by_action": dict(Counter(row["recommended_action"] for row in manifest).most_common()),
        "by_tag": dict(Counter(tag for row in manifest for tag in row["v2_tags"]).most_common()),
        "by_dataset": dict(Counter(str(row["source_dataset"]) for row in manifest).most_common()),
        "by_repo_top": dict(Counter(str(row["repo"]) for row in manifest).most_common(30)),
    }
    (output_dir / "retry_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
