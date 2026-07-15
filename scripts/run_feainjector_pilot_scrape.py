"""Scrape a small FeaBench pilot subset without running the full 1,401-task job."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from datasets import load_dataset


ROOT = Path(__file__).resolve().parent.parent
FEABENCH_ROOT = ROOT / ".external" / "FEA-Bench"


def ensure_github_token() -> None:
    if os.environ.get("GITHUB_TOKEN"):
        return
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception as exc:
        raise SystemExit(
            "GITHUB_TOKEN is not set and `gh auth token` failed. "
            "FeaBench PR scraping requires a GitHub token."
        ) from exc
    if not token:
        raise SystemExit("GitHub token lookup returned empty output.")
    os.environ["GITHUB_TOKEN"] = token


def load_existing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                row = json.loads(line)
                out[str(row["instance_id"])] = row
    return out


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot-ids", required=True)
    parser.add_argument("--dataset", default="microsoft/FEA-Bench")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()

    ensure_github_token()
    sys.path.insert(0, str(FEABENCH_ROOT))

    from feabench.get_dataset import (  # type: ignore
        clone_or_update_repo,
        get_file_related_info,
        get_source_data,
        save_dataset,
        save_instance_to_file,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    medium_file = output_dir / "FEA-INJECTOR-pilot-medium.jsonl"
    error_file = output_dir / "scrape_errors.jsonl"
    standard_path = output_dir / "FEA-INJECTOR-pilot-Standard"
    oracle_path = output_dir / "FEA-INJECTOR-pilot-Oracle"
    lite_standard_path = output_dir / "FEA-INJECTOR-pilot-Lite-Standard"
    lite_oracle_path = output_dir / "FEA-INJECTOR-pilot-Lite-Oracle"
    testbed = output_dir / "testbed"
    testbed.mkdir(parents=True, exist_ok=True)

    requested_ids = json.loads(Path(args.pilot_ids).read_text(encoding="utf-8"))
    selected_ids = set(str(instance_id) for instance_id in requested_ids[: args.limit])
    selected_ids_path = output_dir / f"instances_smoke{args.limit}.json"
    selected_ids_path.write_text(
        json.dumps(sorted(selected_ids), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    existing = load_existing(medium_file)
    dataset = load_dataset(args.dataset)["test"]
    source_instances: list[dict[str, Any]] = []
    processed: list[str] = []
    failed: list[str] = []
    for item in dataset:
        instance_id = str(item["instance_id"])
        if instance_id not in selected_ids:
            continue
        if instance_id in existing:
            instance = existing[instance_id]
        else:
            try:
                repo_folder = str(item["repo"]).replace("/", "__")
                repo_testbed = testbed / repo_folder
                clone_or_update_repo(str(item["repo"]), str(repo_testbed))
                instance = get_source_data(item)
                get_file_related_info(instance, testbed_dir=repo_testbed)
                instance["version"] = item["version"]
                instance["FAIL_TO_PASS"] = item["FAIL_TO_PASS"]
                instance["PASS_TO_PASS"] = item["PASS_TO_PASS"]
                instance["environment_setup_commit"] = item["environment_setup_commit"]
                save_instance_to_file(instance, str(medium_file))
            except Exception as exc:
                failed.append(instance_id)
                append_jsonl(
                    error_file,
                    {
                        "instance_id": instance_id,
                        "repo": item.get("repo"),
                        "error": str(exc),
                    },
                )
                print(
                    f"[FEA-INJECTOR] failed {instance_id} ({item.get('repo')}): {exc}",
                    flush=True,
                )
                continue
        source_instances.append(instance)
        processed.append(instance_id)

    if not source_instances:
        raise SystemExit("No requested pilot IDs were found in the FeaBench dataset.")

    save_dataset(
        instances=source_instances,
        instruction_data_save_dir=str(oracle_path),
        standard_data_save_dir=str(standard_path),
        lite_instruction_data_save_dir=str(lite_oracle_path),
        lite_standard_data_save_dir=str(lite_standard_path),
        lite_ids=sorted(selected_ids),
    )

    summary = {
        "dataset": args.dataset,
        "requested": len(selected_ids),
        "processed": len(processed),
        "failed": len(failed),
        "processed_ids": processed,
        "failed_ids": failed,
        "medium_file": str(medium_file),
        "error_file": str(error_file),
        "standard_dataset": str(standard_path),
        "oracle_dataset": str(oracle_path),
        "lite_ids_file": str(selected_ids_path),
    }
    (output_dir / "scrape_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
