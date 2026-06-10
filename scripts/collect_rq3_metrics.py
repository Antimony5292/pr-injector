"""Collect construction and RQ2 agent metrics for PR-INJECTOR experiments.

The construction side is read from a finalized B-set directory. Agent metrics
are optional and are parsed from run_rq2_claude_bedrock_eval.py outputs.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def safe_mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def safe_median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def summarize_numbers(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "sum": round(sum(values), 4) if values else 0,
        "mean": safe_mean(values),
        "median": safe_median(values),
        "min": round(min(values), 4) if values else None,
        "max": round(max(values), 4) if values else None,
    }


def field_number(obj: dict[str, Any], names: list[str]) -> float | None:
    for name in names:
        value = obj.get(name)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def patch_text(value: Any, final_dir: Path) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if value.lstrip().startswith("diff --git "):
        return value
    path = Path(value)
    candidates = [path]
    if not path.is_absolute():
        candidates.extend([ROOT / path, final_dir / path])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    return ""


def l3_cost_estimate_usd(prompt_tokens: int, completion_tokens: int) -> float:
    # Sonnet 4.x Bedrock public list prices are not hard-coded here because
    # account discounts/profiles vary. Keep this as token accounting; cost is
    # reported only when the upstream record exposes it.
    return 0.0 if prompt_tokens == completion_tokens == 0 else 0.0


def construction_metrics(final_dir: Path) -> dict[str, Any]:
    rows = read_jsonl(final_dir / "selected.jsonl")
    if not rows:
        rows = read_jsonl(final_dir / "injection_results.jsonl")

    levels = Counter(str(row.get("injection_level") or "unknown") for row in rows)
    datasets = Counter(str(row.get("source_dataset") or "unknown") for row in rows)
    repos = Counter(str(row.get("repo") or "unknown") for row in rows)
    diff_file_counts = Counter()
    source_patch_file_counts = Counter()
    verification_seconds_by_level: dict[str, list[float]] = defaultdict(list)
    l3_prompt_tokens = 0
    l3_completion_tokens = 0
    l3_attempts: list[float] = []
    l3_confidence: list[float] = []
    l3_costs: list[float] = []
    estimated_test_runs_by_level: dict[str, list[float]] = defaultdict(list)
    p2p_clean_counts: list[float] = []
    p2p_raw_counts: list[float] = []

    for row in rows:
        level = str(row.get("injection_level") or "unknown")
        injected_diff = patch_text(row.get("injected_diff"), final_dir)
        diff_file_counts[len([line for line in injected_diff.splitlines() if line.startswith("diff --git ")])] += 1
        patch = patch_text(row.get("patch"), final_dir)
        source_patch_file_counts[len([line for line in patch.splitlines() if line.startswith("diff --git ")])] += 1

        verification = row.get("verification") or {}
        duration = field_number(verification, ["duration_seconds", "elapsed_seconds", "runtime_seconds"])
        if duration is not None:
            verification_seconds_by_level[level].append(duration)

        raw_p2p = field_number(verification, ["pass_to_pass_test_count"])
        clean_p2p = field_number(verification, ["clean_pass_to_pass_count"])
        if raw_p2p is not None:
            p2p_raw_counts.append(raw_p2p)
        if clean_p2p is not None:
            p2p_clean_counts.append(clean_p2p)

        # Approximation: preflight target run + healthy target + buggy target +
        # per-test P2P cleaner + buggy P2P aggregate + repaired target +
        # repaired P2P aggregate. The exact subprocess count is runner-specific.
        estimated_runs = 0
        preflight = row.get("preflight") or {}
        if preflight.get("healthy_result") is not None:
            estimated_runs += 1
        if verification.get("healthy_pass") is not None:
            estimated_runs += 1
        if verification.get("target_tests_failed") is not None:
            estimated_runs += 1
        if raw_p2p is not None:
            estimated_runs += int(raw_p2p)
        if verification.get("pass_to_pass_checked"):
            estimated_runs += 1
        if verification.get("golden_repair_checked"):
            estimated_runs += 1
        if verification.get("p2p_repaired_pass") is not None:
            estimated_runs += 1
        estimated_test_runs_by_level[level].append(float(estimated_runs))

        l3 = row.get("l3_metadata") or {}
        l3_prompt_tokens += int(l3.get("prompt_tokens") or 0)
        l3_completion_tokens += int(l3.get("completion_tokens") or 0)
        attempt = field_number(l3, ["attempt", "attempts", "retry_count"])
        if attempt is not None:
            l3_attempts.append(attempt)
        confidence = field_number(l3, ["confidence"])
        if confidence is not None:
            l3_confidence.append(confidence)
        cost = field_number(l3, ["cost_usd", "total_cost_usd"])
        if cost is not None:
            l3_costs.append(cost)

    return {
        "final_dir": str(final_dir),
        "total_cases": len(rows),
        "by_source_dataset": dict(datasets.most_common()),
        "by_repo": dict(repos.most_common()),
        "by_injection_level": dict(levels.most_common()),
        "injected_diff_file_count_histogram": dict(sorted(diff_file_counts.items())),
        "source_patch_file_count_histogram": dict(sorted(source_patch_file_counts.items())),
        "verification_duration_seconds_by_level": {
            level: summarize_numbers(values) for level, values in sorted(verification_seconds_by_level.items())
        },
        "estimated_test_run_count_by_level": {
            level: summarize_numbers(values) for level, values in sorted(estimated_test_runs_by_level.items())
        },
        "p2p_clean_count": summarize_numbers(p2p_clean_counts),
        "p2p_raw_count": summarize_numbers(p2p_raw_counts),
        "l3_usage": {
            "prompt_tokens": l3_prompt_tokens,
            "completion_tokens": l3_completion_tokens,
            "total_tokens": l3_prompt_tokens + l3_completion_tokens,
            "attempts": summarize_numbers(l3_attempts),
            "confidence": summarize_numbers(l3_confidence),
            "reported_cost_usd": summarize_numbers(l3_costs),
            "estimated_cost_usd": l3_cost_estimate_usd(l3_prompt_tokens, l3_completion_tokens),
            "cost_note": "reported_cost_usd is used when present; estimated_cost_usd is intentionally 0 without account-specific pricing.",
        },
    }


def parse_claude_stdout(result: dict[str, Any]) -> dict[str, Any]:
    raw = result.get("agent", {}).get("raw") or {}
    stdout = raw.get("stdout") or ""
    if not stdout.strip():
        raw_path = result.get("agent", {}).get("raw_output_path")
        if raw_path:
            raw_json = read_json(Path(raw_path))
            stdout = (raw_json or {}).get("stdout") or ""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {}


def agent_metrics_for_group(run_dir: Path, group: str) -> dict[str, Any]:
    results = sorted(run_dir.glob(f"*/{group}/result.json"))
    elapsed: list[float] = []
    duration_ms: list[float] = []
    api_ms: list[float] = []
    costs: list[float] = []
    turns: list[float] = []
    infra_attempts: list[float] = []
    transient_failures = 0
    forbidden_edits = 0
    pre_agent_dirty = 0
    solved = 0
    strict_solved = 0
    target_solved = 0
    usage_counter: Counter[str] = Counter()
    model_costs: Counter[str] = Counter()

    for path in results:
        result = read_json(path)
        if not result:
            continue
        agent = result.get("agent") or {}
        value = field_number(agent, ["elapsed_s"])
        if value is not None:
            elapsed.append(value)
        attempts = agent.get("infra_attempts") or []
        infra_attempts.append(float(len(attempts)))
        transient_failures += sum(1 for item in attempts if item.get("transient_failure"))
        forbidden_edits += 1 if result.get("agent_forbidden_files") else 0
        setup = result.get("setup") or {}
        if setup.get("pre_agent_git_status") or setup.get("pre_agent_git_diff_size"):
            pre_agent_dirty += 1
        evaluation = result.get("evaluation") or {}
        solved += 1 if evaluation.get("solved") else 0
        strict_solved += 1 if evaluation.get("strict_solved") else 0
        target_solved += 1 if evaluation.get("target_solved") else 0

        claude = parse_claude_stdout(result)
        for src, dst in [
            ("duration_ms", duration_ms),
            ("duration_api_ms", api_ms),
            ("total_cost_usd", costs),
            ("num_turns", turns),
        ]:
            num = field_number(claude, [src])
            if num is not None:
                dst.append(num)
        usage = claude.get("usage") or {}
        for key in ["input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"]:
            usage_counter[key] += int(usage.get(key) or 0)
        model_usage = claude.get("modelUsage") or {}
        for model, item in model_usage.items():
            model_costs[model] += float(item.get("costUSD") or 0.0)

    return {
        "run_dir": str(run_dir),
        "group": group,
        "completed_results": len(results),
        "solved": solved,
        "strict_solved": strict_solved,
        "target_solved": target_solved,
        "elapsed_s": summarize_numbers(elapsed),
        "claude_duration_ms": summarize_numbers(duration_ms),
        "claude_duration_api_ms": summarize_numbers(api_ms),
        "claude_total_cost_usd": summarize_numbers(costs),
        "claude_num_turns": summarize_numbers(turns),
        "claude_usage": dict(usage_counter),
        "claude_model_cost_usd": dict(model_costs),
        "infra_attempts": summarize_numbers(infra_attempts),
        "transient_failure_attempts": transient_failures,
        "forbidden_edit_results": forbidden_edits,
        "pre_agent_dirty_results": pre_agent_dirty,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-dir", required=True)
    parser.add_argument("--a-run-dir")
    parser.add_argument("--b-run-dir")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    final_dir = Path(args.final_dir).resolve()
    report: dict[str, Any] = {
        "construction": construction_metrics(final_dir),
        "agent": {},
    }
    if args.a_run_dir:
        report["agent"]["A"] = agent_metrics_for_group(Path(args.a_run_dir).resolve(), "A")
    if args.b_run_dir:
        report["agent"]["B"] = agent_metrics_for_group(Path(args.b_run_dir).resolve(), "B")

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
