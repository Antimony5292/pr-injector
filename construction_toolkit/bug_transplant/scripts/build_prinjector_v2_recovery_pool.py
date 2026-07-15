"""Build a retry pool for recoverable PR-INJECTOR v2 construction failures."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ModuleNotFoundError:
    from .prinjector_v2_metrics import read_jsonl, write_jsonl


RECOVERABLE_FAILURE_SUBSTRINGS = {
    "preflight_failed: test_runner_unavailable": "network_or_dependency_test_runner_unavailable",
    "Could not connect to the endpoint URL": "network_bedrock_endpoint_unavailable",
    "Failed to resolve 'pypi.org'": "network_pypi_resolution_failure",
    "nodename nor servname provided": "network_dns_resolution_failure",
    "error: corrupt patch": "l3_diff_extractor_fixed",
    "patch with only garbage": "l3_diff_extractor_fixed",
    "Codex L3 requires a clean dedicated construction worktree": "codex_submodule_baseline_fixed",
    "Codex L3 requires no pre-existing superproject source edits": "codex_submodule_baseline_fixed",
    "preflight_failed: healthy_target_not_executed": "preflight_target_execution_retry",
    "exception: Command '['": "preflight_subprocess_exception_retry",
    "You've hit your usage limit": "codex_usage_window_retry",
    "purchase more credits or try again at": "codex_usage_window_retry",
    "No module named 'luqum'": "openlibrary_runtime_dependency_fixed",
    "Populating font family aliases took": "qutebrowser_headless_qt_policy_fixed",
    "webEngineArgs' are overridden by QTWEBENGINE_CHROMIUM_FLAGS": "qutebrowser_headless_qt_policy_fixed",
}


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or "")


def load_candidate_index(paths: list[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in index:
                index[iid] = row
    return index


def injection_paths(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("shard_new_l1l2_*/verified_injection_results.jsonl"))


def verification_paths(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("shard_new_l1l2_*/verified_verification_results.jsonl"))


def classify_strict_verification(row: dict[str, Any]) -> tuple[str, str] | None:
    verification = row.get("verification") or {}
    if (
        verification.get("status") == "skipped"
        and verification.get("reason") == "too_many_target_tests"
    ):
        return (
            "strict_target_selector_normalization_retry",
            "The previous strict verifier counted overlapping file/class/method pytest selectors as "
            "independent targets. The harness now keeps the most specific leaf nodeids. Re-run the "
            "same injected behavior without changing the semantic diff.",
        )
    if not verification.get("pass_to_fail"):
        return (
            "strict_p2f_miss",
            "The previous diff matched complexity but did not reliably make the mapped target behavior fail. "
            "Trace the modern call chain and change the actual behavior contract exercised by the target tests; "
            "do not add a test-specific trigger.",
        )
    if verification.get("golden_repair_pass") is not True:
        return (
            "strict_golden_repair_not_pass",
            "The previous injected diff was not cleanly reversible into a healthy repaired state. Generate a "
            "self-contained semantic regression whose exact reverse restores all target behavior.",
        )
    if int(verification.get("p2p_buggy_failed") or 0) != 0:
        broken = verification.get("p2p_buggy_failed_tests") or verification.get("buggy_failed_tests") or []
        return (
            "strict_p2p_buggy_regression",
            "The previous injection broke protected adjacent/PASS_TO_PASS behavior. Preserve these broken "
            f"tests while recreating only the historical defect: {broken[:20]}",
        )
    if verification.get("p2p_repaired_pass") is not True:
        broken = verification.get("p2p_repaired_failed_tests") or []
        return (
            "strict_p2p_repaired_not_pass",
            "The previous golden restore left adjacent behavior failing. Keep the injection localized to the "
            f"historical semantic contract and ensure exact reversal restores these tests: {broken[:20]}",
        )
    return None


def failure_text(row: dict[str, Any]) -> str:
    parts = [str(row.get("failure_reason") or "")]
    preflight = row.get("preflight") or {}
    healthy = preflight.get("healthy_result") or {}
    fallback = (preflight.get("target_execution_fallback") or {}).get("result") or {}
    parts.append(str(healthy.get("output_tail") or ""))
    parts.append(str(fallback.get("output_tail") or ""))
    return "\n".join(parts)


def classify_failure(row: dict[str, Any], include_l3_gate_fail: bool) -> str | None:
    text = failure_text(row)
    for needle, reason in RECOVERABLE_FAILURE_SUBSTRINGS.items():
        if needle in text:
            return reason
    if include_l3_gate_fail and "generated diff failed v2 fidelity gate" in text:
        return "l3_v2_gate_retry_with_stronger_budget"
    if include_l3_gate_fail and "LLM diff doesn't apply cleanly" in text:
        return "l3_patch_apply_retry_against_modern_code"
    if include_l3_gate_fail and "LLM diff touched files outside current set" in text:
        return "l3_file_scope_retry_against_allowed_files"
    return None


def materialize(candidate: dict[str, Any], failure_row: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(candidate)
    out["v2_recovery_source_run"] = str(failure_row.get("_source_file") or "")
    out["v2_recovery_previous_injection_level"] = failure_row.get("injection_level")
    out["v2_recovery_failure_reason"] = failure_row.get("failure_reason")
    out["v2_recovery_reason"] = reason
    out["v2_recovery_previous_gate"] = (
        failure_row.get("v2_fidelity_gate")
        or (failure_row.get("l3_metadata") or {}).get("v2_fidelity_gate")
        or {}
    )
    feedback = failure_row.get("v2_fidelity_feedback_prompt") or (
        failure_row.get("l3_metadata") or {}
    ).get("v2_fidelity_feedback_prompt")
    if feedback:
        out["v2_retry_feedback_prompt"] = feedback
    elif reason == "l3_patch_apply_retry_against_modern_code":
        out["v2_retry_feedback_prompt"] = (
            "The previous Level 3 patch did not apply cleanly to the modern repository. "
            "Regenerate the injected bug strictly against the current file contents and line numbers. "
            "Preserve the historical bug semantics and comparable patch footprint, but do not reuse stale "
            "context from the old repository revision."
        )
    elif reason == "l3_file_scope_retry_against_allowed_files":
        out["v2_retry_feedback_prompt"] = (
            "The previous Level 3 patch modified files outside the accepted modern-code file scope. "
            "Regenerate the injected bug using only semantically corresponding files already identified "
            "for this candidate. Preserve the historical bug behavior and comparable complexity without "
            "adding unrelated files."
        )
    elif reason == "codex_submodule_baseline_fixed":
        out["v2_retry_feedback_prompt"] = (
            "The previous run was rejected before generation because repository bootstrap left a "
            "dirty submodule worktree. The harness now isolates that state. Reattempt the same "
            "semantic transplantation against the full modern repository."
        )
    elif reason in {"preflight_target_execution_retry", "preflight_subprocess_exception_retry"}:
        out["v2_retry_feedback_prompt"] = (
            "The previous construction attempt ended in target-test environment preparation rather "
            "than a semantic gate. Re-run preflight with the cached environment and then perform the "
            "same behavior-preserving historical bug transplantation."
        )
    elif reason == "codex_usage_window_retry":
        out["v2_retry_feedback_prompt"] = (
            "The previous attempt ended before generation because the configured Codex account was "
            "temporarily rate limited. Re-run the unchanged semantic transplantation after capacity "
            "is available; do not simplify the historical behavior contract."
        )
    elif reason in {
        "openlibrary_runtime_dependency_fixed",
        "qutebrowser_headless_qt_policy_fixed",
    }:
        out["v2_retry_feedback_prompt"] = (
            "The previous attempt stopped in healthy-HEAD environment preparation before bug "
            "generation. The harness now provides the missing declared dependency or normalized "
            "headless Qt runtime policy. Re-run the unchanged historical behavior transplantation."
        )
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--include-l3-gate-fail", action="store_true")
    parser.add_argument("--exclude-id-file", action="append", default=[])
    args = parser.parse_args()

    candidate_paths = [Path(path) for path in args.candidate_file]
    candidates = load_candidate_index(candidate_paths)
    excluded_ids = {
        row_id(row)
        for path in args.exclude_id_file
        for row in read_jsonl(Path(path))
        if row_id(row)
    }

    retry_rows: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    seen: set[str] = set()
    failure_rows: list[dict[str, Any]] = []
    verification_failure_rows = 0
    for path in injection_paths(Path(args.run_dir)):
        for row in read_jsonl(path):
            copied = dict(row)
            copied["_source_file"] = str(path)
            failure_rows.append(copied)

    for row in failure_rows:
        if row.get("success"):
            continue
        reason = classify_failure(row, args.include_l3_gate_fail)
        if not reason:
            continue
        iid = row_id(row)
        if iid in excluded_ids:
            continue
        if not iid or iid in seen:
            continue
        seen.add(iid)
        candidate = candidates.get(iid)
        if not candidate:
            missing.append(row)
            continue
        retry_rows.append(materialize(candidate, row, reason))

    for path in verification_paths(Path(args.run_dir)):
        for row in read_jsonl(path):
            classified = classify_strict_verification(row)
            if not classified:
                continue
            verification_failure_rows += 1
            iid = row_id(row)
            if not iid or iid in seen or iid in excluded_ids:
                continue
            seen.add(iid)
            candidate = candidates.get(iid)
            if not candidate:
                missing.append(row)
                continue
            reason, feedback = classified
            copied = dict(candidate)
            copied["v2_recovery_source_run"] = str(path)
            copied["v2_recovery_failure_reason"] = reason
            copied["v2_recovery_reason"] = reason
            copied["v2_retry_feedback_prompt"] = feedback
            copied["v2_recovery_previous_verification"] = row.get("verification") or {}
            retry_rows.append(copied)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "recovery_candidates.jsonl", retry_rows)
    write_jsonl(output_dir / "recovery_missing.jsonl", missing)

    summary = {
        "run_dir": args.run_dir,
        "candidate_files": [str(path) for path in candidate_paths],
        "candidate_rows_indexed": len(candidates),
        "failure_rows_scanned": sum(1 for row in failure_rows if not row.get("success")),
        "strict_verification_failure_rows_scanned": verification_failure_rows,
        "recovery_rows": len(retry_rows),
        "missing_rows": len(missing),
        "include_l3_gate_fail": args.include_l3_gate_fail,
        "excluded_ids": len(excluded_ids),
        "by_recovery_reason": dict(Counter(str(row.get("v2_recovery_reason")) for row in retry_rows).most_common()),
        "by_dataset": dict(Counter(str(row.get("source_dataset")) for row in retry_rows).most_common()),
        "by_repo_top20": dict(Counter(str(row.get("repo")) for row in retry_rows).most_common(20)),
        "output": str(output_dir / "recovery_candidates.jsonl"),
        "missing_output": str(output_dir / "recovery_missing.jsonl"),
    }
    (output_dir / "recovery_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
