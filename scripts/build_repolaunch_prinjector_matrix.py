"""Build the RepoLaunch x PR-INJECTOR coverage matrix skeleton.

This script materializes the PR-INJECTOR side from local construction runs and
creates explicit RepoLaunch columns that can be filled once a RepoLaunch runner
or result file is available.
"""

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


ROOT = Path(__file__).resolve().parent.parent


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("A_instance_id") or row.get("instance_id") or "")


def strict_verification_ok(row: dict[str, Any]) -> bool:
    verification = row.get("verification") or {}
    return (
        verification.get("pass_to_fail") is True
        and verification.get("golden_repair_pass") is True
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and verification.get("p2p_repaired_pass") is True
    )


def load_candidates(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in out:
                copied = dict(row)
                copied["candidate_source_file"] = str(path)
                out[iid] = copied
    return out


def load_injections(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl")):
            for row in read_jsonl(path):
                iid = row_id(row)
                if not iid:
                    continue
                copied = dict(row)
                copied["prinjector_injection_source_file"] = str(path)
                out[iid] = copied
    return out


def load_verifications(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl")):
            for row in read_jsonl(path):
                iid = row_id(row)
                if not iid:
                    continue
                copied = dict(row)
                copied["prinjector_verification_source_file"] = str(path)
                out[iid] = copied
    return out


def load_selected(path: Path | None) -> set[str]:
    if not path:
        return set()
    return {row_id(row) for row in read_jsonl(path) if row_id(row)}


def failure_bucket(injection: dict[str, Any] | None, verification: dict[str, Any] | None) -> str:
    if not injection:
        return "not_attempted"
    if not injection.get("success"):
        reason = str(injection.get("failure_reason") or "")
        if reason.startswith("preflight_failed:"):
            return reason.replace("preflight_failed: ", "preflight_")
        if "v2 fidelity gate" in reason:
            return "v2_fidelity_gate_failed"
        if "doesn't apply cleanly" in reason:
            return "l3_patch_apply_failed"
        if "outside current set" in reason:
            return "l3_file_scope_failed"
        return "injection_failed"
    if not verification:
        return "injection_success_not_verified"
    v = verification.get("verification") or {}
    if v.get("pass_to_fail") is not True:
        return "p2f_miss"
    if v.get("golden_repair_pass") is not True:
        return "golden_repair_not_pass"
    if int(v.get("p2p_buggy_failed") or 0) != 0:
        return "p2p_buggy_regression"
    if v.get("p2p_repaired_pass") is not True:
        return "p2p_repaired_not_pass"
    return "strict_verified"


def build_rows(
    candidates: dict[str, dict[str, Any]],
    injections: dict[str, dict[str, Any]],
    verifications: dict[str, dict[str, Any]],
    selected_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for iid, candidate in sorted(candidates.items()):
        selected_with_verification = iid in selected_ids and isinstance(candidate.get("verification"), dict)
        selected_with_injection = (
            iid in selected_ids
            and (
                candidate.get("success") is True
                or bool(candidate.get("injected_diff"))
                or bool(candidate.get("injection_level"))
                or selected_with_verification
            )
        )
        if selected_with_verification:
            injection = dict(candidate)
            injection["success"] = True
            verification = candidate
        else:
            injection = injections.get(iid)
            if not injection and selected_with_injection:
                injection = dict(candidate)
            verification = verifications.get(iid)
        preflight = (injection or {}).get("preflight") or {}
        bucket = failure_bucket(injection, verification)
        row = {
            "source_dataset": candidate.get("source_dataset") or (injection or {}).get("source_dataset"),
            "instance_id": iid,
            "repo": candidate.get("repo") or (injection or {}).get("repo"),
            "base_commit": (
                candidate.get("base_commit")
                or candidate.get("healthy_head")
                or candidate.get("B_healthy_head")
                or (injection or {}).get("base_commit")
                or (injection or {}).get("healthy_head")
            ),
            "candidate_source_file": candidate.get("candidate_source_file", ""),
            "RepoLaunch_status": "not_run",
            "RepoLaunch_success": None,
            "RepoLaunch_failure_reason": "",
            "RepoLaunch_image": "",
            "RepoLaunch_rebuild_command": "",
            "RepoLaunch_test_command": "",
            "PRInjector_attempted": injection is not None,
            "PRInjector_injection_success": bool((injection or {}).get("success")),
            "PRInjector_injection_level": (injection or {}).get("injection_level", ""),
            "PRInjector_failure_reason": (injection or {}).get("failure_reason", ""),
            "PRInjector_preflight_reason": preflight.get("reason", ""),
            "PRInjector_verification_attempted": verification is not None,
            "PRInjector_strict_verified": bucket == "strict_verified",
            "PRInjector_final_selected_current233": iid in selected_ids,
            "PRInjector_failure_bucket": bucket,
            "combined_available": None,
            "quadrant": "RepoLaunch_not_run",
            "prinjector_injection_source_file": (
                (injection or {}).get("prinjector_injection_source_file")
                or (candidate.get("candidate_source_file") if selected_with_injection else "")
            ),
            "prinjector_verification_source_file": (
                (verification or {}).get("prinjector_verification_source_file")
                or (candidate.get("candidate_source_file") if selected_with_verification else "")
            ),
        }
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--run-dir", action="append", default=[])
    parser.add_argument("--selected-current", default="")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    candidates = load_candidates([Path(path) for path in args.candidate_file])
    run_dirs = [Path(path) for path in args.run_dir]
    rows = build_rows(
        candidates,
        load_injections(run_dirs),
        load_verifications(run_dirs),
        load_selected(Path(args.selected_current) if args.selected_current else None),
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "repolaunch_prinjector_matrix.jsonl", rows)
    write_csv(output_dir / "repolaunch_prinjector_matrix.csv", rows)
    repolaunch_manifest = [
        {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "base_commit": row["base_commit"],
            "source_dataset": row["source_dataset"],
            "status": "ready_for_repolaunch_runner",
        }
        for row in rows
    ]
    write_jsonl(output_dir / "repolaunch_input_manifest.jsonl", repolaunch_manifest)
    summary = {
        "rows": len(rows),
        "by_dataset": dict(Counter(str(row["source_dataset"]) for row in rows)),
        "by_repo_top30": dict(Counter(str(row["repo"]) for row in rows).most_common(30)),
        "prinjector_attempted": sum(1 for row in rows if row["PRInjector_attempted"]),
        "prinjector_injection_success": sum(1 for row in rows if row["PRInjector_injection_success"]),
        "prinjector_strict_verified": sum(1 for row in rows if row["PRInjector_strict_verified"]),
        "prinjector_current233_selected": sum(1 for row in rows if row["PRInjector_final_selected_current233"]),
        "prinjector_failure_buckets": dict(Counter(str(row["PRInjector_failure_bucket"]) for row in rows).most_common()),
        "repolaunch_status": {"not_run": len(rows)},
        "next_step": "Run RepoLaunch on repolaunch_input_manifest.jsonl, then merge RepoLaunch_success/failure_reason into the matrix.",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
