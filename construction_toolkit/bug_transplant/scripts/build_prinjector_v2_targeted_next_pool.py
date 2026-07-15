"""Build targeted PR-INJECTOR v2 pools after a low-yield construction wave.

The output has two separate queues:

* feedback_retry_candidates.jsonl: recent P2F/P2P failures with explicit L3
  feedback attached in ``v2_retry_feedback_prompt``.
* fresh_candidates.jsonl: unprocessed Verified/Pro candidates ranked toward
  historically higher-yield repos and bounded test/patch surfaces.

The queues are intentionally split so retry runs can force L3 while fresh runs
can still use the cheaper L1/L2/L3 cascade.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import patch_profile, read_jsonl, write_jsonl


DEFAULT_QUOTAS = {
    "princeton-nlp/SWE-bench": 170,
    "princeton-nlp/SWE-bench_Verified": 125,
    "ScaleAI/SWE-bench_Pro": 125,
    "SWE-bench-Live/SWE-bench-Live": 60,
    "SWE-bench/SWE-bench_Lite": 20,
}


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


def coerce_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [value]
    return [str(value)]


def candidate_paths_from_roots(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            paths.append(root)
            continue
        paths.extend(sorted(root.glob("candidate_pool*.jsonl")))
        paths.extend(sorted(root.glob("*candidates*.jsonl")))
        for child in sorted(path for path in root.iterdir() if path.is_dir()):
            paths.extend(sorted(child.glob("candidate_pool*.jsonl")))
            paths.extend(sorted(child.glob("*candidates*.jsonl")))
    seen: set[Path] = set()
    out: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(path)
    return out


def load_candidates(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in out:
                copied = dict(row)
                copied["targeted_pool_candidate_source_file"] = str(path)
                out[iid] = copied
    return out


def load_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid:
                ids.add(iid)
    return ids


def result_paths(run_dir: Path, filename: str) -> list[Path]:
    if not run_dir.exists():
        return []
    if run_dir.is_file():
        return [run_dir] if run_dir.name == filename else []
    return sorted(run_dir.rglob(filename))


def load_injections(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        for path in result_paths(run_dir, "verified_injection_results.jsonl"):
            for row in read_jsonl(path):
                iid = row_id(row)
                if iid:
                    copied = dict(row)
                    copied["targeted_pool_injection_source_file"] = str(path)
                    out[iid] = copied
    return out


def load_verifications(run_dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        for path in result_paths(run_dir, "verified_verification_results.jsonl"):
            for row in read_jsonl(path):
                copied = dict(row)
                copied["targeted_pool_verification_source_file"] = str(path)
                rows.append(copied)
    return rows


def processed_ids(run_dirs: list[Path]) -> set[str]:
    ids: set[str] = set()
    for run_dir in run_dirs:
        for path in result_paths(run_dir, "verified_injection_results.jsonl"):
            ids |= load_ids([path])
    return ids


def verification_reason(injection: dict[str, Any], verification_row: dict[str, Any]) -> str:
    verification = verification_row.get("verification") or {}
    if not injection.get("success"):
        return "injection_not_success"
    if verification.get("pass_to_fail") is not True:
        return "p2f_miss"
    if verification.get("golden_repair_pass") is not True:
        return "golden_repair_not_pass"
    if int(verification.get("p2p_buggy_failed") or 0) != 0:
        return "p2p_buggy_regression"
    if verification.get("p2p_repaired_pass") is not True:
        return "p2p_repaired_not_pass"
    return "strict_ok"


def feedback_prompt(reason: str, injection: dict[str, Any], verification_row: dict[str, Any]) -> str:
    verification = verification_row.get("verification") or {}
    target_tests = (
        verification.get("actual_failed_tests")
        or injection.get("fail_to_pass")
        or injection.get("FAIL_TO_PASS")
        or []
    )
    p2p_failed = verification.get("p2p_buggy_failed_tests") or verification.get("p2p_failed_tests") or []
    clean_p2p = verification.get("clean_pass_to_pass") or injection.get("B_PASS_TO_PASS_CLEAN") or []
    gate = injection.get("v2_fidelity_gate") or (injection.get("l3_metadata") or {}).get("v2_fidelity_gate") or {}
    a_profile = gate.get("A_profile") or {}
    b_profile = gate.get("B_profile") or {}
    base = [
        "This is a retry after behavioral verification failed.",
        "Generate a new Level-3 semantic bug injection for the current code, not a restatement of the previous diff.",
        "The diff must still match the original A-side bug complexity and must apply only to current source files.",
        "Preserve protected PASS_TO_PASS/adjacent behavior while making the target behavior fail.",
    ]
    if reason == "p2f_miss":
        base.extend([
            "Previous failure: the injected bug did NOT make the target FAIL_TO_PASS tests fail.",
            "The new injected defect must directly affect the behavior exercised by these target tests.",
        ])
    elif reason == "p2p_buggy_regression":
        base.extend([
            "Previous failure: the injected bug caused unrelated PASS_TO_PASS/adjacent regression failures.",
            "Narrow the defect to the historical bug semantics and avoid changing unrelated behavior.",
            "Do not solve this by deleting broad branches, disabling validation globally, or changing shared helpers beyond the historical contract.",
        ])
    elif reason == "p2p_repaired_not_pass":
        base.extend([
            "Previous failure: the golden repair did not restore the broader PASS_TO_PASS surface.",
            "Keep the bug and repair path aligned with the original patch so applying the repair fully restores behavior.",
        ])
    if gate:
        base.append(
            "Complexity target: "
            f"A(files={a_profile.get('source_files') or a_profile.get('files')}, "
            f"hunks={a_profile.get('hunks')}, lines={a_profile.get('line_changes')}, "
            f"symbols={a_profile.get('symbols')}); "
            f"previous B(files={b_profile.get('source_files') or b_profile.get('files')}, "
            f"hunks={b_profile.get('hunks')}, lines={b_profile.get('line_changes')}, "
            f"symbols={b_profile.get('symbols')}); "
            f"previous_score={gate.get('score')}, previous_tags={gate.get('tags')}."
        )
    if target_tests:
        base.append("Target tests that must fail after injection: " + ", ".join(map(str, target_tests[:8])) + ".")
    if clean_p2p:
        base.append("Protected P2P tests that should stay green: " + ", ".join(map(str, clean_p2p[:12])) + ".")
    if p2p_failed:
        base.append("Previously broken P2P tests to avoid breaking again: " + ", ".join(map(str, p2p_failed[:8])) + ".")
    return " ".join(base)


def repo_history(
    injections: dict[str, dict[str, Any]],
    verifications: list[dict[str, Any]],
) -> dict[str, Counter[str]]:
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for verification_row in verifications:
        iid = row_id(verification_row)
        injection = injections.get(iid)
        if not injection:
            continue
        repo = repo_of(injection) or repo_of(verification_row)
        out[repo][verification_reason(injection, verification_row)] += 1
    return out


def repo_quality(repo: str, history: dict[str, Counter[str]]) -> float:
    counts = history.get(repo) or Counter()
    total = sum(counts.values())
    strict = counts.get("strict_ok", 0)
    p2f = counts.get("p2f_miss", 0)
    p2p = counts.get("p2p_buggy_regression", 0)
    # Smoothed, with explicit penalties for the two observed bottlenecks.
    return ((strict + 1.0) / (total + 4.0)) - 0.08 * p2f - 0.12 * p2p


def shape_score(row: dict[str, Any]) -> float:
    profile = patch_profile(str(row.get("patch") or ""))
    fail_to_pass = coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    pass_to_pass = coerce_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    score = 0.0
    if 1 <= len(fail_to_pass) <= 4:
        score += 1.0
    if 1 <= len(pass_to_pass) <= 40:
        score += 0.6
    if 8 <= profile.line_changes <= 180:
        score += 0.8
    if 2 <= profile.hunks <= 18:
        score += 0.8
    if 1 <= profile.source_files <= 5:
        score += 0.7
    if profile.line_changes > 260:
        score -= 0.9
    if profile.hunks > 30:
        score -= 0.8
    if profile.source_files > 8:
        score -= 0.7
    return score


def passes_shape_bounds(
    row: dict[str, Any],
    min_line_changes: int,
    max_line_changes: int,
    min_hunks: int,
    max_hunks: int,
    max_files: int,
    max_fail_to_pass: int,
    max_pass_to_pass: int,
) -> bool:
    profile = patch_profile(str(row.get("patch") or ""))
    fail_to_pass = coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    pass_to_pass = coerce_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    if profile.line_changes < min_line_changes or profile.line_changes > max_line_changes:
        return False
    if profile.hunks < min_hunks or profile.hunks > max_hunks:
        return False
    if profile.source_files < 1 or profile.source_files > max_files:
        return False
    if max_fail_to_pass and len(fail_to_pass) > max_fail_to_pass:
        return False
    if max_pass_to_pass and len(pass_to_pass) > max_pass_to_pass:
        return False
    return True


def round_robin(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], deque[dict[str, Any]]] = defaultdict(deque)
    for row in rows:
        buckets[(dataset_of(row), repo_of(row))].append(row)
    for key, bucket in list(buckets.items()):
        buckets[key] = deque(sorted(bucket, key=lambda row: -float(row.get("targeted_pool_score") or 0)))
    keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key[0], key[1]))
    out: list[dict[str, Any]] = []
    while keys and (not limit or len(out) < limit):
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            bucket = buckets[key]
            if bucket and (not limit or len(out) < limit):
                out.append(bucket.popleft())
            if bucket:
                next_keys.append(key)
        keys = next_keys
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected-existing", required=True)
    parser.add_argument("--candidate-root", action="append", required=True)
    parser.add_argument("--run-dir", action="append", required=True)
    parser.add_argument("--include-dataset", action="append", default=[
        "princeton-nlp/SWE-bench_Verified",
        "ScaleAI/SWE-bench_Pro",
        "SWE-bench-Live/SWE-bench-Live",
        "SWE-bench/SWE-bench_Lite",
    ])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--fresh-limit", type=int, default=260)
    parser.add_argument("--retry-limit", type=int, default=100)
    parser.add_argument("--min-line-changes", type=int, default=8)
    parser.add_argument("--max-line-changes", type=int, default=260)
    parser.add_argument("--min-hunks", type=int, default=1)
    parser.add_argument("--max-hunks", type=int, default=30)
    parser.add_argument("--max-files", type=int, default=8)
    parser.add_argument("--max-fail-to-pass", type=int, default=8)
    parser.add_argument("--max-pass-to-pass", type=int, default=60)
    args = parser.parse_args()

    include_datasets = set(args.include_dataset)
    selected = read_jsonl(Path(args.selected_existing))
    selected_ids = {row_id(row) for row in selected if row_id(row)}
    selected_by_dataset = Counter(dataset_of(row) for row in selected)
    deficits = {
        dataset: max(0, DEFAULT_QUOTAS.get(dataset, 0) - selected_by_dataset.get(dataset, 0))
        for dataset in include_datasets
    }

    run_dirs = [Path(path) for path in args.run_dir]
    injections = load_injections(run_dirs)
    verifications = load_verifications(run_dirs)
    history = repo_history(injections, verifications)
    candidate_paths = candidate_paths_from_roots([Path(path) for path in args.candidate_root])
    candidates = load_candidates(candidate_paths)
    already_processed = processed_ids(run_dirs)

    retry_rows: list[dict[str, Any]] = []
    retry_seen: set[str] = set()
    for verification_row in verifications:
        iid = row_id(verification_row)
        injection = injections.get(iid)
        candidate = candidates.get(iid)
        if not iid or not injection or not candidate or iid in selected_ids or iid in retry_seen:
            continue
        if dataset_of(candidate) not in include_datasets:
            continue
        reason = verification_reason(injection, verification_row)
        if reason not in {"p2f_miss", "p2p_buggy_regression", "p2p_repaired_not_pass"}:
            continue
        copied = dict(candidate)
        copied["v2_construction_source"] = "targeted_l3_feedback_retry"
        copied["v2_previous_failure_reason"] = reason
        copied["v2_retry_feedback_prompt"] = feedback_prompt(reason, injection, verification_row)
        copied["targeted_pool_score"] = (
            4.0
            + (deficits.get(dataset_of(copied), 0) / 125.0)
            + repo_quality(repo_of(copied), history)
            + shape_score(copied)
        )
        retry_rows.append(copied)
        retry_seen.add(iid)
    retry_rows = round_robin(retry_rows, args.retry_limit)

    retry_ids = {row_id(row) for row in retry_rows}
    fresh_rows: list[dict[str, Any]] = []
    for iid, row in candidates.items():
        if (
            not iid
            or iid in selected_ids
            or iid in already_processed
            or iid in retry_ids
            or dataset_of(row) not in include_datasets
        ):
            continue
        if not passes_shape_bounds(
            row,
            args.min_line_changes,
            args.max_line_changes,
            args.min_hunks,
            args.max_hunks,
            args.max_files,
            args.max_fail_to_pass,
            args.max_pass_to_pass,
        ):
            continue
        copied = dict(row)
        copied["v2_construction_source"] = "targeted_high_yield_fresh"
        copied["targeted_pool_score"] = (
            6.0 * (deficits.get(dataset_of(copied), 0) / 125.0)
            + repo_quality(repo_of(copied), history)
            + shape_score(copied)
        )
        fresh_rows.append(copied)
    fresh_rows = round_robin(fresh_rows, args.fresh_limit)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(output_dir / "feedback_retry_candidates.jsonl", retry_rows)
    write_jsonl(output_dir / "fresh_candidates.jsonl", fresh_rows)
    write_jsonl(output_dir / "combined_candidates.jsonl", retry_rows + fresh_rows)
    summary = {
        "selected_existing": len(selected),
        "selected_by_dataset": dict(selected_by_dataset),
        "deficits": deficits,
        "candidate_files": [str(path) for path in candidate_paths],
        "include_datasets": sorted(include_datasets),
        "recent_verification_rows": len(verifications),
        "recent_injection_rows": len(injections),
        "recent_failure_reasons": dict(Counter(
            verification_reason(injections.get(row_id(row), {}), row)
            for row in verifications
            if row_id(row) in injections
        )),
        "repo_history_top": {
            repo: dict(counts)
            for repo, counts in sorted(
                history.items(),
                key=lambda item: (-(item[1].get("strict_ok", 0)), item[0]),
            )[:30]
        },
        "processed_ids_excluded_from_fresh": len(already_processed),
        "retry_rows": len(retry_rows),
        "retry_by_dataset": dict(Counter(dataset_of(row) for row in retry_rows)),
        "retry_by_reason": dict(Counter(str(row.get("v2_previous_failure_reason")) for row in retry_rows)),
        "retry_by_repo_top30": dict(Counter(repo_of(row) for row in retry_rows).most_common(30)),
        "fresh_rows": len(fresh_rows),
        "fresh_by_dataset": dict(Counter(dataset_of(row) for row in fresh_rows)),
        "fresh_by_repo_top30": dict(Counter(repo_of(row) for row in fresh_rows).most_common(30)),
        "shape_bounds": {
            "min_line_changes": args.min_line_changes,
            "max_line_changes": args.max_line_changes,
            "min_hunks": args.min_hunks,
            "max_hunks": args.max_hunks,
            "max_files": args.max_files,
            "max_fail_to_pass": args.max_fail_to_pass,
            "max_pass_to_pass": args.max_pass_to_pass,
        },
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
