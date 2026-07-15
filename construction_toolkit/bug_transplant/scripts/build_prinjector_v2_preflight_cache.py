"""Build a reusable PR-INJECTOR v2 preflight cache from prior construction runs."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import read_jsonl, write_jsonl


def row_id(row: dict[str, Any]) -> str:
    return str(
        row.get("source_instance_id")
        or row.get("A_instance_id")
        or row.get("B_source_instance_id")
        or row.get("instance_id")
        or ""
    )


def dataset_of(row: dict[str, Any]) -> str:
    return str(row.get("source_dataset") or row.get("A_dataset") or "")


def repo_of(row: dict[str, Any]) -> str:
    return str(row.get("repo") or "")


def result_paths(run_dir: Path) -> list[Path]:
    if not run_dir.exists():
        return []
    if run_dir.is_file():
        return [run_dir] if run_dir.name.endswith("injection_results.jsonl") else []
    return sorted(run_dir.rglob("verified_injection_results.jsonl"))


def preflight_status(row: dict[str, Any]) -> tuple[str, str]:
    preflight = row.get("preflight") or {}
    failure = str(row.get("failure_reason") or "")
    level = str(row.get("injection_level") or "")
    if preflight.get("ok") is True:
        return "preflight_ok", str(preflight.get("reason") or "ok")
    if level == "Preflight_Failed" or failure.startswith("preflight_failed:"):
        reason = str(preflight.get("reason") or failure.removeprefix("preflight_failed:").strip() or "unknown")
        return "preflight_failed", reason
    if row.get("success") is True:
        return "preflight_ok_or_not_required", "injection_succeeded"
    return "preflight_unknown", "not_recorded"


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter()
    reasons = Counter()
    sources: list[str] = []
    sample = rows[-1]
    for row in rows:
        status, reason = preflight_status(row)
        statuses[status] += 1
        reasons[reason] += 1
        if row.get("_source_file"):
            sources.append(str(row["_source_file"]))
    if statuses.get("preflight_ok") or statuses.get("preflight_ok_or_not_required"):
        final_status = "preflight_ok"
    elif statuses.get("preflight_failed"):
        final_status = "preflight_failed"
    else:
        final_status = "preflight_unknown"
    return {
        "source_instance_id": row_id(sample),
        "repo": repo_of(sample),
        "source_dataset": dataset_of(sample),
        "preflight_cache_status": final_status,
        "preflight_status_counts": dict(statuses),
        "preflight_reason_counts": dict(reasons),
        "last_injection_level": sample.get("injection_level"),
        "last_success": bool(sample.get("success")),
        "attempt_count": len(rows),
        "source_files": sorted(set(sources)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    input_files: list[str] = []
    for run_dir in [Path(path) for path in args.run_dir]:
        for path in result_paths(run_dir):
            input_files.append(str(path))
            for row in read_jsonl(path):
                iid = row_id(row)
                if not iid:
                    continue
                copied = dict(row)
                copied["_source_file"] = str(path)
                grouped[iid].append(copied)

    rows = [aggregate(grouped[iid]) for iid in sorted(grouped)]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "preflight_cache.jsonl"
    write_jsonl(cache_path, rows)
    summary = {
        "rows": len(rows),
        "input_files": input_files,
        "status_counts": dict(Counter(row["preflight_cache_status"] for row in rows)),
        "reason_counts_top30": dict(Counter(
            reason
            for row in rows
            for reason, count in row.get("preflight_reason_counts", {}).items()
            for _ in range(int(count))
        ).most_common(30)),
        "by_dataset": dict(Counter(row.get("source_dataset") for row in rows)),
        "by_repo_top30": dict(Counter(row.get("repo") for row in rows).most_common(30)),
        "output": str(cache_path),
    }
    (output_dir / "preflight_cache_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
