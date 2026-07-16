"""Build a strict, diverse 300-case RQ2 B benchmark from construction runs.

This is a post-processing helper only. It does not run injection, verification,
or any model call. It selects verified PR-INJECTOR candidates, optionally
accepting gated Level 3 rows, and writes final A/B pairing assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

from prinjector_v2_metrics import FidelityGateConfig, evaluate_patch_pair_fidelity

ROOT = Path(__file__).resolve().parent.parent
RQ2_100_FINAL = ROOT / "experiments" / "rq2_100" / "rq2_b_l1_l2_original_100_final_20260605"
RQ2_300 = ROOT / "experiments" / "rq2_300"

TEST_FILE_PATTERNS = (
    "test/",
    "tests/",
    "testing/",
    "test_",
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" #\t")
        if line:
            return line[:180]
    return ""


def dataset_short(source_dataset: str) -> str:
    if "Pro" in source_dataset:
        return "PRO"
    if "Verified" in source_dataset:
        return "VERIFIED"
    return re.sub(r"[^A-Za-z0-9]+", "_", source_dataset).strip("_").upper()


def test_files_from_nodeids(nodeids: list[str]) -> list[str]:
    files: list[str] = []
    for nodeid in nodeids:
        path = str(nodeid).split("::", 1)[0]
        if path and path not in files:
            files.append(path)
    return files


def diff_text(row: dict) -> str:
    rel = row.get("injected_diff") or ""
    if not rel:
        return ""
    path = ROOT / rel
    if not path.exists():
        path = Path(rel)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def diff_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        match = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
        if match:
            files.append(match.group(2))
    return files


def diff_hash(diff: str) -> str:
    normalized = "\n".join(line for line in diff.splitlines() if not line.startswith("index "))
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()


def touches_test_file(paths: list[str]) -> bool:
    for path in paths:
        normalized = path.replace("\\", "/")
        name = Path(normalized).name
        if normalized.startswith(("test/", "tests/", "testing/")):
            return True
        if "/test/" in normalized or "/tests/" in normalized or "/testing/" in normalized:
            return True
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            return True
    return False


def complexity(row: dict, diff: str) -> dict:
    files = diff_files(diff)
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    hunks = sum(1 for line in diff.splitlines() if line.startswith("@@ "))
    original_patch = row.get("patch", "")
    original_files = len(set(diff_files(original_patch)))
    return {
        "diff_files": len(set(files)),
        "diff_hunks": hunks,
        "diff_added": added,
        "diff_removed": removed,
        "diff_line_changes": added + removed,
        "original_patch_files": original_files,
        "score": (
            min(len(set(files)), 5) * 8
            + min(hunks, 8) * 3
            + min(added + removed, 80) / 8
            + min(original_files, 5) * 4
        ),
    }


def diff_profile(diff: str) -> dict:
    files = diff_files(diff)
    source_files = [path for path in files if not touches_test_file([path])]
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    hunks = sum(1 for line in diff.splitlines() if line.startswith("@@ "))
    hunk_sections = [
        line.rsplit("@@", 1)[-1].strip()
        for line in diff.splitlines()
        if line.startswith("@@ ")
    ]
    return {
        "files": len(set(files)),
        "source_files": len(set(source_files)),
        "test_files": len(set(files)) - len(set(source_files)),
        "hunks": hunks,
        "added": added,
        "removed": removed,
        "line_changes": added + removed,
        "hunk_sections": len(set(section for section in hunk_sections if section)),
        "modules": len(set(str(Path(path).parent) for path in source_files)),
    }


def fidelity_assessment(candidate: dict, injection: dict, injected_diff: str, args: argparse.Namespace) -> dict:
    original = diff_profile(candidate.get("patch", "") or injection.get("patch", ""))
    injected = diff_profile(injected_diff)
    original_lines = max(int(original["line_changes"] or 0), 1)
    original_hunks = max(int(original["hunks"] or 0), 1)
    line_ratio = injected["line_changes"] / original_lines
    hunk_ratio = injected["hunks"] / original_hunks
    file_ratio = injected["source_files"] / max(int(original["source_files"] or 0), 1)
    tags: list[str] = []
    reasons: list[str] = []

    if original["source_files"] >= 2 and injected["source_files"] <= 1:
        tags.append("localized_simplified")
        reasons.append("source_file_count_collapsed")
    if original["hunks"] >= 3 and hunk_ratio < args.min_injected_to_original_hunk_ratio:
        tags.append("localized_simplified")
        reasons.append("hunk_count_collapsed")
    if original["line_changes"] >= 20 and line_ratio < args.min_injected_to_original_line_ratio:
        tags.append("over_simplified")
        reasons.append("line_change_count_collapsed")
    if original["modules"] >= 2 and injected["modules"] <= 1:
        tags.append("localized_simplified")
        reasons.append("module_count_collapsed")
    if injected["line_changes"] <= args.min_injected_line_changes and original["line_changes"] >= 12:
        tags.append("over_simplified")
        reasons.append("injected_diff_too_small")

    level = str(injection.get("injection_level", ""))
    l2_meta = injection.get("l2_metadata") or {}
    l3_meta = injection.get("l3_metadata") or {}
    if l2_meta.get("compatibility_flagged_files") or l2_meta.get("compatibility_rejected_files"):
        tags.append("api_drift")
        reasons.append("compatibility_flags")
    if level.startswith("Level_2") and l2_meta.get("function_replacements") and not l2_meta.get("hunk_replacements"):
        tags.append("localized_simplified")
        reasons.append("level2_whole_function_only")
    if level.startswith("Level_3") and l3_meta.get("simplification_risk"):
        tags.append("over_simplified")
        reasons.append("level3_marked_simplification_risk")

    if not tags:
        tags.append("faithful")
    return {
        "original": original,
        "injected": injected,
        "ratios": {
            "source_file_ratio": round(file_ratio, 4),
            "hunk_ratio": round(hunk_ratio, 4),
            "line_change_ratio": round(line_ratio, 4),
        },
        "tags": sorted(set(tags)),
        "reasons": sorted(set(reasons)),
    }


def v2_gate_config_from_args(args: argparse.Namespace) -> FidelityGateConfig:
    return FidelityGateConfig(
        min_score=args.v2_min_score,
        min_line_ratio=args.v2_min_line_ratio,
        max_line_ratio=args.v2_max_line_ratio,
        min_hunk_ratio=args.v2_min_hunk_ratio,
        min_file_ratio=args.v2_min_file_ratio,
        min_regression_ratio=args.v2_min_regression_ratio,
    )


def strict_ok(row: dict, injection: dict, args: argparse.Namespace, candidate: dict | None = None) -> tuple[bool, str]:
    verification = row.get("verification") or {}
    level = str(injection.get("injection_level", ""))
    if not injection.get("success"):
        return False, "injection_not_success"
    if level.startswith("Level_3") and not args.allow_level3:
        return False, "level3_disabled"
    if not verification.get("pass_to_fail"):
        return False, "p2f_miss"
    if verification.get("golden_repair_pass") is not True:
        return False, "golden_repair_not_pass"
    if int(verification.get("p2p_buggy_failed") or 0) != 0:
        return False, "p2p_buggy_regression"
    if verification.get("p2p_repaired_pass") is not True:
        return False, "p2p_repaired_not_pass"
    clean_p2p = verification.get("clean_pass_to_pass") or injection.get("B_PASS_TO_PASS_CLEAN") or []
    if len(clean_p2p) < args.min_clean_p2p:
        return False, "too_few_clean_p2p"
    l2_meta = injection.get("l2_metadata") or {}
    if level.startswith("Level_2"):
        if l2_meta.get("compatibility_rejected_files"):
            return False, "compatibility_rejected"
        if args.reject_compatibility_flags and l2_meta.get("compatibility_flagged_files"):
            return False, "compatibility_flagged"
        if args.reject_whole_function_level2 and l2_meta.get("function_replacements") and not l2_meta.get("hunk_replacements"):
            return False, "level2_whole_function_only"
    l3_meta = injection.get("l3_metadata") or {}
    if level.startswith("Level_3"):
        if l3_meta.get("disabled") or l3_meta.get("invalid_files"):
            return False, "level3_invalid_metadata"
        if float(l3_meta.get("confidence") or 0.0) < args.min_l3_confidence:
            return False, "level3_low_confidence"
    diff = diff_text(injection)
    if not diff.strip():
        return False, "diff_missing"
    fidelity = fidelity_assessment(candidate or injection, injection, diff, args)
    injection["_fidelity_assessment"] = fidelity
    if args.require_v2_fidelity_gate:
        source = candidate or injection
        v2_gate = evaluate_patch_pair_fidelity(
            a_patch=source.get("patch") or injection.get("patch", ""),
            b_patch=diff,
            a_fail_to_pass=source.get("fail_to_pass") or source.get("FAIL_TO_PASS") or [],
            b_fail_to_pass=verification.get("actual_failed_tests") or injection.get("fail_to_pass") or [],
            a_pass_to_pass=source.get("pass_to_pass") or source.get("PASS_TO_PASS") or [],
            b_pass_to_pass=clean_p2p,
            injection_level=level,
            config=v2_gate_config_from_args(args),
        )
        v2_gate["stage"] = "final_strict_verified"
        injection["_v2_fidelity_gate_final"] = v2_gate
        if not v2_gate.get("pass_gate"):
            return False, "v2_fidelity_gate_failed"
    if args.reject_over_simplified and "over_simplified" in fidelity["tags"]:
        return False, "over_simplified"
    if args.reject_localized_simplified and "localized_simplified" in fidelity["tags"]:
        return False, "localized_simplified"
    files = diff_files(diff)
    if touches_test_file(files):
        return False, "diff_touches_tests"
    if args.max_diff_files and len(set(files)) > args.max_diff_files:
        return False, "too_many_diff_files"
    if args.max_diff_line_changes:
        c = complexity(injection, diff)
        if c["diff_line_changes"] > args.max_diff_line_changes:
            return False, "too_many_diff_lines"
    return True, "strict_ok"


def load_candidate_meta(candidate_paths: list[Path]) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    for path in candidate_paths:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid:
                meta.setdefault(iid, {}).update(row)
    return meta


def collect_run_candidates(
    run_dir: Path,
    candidate_meta: dict[str, dict],
    group: str,
    source_dataset: str,
    args: argparse.Namespace,
) -> tuple[list[dict], Counter]:
    injections = {
        row["instance_id"]: row
        for row in read_jsonl(run_dir / f"{group}_injection_results.jsonl")
        if row.get("instance_id")
    }
    out: list[dict] = []
    rejects: Counter = Counter()
    for verification_row in read_jsonl(run_dir / f"{group}_verification_results.jsonl"):
        iid = verification_row.get("instance_id")
        if not iid:
            continue
        injection = injections.get(iid)
        if not injection:
            rejects["missing_injection"] += 1
            continue
        candidate = candidate_meta.get(iid, {})
        ok, reason = strict_ok(verification_row, injection, args, candidate)
        if not ok:
            rejects[reason] += 1
            continue
        verification = verification_row.get("verification") or {}
        fail_to_pass = (
            verification.get("actual_failed_tests")
            or injection.get("fail_to_pass")
            or candidate.get("fail_to_pass")
            or []
        )
        pass_to_pass = (
            verification.get("clean_pass_to_pass")
            or injection.get("B_PASS_TO_PASS_CLEAN")
            or candidate.get("pass_to_pass")
            or []
        )
        merged = dict(candidate)
        merged.update(injection)
        merged.update({
            "success": True,
            "source_dataset": candidate.get("source_dataset") or source_dataset,
            "source_instance_id": candidate.get("source_instance_id") or iid,
            "title": first_line(candidate.get("problem_statement", "")) or iid,
            "problem_statement": candidate.get("problem_statement", ""),
            "patch": candidate.get("patch", ""),
            "test_patch": candidate.get("test_patch", ""),
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "B_PASS_TO_PASS_CLEAN": pass_to_pass,
            "test_files": (
                candidate.get("selected_test_files_to_run")
                or test_files_from_nodeids(fail_to_pass)
            ),
            "verification": verification,
            "verification_source": f"{group}_verification_results.jsonl",
            "construction_run_dir": str(run_dir),
        })
        dtext = diff_text(merged)
        merged["injected_diff_hash"] = diff_hash(dtext)
        merged["complexity"] = complexity(merged, dtext)
        merged["fidelity"] = injection.get("_fidelity_assessment") or fidelity_assessment(candidate, merged, dtext, args)
        if injection.get("_v2_fidelity_gate_final"):
            merged["v2_fidelity_gate_final"] = injection["_v2_fidelity_gate_final"]
            merged["v2_fidelity_gate_pass_final"] = bool(
                injection["_v2_fidelity_gate_final"].get("pass_gate")
            )
        merged.pop("_fidelity_assessment", None)
        merged.pop("_v2_fidelity_gate_final", None)
        out.append(merged)
    return out, rejects


def load_seed_rows(seed_final_dir: Path, args: argparse.Namespace) -> list[dict]:
    rows: list[dict] = []
    for row in read_jsonl(seed_final_dir / "injection_results.jsonl"):
        fake_verification_row = {"verification": row.get("verification") or {}}
        ok, reason = strict_ok(fake_verification_row, row, args, row)
        if not ok:
            continue
        dtext = diff_text(row)
        row = dict(row)
        row["injected_diff_hash"] = diff_hash(dtext)
        row["complexity"] = complexity(row, dtext)
        row["fidelity"] = row.get("_fidelity_assessment") or fidelity_assessment(row, row, dtext, args)
        if row.get("_v2_fidelity_gate_final"):
            row["v2_fidelity_gate_final"] = row["_v2_fidelity_gate_final"]
            row["v2_fidelity_gate_pass_final"] = bool(
                row["_v2_fidelity_gate_final"].get("pass_gate")
            )
        row.pop("_fidelity_assessment", None)
        row.pop("_v2_fidelity_gate_final", None)
        row["construction_run_dir"] = str(seed_final_dir)
        rows.append(row)
    return rows


def select_diverse(
    rows: list[dict],
    limit: int,
    *,
    pinned_rows: list[dict] | None = None,
    max_repo_count: int = 0,
) -> list[dict]:
    def bucket(value: int | float, cuts: tuple[int, ...] = (1, 2, 4, 8, 16, 32, 64, 128)) -> str:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 0.0
        for cut in cuts:
            if v <= cut:
                return f"<= {cut}"
        return f"> {cuts[-1]}"

    def diversity_shape(row: dict) -> tuple[str, str, str]:
        c = row.get("complexity") or {}
        return (
            bucket(c.get("diff_files") or 0, (1, 2, 3, 5, 8)),
            bucket(c.get("diff_hunks") or 0, (1, 2, 4, 8, 16)),
            bucket(c.get("diff_line_changes") or 0, (4, 8, 16, 32, 64, 128)),
        )

    by_id: dict[str, dict] = {}
    for row in rows:
        iid = row.get("source_instance_id") or row.get("instance_id")
        if not iid:
            continue
        current = by_id.get(iid)
        if current is None or row["complexity"]["score"] > current["complexity"]["score"]:
            by_id[iid] = row

    candidates = list(by_id.values())
    selected: list[dict] = []
    seen_diff_hashes: set[str] = set()
    repo_counts: Counter = Counter()
    level_counts: Counter = Counter()
    dataset_counts: Counter = Counter()
    file_bucket_counts: Counter = Counter()
    hunk_bucket_counts: Counter = Counter()
    line_bucket_counts: Counter = Counter()

    for row in pinned_rows or []:
        iid = row.get("source_instance_id") or row.get("instance_id")
        if not iid or iid not in by_id:
            continue
        h = row.get("injected_diff_hash")
        if h and h in seen_diff_hashes:
            continue
        selected.append(row)
        repo_counts[row.get("repo", "")] += 1
        level_counts[str(row.get("injection_level", ""))] += 1
        dataset_counts[row.get("source_dataset", "")] += 1
        file_bucket, hunk_bucket, line_bucket = diversity_shape(row)
        file_bucket_counts[file_bucket] += 1
        hunk_bucket_counts[hunk_bucket] += 1
        line_bucket_counts[line_bucket] += 1
        if h:
            seen_diff_hashes.add(h)
        by_id.pop(iid, None)
        if len(selected) >= limit:
            return selected

    candidates = list(by_id.values())
    while candidates and len(selected) < limit:
        candidates.sort(
            key=lambda row: (
                dataset_counts[row.get("source_dataset", "")],
                repo_counts[row.get("repo", "")],
                level_counts[str(row.get("injection_level", ""))],
                file_bucket_counts[diversity_shape(row)[0]],
                hunk_bucket_counts[diversity_shape(row)[1]],
                line_bucket_counts[diversity_shape(row)[2]],
                -float(row.get("complexity", {}).get("score") or 0),
                row.get("source_instance_id", ""),
            )
        )
        picked = None
        for idx, row in enumerate(candidates):
            h = row.get("injected_diff_hash")
            if h and h in seen_diff_hashes:
                continue
            if max_repo_count and repo_counts[row.get("repo", "")] >= max_repo_count:
                continue
            picked = candidates.pop(idx)
            break
        if picked is None and max_repo_count:
            # If a strict cap makes the requested size impossible, relax it
            # only after exhausting all under-cap candidates. The audit file
            # still exposes the final repo distribution.
            for idx, row in enumerate(candidates):
                h = row.get("injected_diff_hash")
                if h and h in seen_diff_hashes:
                    continue
                picked = candidates.pop(idx)
                break
        if picked is None:
            break
        selected.append(picked)
        repo_counts[picked.get("repo", "")] += 1
        level_counts[str(picked.get("injection_level", ""))] += 1
        dataset_counts[picked.get("source_dataset", "")] += 1
        file_bucket, hunk_bucket, line_bucket = diversity_shape(picked)
        file_bucket_counts[file_bucket] += 1
        hunk_bucket_counts[hunk_bucket] += 1
        line_bucket_counts[line_bucket] += 1
        if picked.get("injected_diff_hash"):
            seen_diff_hashes.add(picked["injected_diff_hash"])

    return selected


def copy_diff_assets(rows: list[dict], final_dir: Path) -> None:
    diff_dir = final_dir / "injected_diffs"
    diff_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        src_rel = row.get("injected_diff") or ""
        src = ROOT / src_rel
        if not src.exists():
            src = Path(src_rel)
        if not src.exists():
            continue
        dst = diff_dir / f"{row['source_instance_id']}.diff"
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        row["injected_diff"] = str(dst.relative_to(ROOT))


def build_report(final_dir: Path) -> None:
    report = final_dir / "report.html"
    script = ROOT / "scripts" / "build_injection_report.py"
    if not script.exists():
        return
    subprocess.run(
        [sys.executable, str(script), str(final_dir)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", action="append", default=[])
    parser.add_argument("--candidate-dir", action="append", default=[str(RQ2_300)])
    parser.add_argument("--seed-final-dir", default=str(RQ2_100_FINAL))
    parser.add_argument("--final-dir", default=str(RQ2_300 / "rq2_b_300_final_20260608"))
    parser.add_argument("--limit-per-group", type=int, default=150)
    parser.add_argument("--total-limit", type=int, default=0,
                        help="Select this many strict cases across all source datasets instead of 150/150 Pro/Verified")
    parser.add_argument("--allow-level3", action="store_true")
    parser.add_argument("--min-l3-confidence", type=float, default=0.45)
    parser.add_argument("--min-clean-p2p", type=int, default=1)
    parser.add_argument("--reject-compatibility-flags", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--reject-whole-function-level2", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reject-localized-simplified", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--reject-over-simplified", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--min-injected-to-original-line-ratio", type=float, default=0.20)
    parser.add_argument("--min-injected-to-original-hunk-ratio", type=float, default=0.35)
    parser.add_argument("--min-injected-line-changes", type=int, default=4)
    parser.add_argument("--require-v2-fidelity-gate", action="store_true",
                        help="Require the verified A/B v2 complexity gate before final selection")
    parser.add_argument("--v2-min-score", type=float, default=0.65)
    parser.add_argument("--v2-min-line-ratio", type=float, default=0.50)
    parser.add_argument("--v2-max-line-ratio", type=float, default=2.50)
    parser.add_argument("--v2-min-hunk-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-file-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-regression-ratio", type=float, default=0.25)
    parser.add_argument("--max-diff-files", type=int, default=8)
    parser.add_argument("--max-diff-line-changes", type=int, default=600)
    parser.add_argument("--pin-seed", action="store_true",
                        help="Preserve all strict rows from --seed-final-dir before filling new cases")
    parser.add_argument("--max-repo-count", type=int, default=0,
                        help="Soft cap per repository while selecting additional rows")
    parser.add_argument("--no-seed", action="store_true")
    args = parser.parse_args()

    final_dir = Path(args.final_dir).resolve()
    candidate_paths: list[Path] = []
    for candidate_dir in args.candidate_dir:
        base = Path(candidate_dir)
        candidate_paths.extend(sorted(base.glob("candidate_pool*.jsonl")))
        candidate_paths.extend(sorted(base.glob("*candidates*.jsonl")))
    candidate_meta = load_candidate_meta(candidate_paths)

    all_rows: list[dict] = []
    seed_rows: list[dict] = []
    reject_summary: dict[str, dict] = {}
    if not args.no_seed:
        seed_rows = load_seed_rows(Path(args.seed_final_dir), args)
        all_rows.extend(seed_rows)

    for run_dir_arg in args.run_dir:
        run_dir = Path(run_dir_arg).resolve()
        for group, source_dataset in (
            ("pro", "ScaleAI/SWE-bench_Pro"),
            ("verified", "princeton-nlp/SWE-bench_Verified"),
        ):
            rows, rejects = collect_run_candidates(run_dir, candidate_meta, group, source_dataset, args)
            all_rows.extend(rows)
            reject_summary[f"{run_dir.name}:{group}"] = dict(rejects)

    if args.total_limit:
        selected = select_diverse(
            all_rows,
            args.total_limit,
            pinned_rows=seed_rows if args.pin_seed else None,
            max_repo_count=args.max_repo_count,
        )
        if len(selected) < args.total_limit:
            write_json(final_dir / "selection_failed_summary.json", {
                "selected": len(selected),
                "target_total": args.total_limit,
                "selected_by_dataset": dict(Counter(row.get("source_dataset") for row in selected)),
                "selected_by_repo": dict(Counter(row.get("repo") for row in selected)),
                "candidate_paths": [str(p) for p in candidate_paths],
                "run_dirs": args.run_dir,
                "reject_summary": reject_summary,
            })
            raise SystemExit(
                f"not enough strict candidates: selected={len(selected)}/{args.total_limit}"
            )
        pro = [r for r in selected if r.get("source_dataset") == "ScaleAI/SWE-bench_Pro"]
        verified = [
            r for r in selected
            if r.get("source_dataset") == "princeton-nlp/SWE-bench_Verified"
        ]
    else:
        pro = select_diverse(
            [r for r in all_rows if r.get("source_dataset") == "ScaleAI/SWE-bench_Pro"],
            args.limit_per_group,
            max_repo_count=args.max_repo_count,
        )
        verified = select_diverse(
            [r for r in all_rows if r.get("source_dataset") == "princeton-nlp/SWE-bench_Verified"],
            args.limit_per_group,
            max_repo_count=args.max_repo_count,
        )
        if len(pro) < args.limit_per_group or len(verified) < args.limit_per_group:
            write_json(final_dir / "selection_failed_summary.json", {
                "pro_selected": len(pro),
                "verified_selected": len(verified),
                "target_per_group": args.limit_per_group,
                "candidate_paths": [str(p) for p in candidate_paths],
                "run_dirs": args.run_dir,
                "reject_summary": reject_summary,
            })
            raise SystemExit(
                "not enough strict candidates: "
                f"pro={len(pro)}/{args.limit_per_group} "
                f"verified={len(verified)}/{args.limit_per_group}"
            )
        selected = pro + verified

    copy_diff_assets(selected, final_dir)
    for i, row in enumerate(selected, 1):
        row["pr_number"] = i
        row["benchmark_group"] = "B_injected_strict_300"

    write_jsonl(final_dir / "pro_150.jsonl", pro)
    write_jsonl(final_dir / "verified_150.jsonl", verified)
    by_dataset_slug = defaultdict(list)
    for row in selected:
        by_dataset_slug[dataset_short(row.get("source_dataset", "unknown")).lower()].append(row)
    for slug, dataset_rows in by_dataset_slug.items():
        write_jsonl(final_dir / f"{slug}.jsonl", dataset_rows)
    write_jsonl(final_dir / "selected.jsonl", selected)
    write_jsonl(final_dir / "injection_results.jsonl", selected)
    write_jsonl(final_dir / "sampled.jsonl", [
        {
            "pr_number": row["pr_number"],
            "patch": row.get("patch", ""),
            "html_url": row.get("source_instance_id", ""),
        }
        for row in selected
    ])

    audit = {
        "final_dir": str(final_dir),
        "total": len(selected),
        "pro": len(pro),
        "verified": len(verified),
        "by_dataset": dict(Counter(row.get("source_dataset") for row in selected)),
        "levels": dict(Counter(row.get("injection_level") for row in selected)),
        "repos": dict(Counter(row.get("repo") for row in selected)),
        "level3_rows": sum(str(row.get("injection_level", "")).startswith("Level_3") for row in selected),
        "fidelity_tags": dict(Counter(tag for row in selected for tag in row.get("fidelity", {}).get("tags", []))),
        "fidelity_reasons": dict(Counter(reason for row in selected for reason in row.get("fidelity", {}).get("reasons", []))),
        "require_v2_fidelity_gate": args.require_v2_fidelity_gate,
        "v2_gate_pass_final": sum(bool(row.get("v2_fidelity_gate_pass_final")) for row in selected),
        "v2_gate_tags_final": dict(Counter(
            tag
            for row in selected
            for tag in (row.get("v2_fidelity_gate_final") or {}).get("tags", [])
        )),
        "diff_file_count_histogram": dict(Counter(row["complexity"]["diff_files"] for row in selected)),
        "source_patch_file_count_histogram": dict(Counter(row["complexity"]["original_patch_files"] for row in selected)),
        "candidate_paths": [str(p) for p in candidate_paths],
        "run_dirs": args.run_dir,
        "reject_summary": reject_summary,
        "pin_seed": args.pin_seed,
        "pinned_seed_rows": len(seed_rows) if args.pin_seed else 0,
        "max_repo_count": args.max_repo_count,
    }
    write_json(final_dir / "final_audit.json", audit)

    # Reuse the existing pairing builder. It writes A/B JSONL and golden patches.
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_rq2_pairing_table.py"), "--final-dir", str(final_dir)],
        cwd=str(ROOT),
        check=True,
    )
    build_report(final_dir)
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
