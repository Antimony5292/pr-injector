"""Collect live observations from PR-INJECTOR v2 construction runs.

The output is intentionally research-facing: it separates environment/repo
quality signals, PR-INJECTOR framework issues, and LLM/L3 weaknesses.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import read_jsonl


def short(text: Any, limit: int = 220) -> str:
    value = str(text or "").replace("\n", " ").strip()
    return value[:limit]


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or "")


def load_injection_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl")):
        for row in read_jsonl(path):
            copied = dict(row)
            copied["_source_file"] = str(path)
            rows.append(copied)
    return rows


def load_verification_rows(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl")):
        for row in read_jsonl(path):
            copied = dict(row)
            copied["_source_file"] = str(path)
            rows.append(copied)
    return rows


def exemplar(row: dict[str, Any], detail: str = "") -> dict[str, Any]:
    preflight = row.get("preflight") or {}
    l3 = row.get("l3_metadata") or {}
    gate = get_v2_gate(row)
    return {
        "instance_id": row.get("instance_id"),
        "source_instance_id": row.get("source_instance_id"),
        "repo": row.get("repo"),
        "source_dataset": row.get("source_dataset"),
        "injection_level": row.get("injection_level"),
        "failure_reason": short(row.get("failure_reason")),
        "preflight_reason": preflight.get("reason"),
        "requires_python": preflight.get("requires_python"),
        "v2_score": gate.get("score"),
        "v2_tags": gate.get("tags") or [],
        "l3_finish_reason": l3.get("finish_reason"),
        "l3_attempts": l3.get("attempt"),
        "l3_total_tokens": l3.get("total_tokens"),
        "detail": detail,
        "source_file": row.get("_source_file"),
    }


def append_limited(bucket: dict[str, list[dict[str, Any]]], key: str, row: dict[str, Any], detail: str = "", limit: int = 8) -> None:
    if len(bucket[key]) < limit:
        bucket[key].append(exemplar(row, detail))


def append_direct_limited(bucket: dict[str, list[dict[str, Any]]], key: str, item: dict[str, Any], limit: int = 8) -> None:
    if len(bucket[key]) < limit:
        bucket[key].append(item)


def log_exemplar(path: Path, detail: str, failure_reason: str = "") -> dict[str, Any]:
    return {
        "instance_id": None,
        "source_instance_id": None,
        "repo": None,
        "source_dataset": None,
        "injection_level": "Log_Signal",
        "failure_reason": short(failure_reason),
        "preflight_reason": None,
        "requires_python": None,
        "v2_score": None,
        "v2_tags": [],
        "l3_finish_reason": None,
        "l3_attempts": None,
        "l3_total_tokens": None,
        "detail": detail,
        "source_file": str(path),
    }


def get_v2_gate(row: dict[str, Any]) -> dict[str, Any]:
    l3 = row.get("l3_metadata") or {}
    gate = row.get("v2_fidelity_gate") or l3.get("v2_fidelity_gate") or {}
    return gate if isinstance(gate, dict) else {}


def get_v2_gate_pass(row: dict[str, Any]) -> bool | None:
    if "v2_fidelity_gate_pass" in row:
        value = row.get("v2_fidelity_gate_pass")
        if isinstance(value, bool):
            return value
    l3 = row.get("l3_metadata") or {}
    if "v2_fidelity_gate_pass" in l3:
        value = l3.get("v2_fidelity_gate_pass")
        if isinstance(value, bool):
            return value
    gate = get_v2_gate(row)
    if "pass_gate" in gate:
        return bool(gate.get("pass_gate"))
    return None


def collect_log_signals(run_dir: Path) -> dict[str, Any]:
    dep_counter: Counter[str] = Counter()
    no_python: Counter[str] = Counter()
    rejected_gate_lines = 0
    patch_apply_fail_lines = 0
    max_tokens_lines = 0
    pypi_resolution_failure_lines = 0
    bedrock_endpoint_failure_lines = 0
    log_observations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_injection.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        path_deps: Counter[str] = Counter()
        for match in re.finditer(r"Installing missing deps \(round \d+\): \[(.*?)\]", text):
            for dep in re.findall(r"'([^']+)'", match.group(1)):
                dep_counter[dep] += 1
                path_deps[dep] += 1
        for match in re.finditer(r"No available Python satisfies ([^\n]+)", text):
            no_python[match.group(1).strip()] += 1
        rejected_gate_lines += text.count("Rejected v2-gate-failing Level 3 diff")
        patch_apply_fail_lines += text.count("patch does not apply") + text.count("Patch check failed")
        max_tokens_lines += text.count("finish=max_tokens")
        pypi_resolution_failure_lines += text.count("Failed to resolve 'pypi.org'")
        pypi_resolution_failure_lines += text.count("nodename nor servname provided")
        bedrock_endpoint_failure_lines += text.count("Could not connect to the endpoint URL")
        if path_deps:
            append_direct_limited(
                log_observations,
                "repo_test_dependency_bootstrap_fragility",
                log_exemplar(path, f"missing deps during target-test bootstrap: {dict(path_deps.most_common(12))}"),
            )
        if "Failed to resolve 'pypi.org'" in text or "nodename nor servname provided" in text:
            append_direct_limited(
                log_observations,
                "network_package_resolution_failure",
                log_exemplar(path, "dependency bootstrap failed while resolving PyPI; retry after network recovery"),
            )
        if "Could not connect to the endpoint URL" in text:
            append_direct_limited(
                log_observations,
                "network_bedrock_endpoint_failure",
                log_exemplar(path, "Level 3 Bedrock call failed due endpoint/network connectivity"),
            )
        if "unexpected line:" in text or "corrupt patch" in text:
            detail = "LLM output included non-diff explanatory text or malformed hunks during Level 3 patch generation"
            line_match = re.search(r"(Patch check failed: .*?(?:\n.*?corrupt patch[^\n]*)?)", text)
            append_direct_limited(
                log_observations,
                "l3_diff_format_violation",
                log_exemplar(path, detail, line_match.group(1) if line_match else "corrupt patch"),
            )
        if "finish=max_tokens" in text:
            append_direct_limited(
                log_observations,
                "l3_attempt_output_budget_pressure",
                log_exemplar(path, f"Level 3 had {text.count('finish=max_tokens')} max-token attempts in this shard"),
            )
    return {
        "missing_dependency_top": dict(dep_counter.most_common(30)),
        "python_constraints_unavailable": dict(no_python.most_common(20)),
        "rejected_v2_gate_l3_lines": rejected_gate_lines,
        "patch_apply_failure_lines": patch_apply_fail_lines,
        "max_tokens_lines": max_tokens_lines,
        "pypi_resolution_failure_lines": pypi_resolution_failure_lines,
        "bedrock_endpoint_failure_lines": bedrock_endpoint_failure_lines,
        "observations": dict(log_observations),
    }


def build_observations(run_dir: Path) -> dict[str, Any]:
    rows = load_injection_rows(run_dir)
    verifications = load_verification_rows(run_dir)
    observations: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        preflight = row.get("preflight") or {}
        failure = str(row.get("failure_reason") or "")
        level = str(row.get("injection_level") or "")
        l2 = row.get("l2_metadata") or {}
        l3 = row.get("l3_metadata") or {}
        gate = get_v2_gate(row)
        gate_pass = get_v2_gate_pass(row)

        if preflight.get("reason") == "python_version_unavailable":
            append_limited(
                observations,
                "repo_environment_python_version_gap",
                row,
                f"modern repo requires Python {preflight.get('requires_python')}",
            )
        if preflight.get("reason") in {"target_nodeids_not_remappable", "target_nodeids_not_collectable"}:
            append_limited(
                observations,
                "benchmark_target_mapping_gap",
                row,
                f"target tests could not be remapped: {short(preflight.get('raw_target_tests'))}",
            )
        if preflight.get("reason") == "healthy_target_not_executed":
            healthy_result = preflight.get("healthy_result") or {}
            append_limited(
                observations,
                "repo_or_harness_test_execution_not_executed",
                row,
                (
                    f"returncode={healthy_result.get('returncode')} "
                    f"total={healthy_result.get('total')} "
                    f"tail={short(healthy_result.get('output_tail'), 180)}"
                ),
            )
        if preflight.get("reason") == "test_runner_unavailable":
            append_limited(
                observations,
                "test_runner_unavailable",
                row,
                "pytest or the project test runner was unavailable after dependency bootstrap",
            )
        if preflight.get("reason") == "healthy_target_failed":
            healthy_result = preflight.get("healthy_result") or {}
            append_limited(
                observations,
                "healthy_head_target_instability",
                row,
                (
                    f"returncode={healthy_result.get('returncode')} "
                    f"failed={healthy_result.get('failed')} "
                    f"failed_tests={len(preflight.get('healthy_failed_tests') or [])} "
                    f"tail={short(healthy_result.get('output_tail'), 180)}"
                ),
            )
        fallback = preflight.get("target_execution_fallback") or {}
        fallback_result = fallback.get("result") or {}
        if fallback and int(fallback_result.get("total") or 0) > 0:
            append_limited(
                observations,
                "target_execution_fallback_broadens_surface",
                row,
                f"fallback total={fallback_result.get('total')} from={len(fallback.get('from') or [])} to={len(fallback.get('to') or [])}",
            )
        if l2.get("compatibility_rejected_files"):
            append_limited(
                observations,
                "level2_api_drift_or_symbol_gap",
                row,
                f"compatibility rejected files={l2.get('compatibility_rejected_files')}",
            )
        if gate_pass is False or "v2_fidelity_gate_failed" in failure or "generated diff failed v2 fidelity gate" in failure:
            append_limited(
                observations,
                "llm_or_l2_complexity_collapse",
                row,
                f"v2 score={gate.get('score')} tags={gate.get('tags')}",
            )
        if "patch does not apply" in failure or "doesn't apply cleanly" in failure:
            append_limited(
                observations,
                "l3_patch_application_drift",
                row,
                "LLM produced stale or non-applicable unified diff against modern code",
            )
        if l3.get("finish_reason") == "max_tokens":
            append_limited(
                observations,
                "l3_context_or_output_budget_limit",
                row,
                f"finish=max_tokens total_tokens={l3.get('total_tokens')}",
            )
        if level.startswith("Level_3") and not row.get("success"):
            append_limited(
                observations,
                "l3_failed_after_retry_budget",
                row,
                f"attempts={l3.get('attempt')} reason={short(failure)}",
            )

    verification_counter: Counter[str] = Counter()
    verification_status_counter: Counter[str] = Counter()
    strict_verified_count = 0
    for row in verifications:
        verification = row.get("verification") or {}
        verification_status_counter[str(verification.get("status") or "missing")] += 1
        if not verification.get("pass_to_fail"):
            verification_counter["p2f_miss"] += 1
        if verification.get("golden_repair_pass") is False:
            verification_counter["golden_repair_failed"] += 1
        if int(verification.get("p2p_buggy_failed") or 0) > 0:
            verification_counter["p2p_buggy_regression"] += 1
        if verification.get("p2p_repaired_pass") is False:
            verification_counter["p2p_repaired_failed"] += 1
        if (
            verification.get("pass_to_fail")
            and verification.get("golden_repair_pass") is True
            and int(verification.get("p2p_buggy_failed") or 0) == 0
            and verification.get("p2p_repaired_pass") is True
        ):
            strict_verified_count += 1

    l3_tokens = [
        int((row.get("injection_metrics") or {}).get("l3_total_tokens") or 0)
        for row in rows
        if int((row.get("injection_metrics") or {}).get("l3_total_tokens") or 0) > 0
    ]
    token_summary = {
        "count": len(l3_tokens),
        "total": sum(l3_tokens),
        "max": max(l3_tokens) if l3_tokens else 0,
        "avg": round(sum(l3_tokens) / len(l3_tokens), 2) if l3_tokens else 0,
    }
    log_signals = collect_log_signals(run_dir)
    for key, examples in (log_signals.get("observations") or {}).items():
        for item in examples:
            append_direct_limited(observations, key, item)

    gate_pass_values = [
        str(value)
        for row in rows
        for value in [get_v2_gate_pass(row)]
        if value is not None
    ]

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir),
        "injection_rows": len(rows),
        "verification_rows": len(verifications),
        "success_counts": dict(Counter(str(bool(row.get("success"))) for row in rows)),
        "injection_levels": dict(Counter(str(row.get("injection_level")) for row in rows).most_common()),
        "failure_reasons": dict(Counter(short(row.get("failure_reason"), 120) for row in rows if not row.get("success")).most_common(30)),
        "preflight_reasons": dict(Counter(str((row.get("preflight") or {}).get("reason")) for row in rows if row.get("preflight")).most_common()),
        "repos_seen": dict(Counter(str(row.get("repo")) for row in rows).most_common(30)),
        "v2_gate_pass_counts": dict(Counter(gate_pass_values)),
        "l3_token_summary": token_summary,
        "verification_status_counts": dict(verification_status_counter),
        "strict_verified_count": strict_verified_count,
        "verification_failure_counts": dict(verification_counter),
        "log_signals": log_signals,
        "observations": dict(observations),
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# PR-INJECTOR v2 Live Observations")
    lines.append("")
    lines.append(f"- generated_at_utc: `{payload['generated_at_utc']}`")
    lines.append(f"- run_dir: `{payload['run_dir']}`")
    lines.append(f"- injection_rows: `{payload['injection_rows']}`")
    lines.append(f"- verification_rows: `{payload['verification_rows']}`")
    lines.append(f"- success_counts: `{payload['success_counts']}`")
    lines.append(f"- injection_levels: `{payload['injection_levels']}`")
    lines.append(f"- l3_token_summary: `{payload['l3_token_summary']}`")
    lines.append("")
    lines.append("## Current Counters")
    lines.append("")
    lines.append(f"- failure_reasons: `{payload['failure_reasons']}`")
    lines.append(f"- preflight_reasons: `{payload['preflight_reasons']}`")
    lines.append(f"- v2_gate_pass_counts: `{payload['v2_gate_pass_counts']}`")
    lines.append(f"- verification_status_counts: `{payload['verification_status_counts']}`")
    lines.append(f"- strict_verified_count: `{payload['strict_verified_count']}`")
    lines.append(f"- verification_failure_counts: `{payload['verification_failure_counts']}`")
    lines.append(f"- log_signals: `{payload['log_signals']}`")
    lines.append("")
    lines.append("## Observation Buckets")
    lines.append("")
    for key, examples in sorted(payload["observations"].items()):
        lines.append(f"### {key}")
        lines.append("")
        for item in examples:
            label = item.get("repo") or item.get("source_file") or "log"
            lines.append(
                "- "
                f"`{label}` `{item.get('instance_id')}` "
                f"level=`{item.get('injection_level')}` "
                f"reason=`{item.get('failure_reason')}` "
                f"detail={item.get('detail')}"
            )
            if item.get("v2_tags"):
                lines.append(f"  v2: score=`{item.get('v2_score')}` tags=`{item.get('v2_tags')}`")
            if item.get("l3_total_tokens"):
                lines.append(
                    f"  l3: finish=`{item.get('l3_finish_reason')}` "
                    f"attempts=`{item.get('l3_attempts')}` tokens=`{item.get('l3_total_tokens')}`"
                )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_observations(Path(args.run_dir))
    (output_dir / "observations.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "observations.md", payload)
    print(json.dumps({
        "run_dir": payload["run_dir"],
        "injection_rows": payload["injection_rows"],
        "verification_rows": payload["verification_rows"],
        "observation_buckets": {key: len(value) for key, value in payload["observations"].items()},
        "output_dir": str(output_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
