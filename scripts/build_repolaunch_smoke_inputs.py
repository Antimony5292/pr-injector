"""Build a small RepoLaunch dataset/config from the coverage matrix."""

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


PREFERRED_REPOS = [
    "psf/requests",
    "mwaskom/seaborn",
    "pytest-dev/pytest",
    "sphinx-doc/sphinx",
    "pydata/xarray",
    "pylint-dev/pylint",
    "astropy/astropy",
    "django/django",
    "matplotlib/matplotlib",
    "sympy/sympy",
    "ansible/ansible",
    "qutebrowser/qutebrowser",
]


def _row_priority(row: dict[str, Any]) -> tuple[int, int, str]:
    dataset = str(row.get("source_dataset") or "")
    repo = str(row.get("repo") or "")
    if row.get("PRInjector_strict_verified"):
        injector_rank = 0
    elif row.get("PRInjector_injection_success"):
        injector_rank = 1
    elif row.get("PRInjector_attempted"):
        injector_rank = 2
    else:
        injector_rank = 3
    dataset_rank = {
        "princeton-nlp/SWE-bench_Verified": 0,
        "ScaleAI/SWE-bench_Pro": 1,
        "princeton-nlp/SWE-bench": 2,
    }.get(dataset, 3)
    return (injector_rank, dataset_rank, repo)


def select_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    valid = [
        row
        for row in rows
        if row.get("instance_id") and row.get("repo") and row.get("base_commit")
    ]
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(valid, key=_row_priority):
        by_repo.setdefault(str(row["repo"]), []).append(row)

    selected: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for repo in PREFERRED_REPOS:
        if repo not in by_repo:
            continue
        row = by_repo[repo][0]
        selected.append(row)
        seen_ids.add(str(row["instance_id"]))
        if len(selected) >= limit:
            return selected

    for row in sorted(valid, key=lambda item: (_row_priority(item), str(item["instance_id"]))):
        iid = str(row["instance_id"])
        if iid in seen_ids:
            continue
        selected.append(row)
        seen_ids.add(iid)
        if len(selected) >= limit:
            return selected
    return selected


def to_repolaunch_instance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "language": "python",
        "created_at": "2026-06-28",
        "hints": (
            "Set up this Python repository and find a reliable command that can "
            "verify the repository/test environment. Do not assume PR-INJECTOR "
            "patches or hidden benchmark tests are available."
        ),
        "source_dataset": row.get("source_dataset", ""),
        "prinjector_failure_bucket": row.get("PRInjector_failure_bucket", ""),
        "prinjector_strict_verified": row.get("PRInjector_strict_verified", False),
    }


def build_config(args: argparse.Namespace, dataset_path: Path) -> dict[str, Any]:
    return {
        "mode": {
            "setup": True,
            "organize": args.organize,
        },
        "model_config": {
            "model": args.model,
            "temperature": 0.0,
            "max_tokens": args.max_tokens,
        },
        "workspace_root": str(Path(args.workspace_root).resolve()),
        "dataset": str(dataset_path.resolve()),
        "print_to_console": True,
        "first_N_repos": -1,
        "overwrite": args.overwrite,
        "max_workers": args.workers,
        "os": "linux",
        "max_trials": args.max_trials,
        "max_steps_setup": args.max_steps_setup,
        "max_steps_verify": args.max_steps_verify,
        "max_steps_organize": args.max_steps_organize,
        "cmd_timeout": args.cmd_timeout,
        "image_prefix": args.image_prefix,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--model", default="bedrock/global.anthropic.claude-sonnet-4-6")
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--max-trials", type=int, default=1)
    parser.add_argument("--max-steps-setup", type=int, default=12)
    parser.add_argument("--max-steps-verify", type=int, default=8)
    parser.add_argument("--max-steps-organize", type=int, default=12)
    parser.add_argument("--cmd-timeout", type=int, default=10)
    parser.add_argument("--image-prefix", default="repolaunch/prinjector")
    parser.add_argument("--organize", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    Path(args.workspace_root).mkdir(parents=True, exist_ok=True)

    rows = read_jsonl(Path(args.matrix))
    selected = select_rows(rows, args.limit)
    dataset = [to_repolaunch_instance(row) for row in selected]
    dataset_path = output_dir / "dataset.jsonl"
    write_jsonl(dataset_path, dataset)

    config = build_config(args, dataset_path)
    config_path = output_dir / "config.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {
        "matrix": args.matrix,
        "dataset": str(dataset_path),
        "config": str(config_path),
        "workspace_root": str(Path(args.workspace_root).resolve()),
        "limit": args.limit,
        "selected": len(dataset),
        "selected_by_repo": dict(Counter(item["repo"] for item in dataset)),
        "selected_by_dataset": dict(Counter(item.get("source_dataset", "") for item in dataset)),
        "model": args.model,
        "organize": args.organize,
    }
    (output_dir / "input_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
