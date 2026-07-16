"""Build second-chance PR-INJECTOR v2 candidates from failed construction rows."""

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


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("A_instance_id") or row.get("instance_id") or "")


def load_index(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in out:
                out[iid] = row
    return out


def injection_paths(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl"))


def verification_paths(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl"))


def injection_retry_reason(row: dict[str, Any]) -> tuple[str, str] | None:
    if row.get("success"):
        return None
    reason = str(row.get("failure_reason") or "")
    if "generated diff failed v2 fidelity gate" in reason:
        return (
            "l3_v2_gate_retry",
            "Previous generated bug failed the v2 complexity-fidelity gate. Preserve the historical bug semantics while increasing structural footprint only where semantically justified.",
        )
    if "LLM diff doesn't apply cleanly" in reason:
        return (
            "l3_patch_apply_retry",
            "Previous L3 diff was stale for the modern repository. Regenerate strictly against current file contents and current line numbers.",
        )
    if "LLM diff touched files outside current set" in reason:
        return (
            "l3_file_scope_retry",
            "Previous L3 diff touched files outside the accepted semantic file scope. Stay within corresponding files and avoid unrelated edits.",
        )
    if "patch applied but produced no diff" in reason:
        return (
            "l3_noop_retry",
            "Previous generated patch applied as a no-op. Produce a real injected bug that changes behavior and makes target tests fail.",
        )
    return None


def verification_retry_reason(row: dict[str, Any]) -> tuple[str, str] | None:
    verification = row.get("verification") or {}
    if verification.get("pass_to_fail") is not True:
        return (
            "verification_p2f_miss_retry",
            "Previous injected bug did not make the target tests fail. Regenerate the bug so the specified target tests fail on the buggy revision while healthy HEAD still passes.",
        )
    if verification.get("golden_repair_pass") is not True:
        return (
            "verification_golden_repair_retry",
            "Previous injected bug could not be repaired by the golden reverse patch. Regenerate a reversible injected bug whose inverse patch cleanly restores target behavior.",
        )
    if int(verification.get("p2p_buggy_failed") or 0) != 0:
        return (
            "verification_p2p_regression_retry",
            "Previous injected bug over-broadened the failure and broke adjacent/P2P tests. Keep the target failure but narrow the injected defect so clean P2P tests continue to pass.",
        )
    if verification.get("p2p_repaired_pass") is not True:
        return (
            "verification_p2p_repair_retry",
            "Previous golden repair fixed target tests but did not restore adjacent/P2P behavior. Regenerate a cleaner reversible bug with no residual regression after repair.",
        )
    return None


def materialize(candidate: dict[str, Any], reason: str, feedback: str, source_file: str) -> dict[str, Any]:
    out = dict(candidate)
    out["v2_second_chance_reason"] = reason
    out["v2_second_chance_source_file"] = source_file
    out["v2_retry_feedback_prompt"] = feedback
    out["v2_construction_source"] = "v2_second_chance"
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--selected-current", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dataset", action="append", default=[])
    parser.add_argument("--limit", type=int, default=400)
    args = parser.parse_args()

    candidates = load_index([Path(path) for path in args.candidate_file])
    selected_ids = {row_id(row) for row in read_jsonl(Path(args.selected_current)) if row_id(row)}
    dataset_filter = set(args.dataset)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for run_dir in [Path(path) for path in args.run_dir]:
        for path in injection_paths(run_dir):
            for row in read_jsonl(path):
                iid = row_id(row)
                if not iid or iid in selected_ids or iid in seen:
                    continue
                candidate = candidates.get(iid)
                if not candidate:
                    continue
                if dataset_filter and str(candidate.get("source_dataset") or "") not in dataset_filter:
                    continue
                classified = injection_retry_reason(row)
                if not classified:
                    continue
                reason, feedback = classified
                rows.append(materialize(candidate, reason, feedback, str(path)))
                seen.add(iid)

        for path in verification_paths(run_dir):
            for row in read_jsonl(path):
                iid = row_id(row)
                if not iid or iid in selected_ids or iid in seen:
                    continue
                candidate = candidates.get(iid)
                if not candidate:
                    continue
                if dataset_filter and str(candidate.get("source_dataset") or "") not in dataset_filter:
                    continue
                classified = verification_retry_reason(row)
                if not classified:
                    continue
                reason, feedback = classified
                rows.append(materialize(candidate, reason, feedback, str(path)))
                seen.add(iid)

    rows.sort(key=lambda row: (
        str(row.get("source_dataset") or ""),
        str(row.get("v2_second_chance_reason") or ""),
        str(row.get("repo") or ""),
        row_id(row),
    ))
    if args.limit:
        rows = rows[: args.limit]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "second_chance_candidates.jsonl", rows)
    summary = {
        "rows": len(rows),
        "datasets": dict(Counter(str(row.get("source_dataset") or "") for row in rows)),
        "reasons": dict(Counter(str(row.get("v2_second_chance_reason") or "") for row in rows).most_common()),
        "repos_top30": dict(Counter(str(row.get("repo") or "") for row in rows).most_common(30)),
        "selected_current_excluded": len(selected_ids),
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
