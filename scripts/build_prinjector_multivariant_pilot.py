"""Build a small same-issue multiple-B-variants pilot manifest."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import read_jsonl, write_jsonl


VARIANTS = {
    "l1_clean_revert": "Prefer a direct clean-revert style injected bug if it remains faithful and passes the v2 gate.",
    "l2_ast_surgery": "Skip clean revert and prefer AST/hunk surgery that preserves the historical bug semantics in modern code.",
    "l3_semantic": "Use semantic L3 generation to preserve the historical bug behavior while allowing modern-code implementation differences.",
    "l3_expanded_p2p": "Use semantic L3 generation and preserve or expand adjacent/P2P regression surface where semantically valid.",
}


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("A_instance_id") or row.get("instance_id") or "")


def load_candidate_index(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in out:
                out[iid] = row
    return out


def choose_pilot_rows(rows: list[dict[str, Any]], limit: int, excluded_repos: set[str]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("repo") or "") in excluded_repos:
            continue
        buckets[(str(row.get("source_dataset") or ""), str(row.get("injection_level") or ""))].append(row)
    for key in buckets:
        buckets[key].sort(key=lambda row: (str(row.get("repo") or ""), row_id(row)))
    selected: list[dict[str, Any]] = []
    keys = sorted(buckets, key=lambda key: (key[0], key[1]))
    while keys and len(selected) < limit:
        next_keys = []
        used_repos = Counter(str(row.get("repo") or "") for row in selected)
        for key in keys:
            candidates = buckets[key]
            if not candidates:
                continue
            candidates.sort(key=lambda row: (used_repos[str(row.get("repo") or "")], str(row.get("repo") or ""), row_id(row)))
            selected.append(candidates.pop(0))
            if len(selected) >= limit:
                break
            if candidates:
                next_keys.append(key)
        keys = next_keys
    return selected


def variant_row(row: dict[str, Any], source_candidate: dict[str, Any], variant: str) -> dict[str, Any]:
    source_id = row_id(row)
    out = dict(source_candidate or row)
    out["source_instance_id"] = source_id
    out["instance_id"] = f"{source_id}__variant__{variant}"
    out["v2_variant_source_instance_id"] = source_id
    out["v2_variant_mode"] = variant
    out["v2_variant_prompt"] = VARIANTS[variant]
    out["v2_retry_feedback_prompt"] = (
        f"Variant mode: {variant}. {VARIANTS[variant]} "
        "Keep the same historical issue semantics, but produce this variant's requested surface form."
    )
    if variant == "l3_expanded_p2p":
        p2p = (
            source_candidate.get("pass_to_pass")
            or source_candidate.get("PASS_TO_PASS")
            or row.get("pass_to_pass")
            or row.get("PASS_TO_PASS")
            or []
        )
        out["pass_to_pass"] = p2p
        out["PASS_TO_PASS"] = p2p
        out["v2_variant_expanded_p2p_requested"] = True
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected", required=True)
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--exclude-repo", action="append", default=[])
    args = parser.parse_args()

    selected_rows = read_jsonl(Path(args.selected))
    candidates = load_candidate_index([Path(path) for path in args.candidate_file])
    pilot = choose_pilot_rows(selected_rows, args.limit, set(args.exclude_repo))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "pilot_source_cases.jsonl", pilot)
    for variant in VARIANTS:
        rows = [
            variant_row(row, candidates.get(row_id(row), row), variant)
            for row in pilot
        ]
        write_jsonl(output_dir / f"candidate_pool_{variant}.jsonl", rows)
    summary = {
        "pilot_cases": len(pilot),
        "variants": list(VARIANTS),
        "total_variant_instances": len(pilot) * len(VARIANTS),
        "by_dataset": dict(Counter(str(row.get("source_dataset") or "") for row in pilot)),
        "by_repo": dict(Counter(str(row.get("repo") or "") for row in pilot).most_common()),
        "by_existing_injection_level": dict(Counter(str(row.get("injection_level") or "") for row in pilot)),
        "excluded_repos": sorted(set(args.exclude_repo)),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
