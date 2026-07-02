"""Build a lightweight FEA-INJECTOR pilot manifest from FEA-Bench Lite IDs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import read_jsonl, write_jsonl


LIGHTWEIGHT_REPOS = {
    "Textualize/rich",
    "RDFLib/rdflib",
    "PyThaiNLP/pythainlp",
    "joke2k/faker",
    "lark-parser/lark",
    "pylint-dev/pylint",
    "mwaskom/seaborn",
    "pallets/flask",
    "pydata/xarray",
    "astropy/astropy",
    "matplotlib/matplotlib",
    "sphinx-doc/sphinx",
}


def parse_feabench_id(instance_id: str) -> dict[str, Any]:
    repo_part, pull_text = instance_id.rsplit("-", 1)
    owner, repo_name = repo_part.split("__", 1)
    return {
        "instance_id": instance_id,
        "repo": f"{owner}/{repo_name}",
        "pull_number": int(pull_text),
    }


def load_current_repos(path: Path | None) -> set[str]:
    if not path or not path.exists():
        return set()
    return {str(row.get("repo") or "") for row in read_jsonl(path) if row.get("repo")}


def priority(row: dict[str, Any], current_repos: set[str]) -> tuple[int, str, int]:
    repo = str(row["repo"])
    if repo in current_repos and repo in LIGHTWEIGHT_REPOS:
        tier = 0
    elif repo in LIGHTWEIGHT_REPOS:
        tier = 1
    elif repo in current_repos:
        tier = 2
    else:
        tier = 3
    return (tier, repo, int(row["pull_number"]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lite-ids", required=True)
    parser.add_argument("--current-selected", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pilot-limit", type=int, default=50)
    args = parser.parse_args()

    lite_ids = json.loads(Path(args.lite_ids).read_text(encoding="utf-8"))
    current_repos = load_current_repos(Path(args.current_selected) if args.current_selected else None)
    rows = [parse_feabench_id(str(instance_id)) for instance_id in lite_ids]
    for row in rows:
        repo = str(row["repo"])
        row["source_benchmark"] = "FEA-Bench-Lite"
        row["task_family"] = "feature_addition"
        row["repo_in_current_prinjector_b"] = repo in current_repos
        row["lightweight_repo_hint"] = repo in LIGHTWEIGHT_REPOS
        row["pilot_priority"] = priority(row, current_repos)[0]
        row["status"] = "needs_full_feabench_scrape"
        row["required_next_fields"] = [
            "base_commit",
            "patch",
            "test_patch",
            "FAIL_TO_PASS",
            "PASS_TO_PASS",
            "problem_statement",
            "new_components",
        ]

    ranked = sorted(rows, key=lambda row: priority(row, current_repos))
    pilot_rows = ranked[: args.pilot_limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "feainjector_lite_all.jsonl", ranked)
    write_jsonl(output_dir / "feainjector_pilot_manifest.jsonl", pilot_rows)
    (output_dir / f"instances_pilot{args.pilot_limit}.json").write_text(
        json.dumps([row["instance_id"] for row in pilot_rows], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary = {
        "lite_ids": len(rows),
        "pilot_limit": args.pilot_limit,
        "pilot_rows": len(pilot_rows),
        "repo_count_all": len({row["repo"] for row in rows}),
        "repo_count_pilot": len({row["repo"] for row in pilot_rows}),
        "all_by_repo_top30": dict(Counter(row["repo"] for row in rows).most_common(30)),
        "pilot_by_repo": dict(Counter(row["repo"] for row in pilot_rows).most_common()),
        "pilot_by_priority": dict(Counter(str(row["pilot_priority"]) for row in pilot_rows)),
        "pilot_overlap_current_prinjector_b": sum(1 for row in pilot_rows if row["repo_in_current_prinjector_b"]),
        "next_step": (
            "Run FEA-Bench get_dataset for these IDs, then create modern "
            "feature-addition B variants with feature tests transplanted and "
            "gold feature implementation withheld."
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
