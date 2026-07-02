"""Merge RepoLaunch setup results back into the PR-INJECTOR coverage matrix."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import read_jsonl, write_jsonl


def load_setup_successes(workspace_root: Path) -> dict[str, dict[str, Any]]:
    setup_path = workspace_root / "setup.jsonl"
    if not setup_path.exists():
        return {}
    return {
        str(row.get("instance_id")): row
        for row in read_jsonl(setup_path)
        if row.get("instance_id")
    }


def load_result_jsons(workspace_root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    playground = workspace_root / "playground"
    if not playground.exists():
        return out
    for result_path in sorted(playground.glob("*/result.json")):
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception as exc:
            out[result_path.parent.name] = {
                "instance_id": result_path.parent.name,
                "completed": False,
                "exception": f"result_json_parse_error: {exc}",
            }
            continue
        iid = str(result.get("instance_id") or result_path.parent.name)
        out[iid] = result
    return out


def classify_quadrant(row: dict[str, Any]) -> str:
    repo_ok = row.get("RepoLaunch_success") is True
    pri_ok = row.get("PRInjector_strict_verified") is True
    if row.get("RepoLaunch_status") == "not_run":
        return "RepoLaunch_not_run"
    if repo_ok and pri_ok:
        return "both_success"
    if repo_ok and not pri_ok:
        return "repolaunch_success_prinjector_failed"
    if not repo_ok and pri_ok:
        return "prinjector_success_repolaunch_failed"
    return "both_failed"


def normalize_failure_reason(raw: str) -> str:
    if not raw:
        return "unknown"
    lowered = raw.lower()
    if "nonetype" in lowered and "send_command" in lowered:
        return "session_not_started_docker_unavailable_observed_in_run_log"
    if "docker is not installed or not running" in lowered:
        return "docker_unavailable"
    if "api connection" in lowered and "bedrock" in lowered:
        return "bedrock_connection_error"
    return raw.splitlines()[0][:240]


def merge_rows(
    matrix_rows: list[dict[str, Any]],
    successes: dict[str, dict[str, Any]],
    results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for row in matrix_rows:
        copied = dict(row)
        iid = str(copied.get("instance_id") or "")
        result = results.get(iid)
        success = successes.get(iid)
        if result is None and success is None:
            copied.setdefault("RepoLaunch_status", "not_run")
        else:
            completed = bool((result or {}).get("completed") or success)
            raw_failure = "" if completed else str((result or {}).get("exception") or "unknown")
            copied["RepoLaunch_status"] = "success" if completed else "failed"
            copied["RepoLaunch_success"] = completed
            copied["RepoLaunch_failure_reason"] = "" if completed else normalize_failure_reason(raw_failure)
            copied["RepoLaunch_raw_exception"] = raw_failure
            copied["RepoLaunch_image"] = str((success or result or {}).get("docker_image") or "")
            copied["RepoLaunch_rebuild_command"] = json.dumps(
                (success or result or {}).get("setup_cmds")
                or (result or {}).get("setup_commands")
                or [],
                ensure_ascii=False,
            )
            copied["RepoLaunch_test_command"] = json.dumps(
                (success or result or {}).get("test_cmds")
                or (result or {}).get("test_commands")
                or [],
                ensure_ascii=False,
            )
        copied["combined_available"] = (
            copied.get("RepoLaunch_success") is True
            and copied.get("PRInjector_strict_verified") is True
        )
        copied["quadrant"] = classify_quadrant(copied)
        merged.append(copied)
    return merged


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    workspace_root = Path(args.workspace_root)
    rows = read_jsonl(Path(args.matrix))
    successes = load_setup_successes(workspace_root)
    results = load_result_jsons(workspace_root)
    merged = merge_rows(rows, successes, results)

    write_jsonl(output_dir / "repolaunch_prinjector_matrix.jsonl", merged)
    write_csv(output_dir / "repolaunch_prinjector_matrix.csv", merged)
    summary = {
        "rows": len(merged),
        "repolaunch_result_jsons": len(results),
        "repolaunch_setup_successes": len(successes),
        "repolaunch_status": dict(Counter(str(row.get("RepoLaunch_status")) for row in merged)),
        "quadrants": dict(Counter(str(row.get("quadrant")) for row in merged)),
        "combined_available": sum(1 for row in merged if row.get("combined_available") is True),
        "prinjector_strict_verified": sum(1 for row in merged if row.get("PRInjector_strict_verified") is True),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
