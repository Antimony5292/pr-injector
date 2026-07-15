#!/usr/bin/env python3
"""Build per-case P2P manifests for the final Feature70 fidelity rescue."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def index(path: Path) -> dict[str, dict[str, Any]]:
    return {str(row.get("instance_id")): row for row in read_jsonl(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--strict-cases", type=Path, required=True)
    parser.add_argument("--previous-results", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--instance-id", action="append", required=True)
    parser.add_argument("--min-regression-ratio", type=float, default=0.25)
    args = parser.parse_args()

    source = index(args.source_manifest)
    strict = index(args.strict_cases)
    previous = index(args.previous_results)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    for instance_id in args.instance_id:
        source_row = source[instance_id]
        row = dict(strict[instance_id])
        source_p2p = list(source_row.get("PASS_TO_PASS") or source_row.get("pass_to_pass") or [])
        required = math.ceil(len(source_p2p) * args.min_regression_ratio)
        previous_p2p = list((previous.get(instance_id) or {}).get("pass_to_pass") or [])

        # PyTeal's collect-only output expands parameterized IDs into strings
        # that pytest cannot replay verbatim. Preserve the source IDs there.
        if instance_id == "algorand__pyteal-514":
            selected_p2p = source_p2p[:required]
            selection = "source_nodeids_avoid_parameter_expansion"
        elif instance_id == "amaranth-lang__amaranth-1321":
            # Modern enum formatting also flows through this operator test.
            # It is part of the feature target surface, not protected P2P.
            evolved_target = "tests/test_hdl_ast.py::OperatorTestCase::test_matches_enum"
            feature_tests = list(row.get("feature_tests") or [])
            if evolved_target not in feature_tests:
                feature_tests.append(evolved_target)
            row["feature_tests"] = feature_tests
            # Ask for two spare nodeids because modern collection can drop
            # historical aliases; the final gate still uses actual collected tests.
            selected_p2p = [test for test in source_p2p if test != evolved_target][: required + 2]
            selection = "source_nodeid_prefix_excluding_evolved_feature_target"
        elif len(previous_p2p) >= required:
            selected_p2p = previous_p2p[:required]
            selection = "modern_collectable_prefix"
        else:
            selected_p2p = source_p2p[:required]
            selection = "source_nodeid_prefix"

        row["pass_to_pass"] = selected_p2p
        row["fidelity_remediation"] = {
            "reason": "targeted_final_feature70_retry",
            "source_p2p_count": len(source_p2p),
            "required_modern_p2p_count": required,
            "selected_p2p_count": len(selected_p2p),
            "selection": selection,
            "case_isolated_venv_required": True,
        }
        case_dir = args.output_dir / instance_id
        case_dir.mkdir(parents=True, exist_ok=True)
        manifest = case_dir / "manifest.jsonl"
        manifest.write_text(json.dumps(row, sort_keys=True) + "\n")
        summary.append(
            {
                "instance_id": instance_id,
                "required_modern_p2p_count": required,
                "selected_p2p_count": len(selected_p2p),
                "selection": selection,
                "manifest": str(manifest),
            }
        )

    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
