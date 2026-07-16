"""Assemble a balanced PR-INJECTOR v2 B500 from locked and new construction rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from prinjector_v2_metrics import (
    FidelityGateConfig,
    complexity_bin,
    evaluate_patch_pair_fidelity,
    patch_profile,
    read_jsonl,
    resolve_text,
    write_jsonl,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUOTAS = {
    "princeton-nlp/SWE-bench": 250,
    "princeton-nlp/SWE-bench_Verified": 125,
    "ScaleAI/SWE-bench_Pro": 125,
}


def workspace_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(path)


def row_id(row: dict[str, Any]) -> str:
    return str(
        row.get("A_instance_id")
        or row.get("source_instance_id")
        or row.get("B_source_instance_id")
        or row.get("instance_id")
        or ""
    )


def read_candidate_index(paths: list[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row_id(row)
            if iid and iid not in index:
                index[iid] = row
    return index


def read_injection_index(run_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl")):
            for row in read_jsonl(path):
                iid = row_id(row)
                if iid:
                    copied = dict(row)
                    copied["construction_injection_source"] = workspace_path(path)
                    index[iid] = copied
    return index


def verification_rows(run_dirs: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        for path in sorted(run_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl")):
            for row in read_jsonl(path):
                copied = dict(row)
                copied["construction_verification_source"] = workspace_path(path)
                rows.append(copied)
    return rows


def diff_hash(text: str) -> str:
    normalized = "\n".join(line for line in text.splitlines() if not line.startswith("index "))
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()


def reverse_unified_diff(diff_text: str) -> str:
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        if body.startswith("diff --git "):
            parts = body.split()
            if len(parts) >= 4:
                out.append(f"diff --git {parts[3]} {parts[2]}{newline}")
            else:
                out.append(line)
        elif body.startswith("--- "):
            out.append("+++ " + body[4:] + newline)
        elif body.startswith("+++ "):
            out.append("--- " + body[4:] + newline)
        elif body.startswith("+") and not body.startswith("+++"):
            out.append("-" + body[1:] + newline)
        elif body.startswith("-") and not body.startswith("---"):
            out.append("+" + body[1:] + newline)
        else:
            out.append(line)
    return "".join(out)


def strict_and_v2_ok(
    verification_row: dict[str, Any],
    injection: dict[str, Any],
    candidate: dict[str, Any],
    config: FidelityGateConfig,
) -> tuple[bool, str, dict[str, Any]]:
    verification = verification_row.get("verification") or {}
    if not injection.get("success"):
        return False, "injection_not_success", {}
    if not verification.get("pass_to_fail"):
        return False, "p2f_miss", {}
    if verification.get("golden_repair_pass") is not True:
        return False, "golden_repair_not_pass", {}
    if int(verification.get("p2p_buggy_failed") or 0) != 0:
        return False, "p2p_buggy_regression", {}
    if verification.get("p2p_repaired_pass") is not True:
        return False, "p2p_repaired_not_pass", {}

    diff_text = resolve_text(injection.get("injected_diff", ""), ROOT)
    if not diff_text.strip():
        return False, "diff_missing", {}
    b_profile = patch_profile(diff_text)
    if b_profile.test_files:
        return False, "diff_touches_tests", {}

    clean_p2p = verification.get("clean_pass_to_pass") or injection.get("B_PASS_TO_PASS_CLEAN") or []
    gate = evaluate_patch_pair_fidelity(
        a_patch=str(candidate.get("patch") or injection.get("patch") or ""),
        b_patch=diff_text,
        a_fail_to_pass=candidate.get("fail_to_pass") or candidate.get("FAIL_TO_PASS") or [],
        b_fail_to_pass=verification.get("actual_failed_tests") or injection.get("fail_to_pass") or [],
        a_pass_to_pass=candidate.get("pass_to_pass") or candidate.get("PASS_TO_PASS") or [],
        b_pass_to_pass=clean_p2p,
        injection_level=str(injection.get("injection_level") or ""),
        config=config,
    )
    gate["stage"] = "v2_final_assembly"
    if not gate.get("pass_gate"):
        return False, "v2_fidelity_gate_failed", gate
    return True, "strict_v2_ok", gate


def merged_new_row(
    verification_row: dict[str, Any],
    injection: dict[str, Any],
    candidate: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    verification = verification_row.get("verification") or {}
    fail_to_pass = (
        verification.get("actual_failed_tests")
        or injection.get("fail_to_pass")
        or candidate.get("fail_to_pass")
        or candidate.get("FAIL_TO_PASS")
        or []
    )
    pass_to_pass = (
        verification.get("clean_pass_to_pass")
        or injection.get("B_PASS_TO_PASS_CLEAN")
        or candidate.get("pass_to_pass")
        or candidate.get("PASS_TO_PASS")
        or []
    )
    row = dict(candidate)
    row.update(injection)
    row.update({
        "success": True,
        "source_dataset": candidate.get("source_dataset") or injection.get("source_dataset"),
        "source_instance_id": candidate.get("source_instance_id") or candidate.get("instance_id") or row_id(candidate),
        "repo": candidate.get("repo") or injection.get("repo"),
        "patch": candidate.get("patch", ""),
        "test_patch": candidate.get("test_patch", ""),
        "problem_statement": candidate.get("problem_statement", ""),
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "FAIL_TO_PASS": fail_to_pass,
        "PASS_TO_PASS": pass_to_pass,
        "B_PASS_TO_PASS_CLEAN": pass_to_pass,
        "verification": verification,
        "verification_source": verification_row.get("construction_verification_source", ""),
        "v2_fidelity_gate_final": gate,
        "v2_fidelity_gate_pass_final": True,
        "complexity_profile": {
            "A_profile": gate.get("A_profile") or {},
            "B_profile": gate.get("B_profile") or {},
            "ratios": gate.get("ratios") or {},
            "v2_score": gate.get("score"),
            "v2_tags": gate.get("tags") or [],
        },
        "v2_final_source": "new_construction",
    })
    return row


def locked_gate_from_audit(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": "v2_locked_existing_audit",
        "score": row.get("v2_score"),
        "pass_gate": row.get("v2_pass_gate"),
        "tags": row.get("v2_tags") or [],
        "reasons": row.get("v2_reasons") or [],
        "ratios": row.get("v2_ratios") or {},
        "a_profile": {
            "files": row.get("A_patch_files"),
            "source_files": row.get("A_patch_source_files"),
            "test_files": row.get("A_patch_test_files"),
            "hunks": row.get("A_patch_hunks"),
            "added": row.get("A_patch_added"),
            "removed": row.get("A_patch_removed"),
            "line_changes": row.get("A_patch_line_changes"),
            "symbols": row.get("A_patch_symbols"),
            "fail_to_pass_count": row.get("A_FAIL_TO_PASS_count"),
            "pass_to_pass_count": row.get("A_PASS_TO_PASS_count"),
            "complexity_bin": row.get("A_complexity_bin"),
        },
        "b_profile": {
            "files": row.get("B_patch_files"),
            "source_files": row.get("B_patch_source_files"),
            "test_files": row.get("B_patch_test_files"),
            "hunks": row.get("B_patch_hunks"),
            "added": row.get("B_patch_added"),
            "removed": row.get("B_patch_removed"),
            "line_changes": row.get("B_patch_line_changes"),
            "symbols": row.get("B_patch_symbols"),
            "fail_to_pass_count": row.get("B_FAIL_TO_PASS_count"),
            "pass_to_pass_count": row.get("B_PASS_TO_PASS_count"),
            "complexity_bin": row.get("B_complexity_bin"),
        },
    }


def selected_locked_rows(selection_rows: list[dict[str, Any]], old_b_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected_index = {row_id(row): row for row in selection_rows if row_id(row)}
    old_index = {row_id(row): row for row in old_b_rows if row_id(row)}
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for iid, audit_row in selected_index.items():
        if audit_row.get("verification") and audit_row.get("injected_diff"):
            row = audit_row
        else:
            row = old_index.get(iid)
        if not row:
            missing.append(iid)
            continue
        copied = dict(row)
        copied["v2_final_source"] = "locked_existing_v2_gate_pass"
        audit_source = audit_row.get("v2_selection_audit") or audit_row
        copied["A_instance_id"] = audit_source.get("A_instance_id") or audit_row.get("A_instance_id") or iid
        copied["B_instance_id"] = audit_source.get("B_instance_id") or audit_row.get("B_instance_id") or copied.get("instance_id")
        copied["v2_selection_audit"] = audit_source
        copied["v2_fidelity_gate_final"] = (
            copied.get("v2_fidelity_gate_final")
            or copied.get("v2_fidelity_gate")
            or locked_gate_from_audit(audit_source)
        )
        copied["v2_fidelity_gate_pass_final"] = bool(
            copied.get("v2_fidelity_gate_pass_final")
            or copied.get("v2_fidelity_gate_pass")
            or audit_source.get("v2_pass_gate")
        )
        copied["v2_fidelity_gate"] = copied["v2_fidelity_gate_final"]
        copied["v2_fidelity_gate_pass"] = copied["v2_fidelity_gate_pass_final"]
        copied["complexity_profile"] = {
            "A_complexity_bin": audit_source.get("A_complexity_bin"),
            "B_complexity_bin": audit_source.get("B_complexity_bin"),
            "A_patch_line_changes": audit_source.get("A_patch_line_changes"),
            "B_patch_line_changes": audit_source.get("B_patch_line_changes"),
            "A_patch_hunks": audit_source.get("A_patch_hunks"),
            "B_patch_hunks": audit_source.get("B_patch_hunks"),
            "A_patch_source_files": audit_source.get("A_patch_source_files"),
            "B_patch_source_files": audit_source.get("B_patch_source_files"),
            "v2_score": audit_source.get("v2_score"),
            "v2_tags": audit_source.get("v2_tags") or [],
        }
        rows.append(copied)
    if missing:
        raise SystemExit(f"missing locked old B rows: {missing[:10]} total={len(missing)}")
    return rows


def row_rank(row: dict[str, Any], repo_counts: Counter[str], dataset_counts: Counter[str]) -> tuple[Any, ...]:
    diff_text = resolve_text(row.get("injected_diff", ""), ROOT)
    b_profile = patch_profile(diff_text)
    a_profile = patch_profile(str(row.get("patch") or ""))
    return (
        repo_counts[str(row.get("repo") or "")],
        dataset_counts[str(row.get("source_dataset") or "")],
        complexity_bin(a_profile.line_changes),
        -b_profile.source_files,
        -b_profile.hunks,
        -b_profile.line_changes,
        row_id(row),
    )


def select_final(
    locked: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    quotas: dict[str, int],
    repo_cap: int,
    allow_repo_cap_replacement: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = list(locked)
    selected_ids = {row_id(row) for row in selected}
    repo_counts = Counter(str(row.get("repo") or "") for row in selected)
    dataset_counts = Counter(str(row.get("source_dataset") or "") for row in selected)

    by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in new_rows:
        iid = row_id(row)
        dataset = str(row.get("source_dataset") or "")
        if not iid or iid in selected_ids or dataset not in quotas:
            continue
        by_dataset[dataset].append(row)

    changed = True
    while changed:
        changed = False
        for dataset, quota in quotas.items():
            if dataset_counts[dataset] >= quota:
                continue
            candidates = by_dataset[dataset]
            candidates.sort(key=lambda row: row_rank(row, repo_counts, dataset_counts))
            picked_idx = None
            for idx, row in enumerate(candidates):
                if row_id(row) in selected_ids:
                    continue
                repo = str(row.get("repo") or "")
                if repo_counts[repo] < repo_cap:
                    picked_idx = idx
                    break
            if picked_idx is None:
                if allow_repo_cap_replacement:
                    replacement_idx: int | None = None
                    replacement_score: tuple[float, str, str] | None = None
                    for candidate_idx, row in enumerate(candidates):
                        if row_id(row) in selected_ids:
                            continue
                        repo = str(row.get("repo") or "")
                        if repo_counts[repo] < repo_cap:
                            continue
                        new_dataset = str(row.get("source_dataset") or "")
                        new_ratio = dataset_counts[new_dataset] / max(quotas.get(new_dataset, 1), 1)
                        for selected_idx, selected_row in enumerate(selected):
                            if str(selected_row.get("repo") or "") != repo:
                                continue
                            old_dataset = str(selected_row.get("source_dataset") or "")
                            if old_dataset == new_dataset or old_dataset not in quotas:
                                continue
                            old_ratio = dataset_counts[old_dataset] / max(quotas.get(old_dataset, 1), 1)
                            if old_ratio <= new_ratio:
                                continue
                            score = (old_ratio - new_ratio, old_dataset, row_id(selected_row))
                            if replacement_score is None or score > replacement_score:
                                replacement_score = score
                                replacement_idx = selected_idx
                                picked_idx = candidate_idx
                    if replacement_idx is not None and picked_idx is not None:
                        picked = candidates.pop(picked_idx)
                        replaced = selected[replacement_idx]
                        replaced_id = row_id(replaced)
                        picked_id = row_id(picked)
                        selected[replacement_idx] = picked
                        selected_ids.discard(replaced_id)
                        selected_ids.add(picked_id)
                        old_dataset = str(replaced.get("source_dataset") or "")
                        new_dataset = str(picked.get("source_dataset") or "")
                        dataset_counts[old_dataset] -= 1
                        dataset_counts[new_dataset] += 1
                        picked["v2_repo_cap_replacement"] = {
                            "replaced_source_instance_id": replaced_id,
                            "replaced_source_dataset": old_dataset,
                            "replacement_reason": "improve_dataset_balance_within_repo_cap",
                        }
                        changed = True
                continue
            picked = candidates.pop(picked_idx)
            iid = row_id(picked)
            selected.append(picked)
            selected_ids.add(iid)
            repo_counts[str(picked.get("repo") or "")] += 1
            dataset_counts[dataset] += 1
            changed = True
    return selected


def copy_diff_assets(rows: list[dict[str, Any]], final_dir: Path) -> None:
    diff_dir = final_dir / "injected_diffs"
    golden_dir = final_dir / "golden_patches"
    diff_dir.mkdir(parents=True, exist_ok=True)
    golden_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        src_rel = str(row.get("injected_diff") or "")
        src = ROOT / src_rel
        if not src.exists():
            src = Path(src_rel)
        if not src.exists():
            continue
        dst = diff_dir / f"{row_id(row)}.diff"
        if src.resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        row["injected_diff"] = workspace_path(dst)
        diff_text = dst.read_text(encoding="utf-8", errors="replace")
        golden = reverse_unified_diff(diff_text)
        golden_dst = golden_dir / f"{row_id(row)}.diff"
        golden_dst.write_text(golden, encoding="utf-8")
        row["golden_patch"] = golden
        row["golden_patch_file"] = workspace_path(golden_dst)


def write_dataset_slices(final_dir: Path, rows: list[dict[str, Any]]) -> None:
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        dataset = str(row.get("source_dataset") or "unknown")
        if "Pro" in dataset:
            slug = "pro"
        elif "Verified" in dataset:
            slug = "verified"
        elif "SWE-bench" in dataset:
            slug = "swebench"
        else:
            slug = dataset.lower().replace("/", "_")
        by_slug[slug].append(row)
    for slug, dataset_rows in by_slug.items():
        write_jsonl(final_dir / f"{slug}.jsonl", dataset_rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locked-selection", required=True)
    parser.add_argument("--old-b", required=True)
    parser.add_argument("--construction-run-dir", action="append", required=True)
    parser.add_argument("--candidate-file", action="append", required=True)
    parser.add_argument("--final-dir", required=True)
    parser.add_argument("--repo-cap", type=int, default=50)
    parser.add_argument("--target-size", type=int, default=500)
    parser.add_argument("--v2-min-score", type=float, default=0.65)
    parser.add_argument("--allow-repo-cap-replacement", action="store_true")
    args = parser.parse_args()

    final_dir = Path(args.final_dir)
    final_dir.mkdir(parents=True, exist_ok=True)
    construction_run_dirs = [Path(path) for path in args.construction_run_dir]
    config = FidelityGateConfig(min_score=args.v2_min_score)

    locked = selected_locked_rows(
        read_jsonl(Path(args.locked_selection)),
        read_jsonl(Path(args.old_b)),
    )
    candidates = read_candidate_index([Path(path) for path in args.candidate_file])
    injections = read_injection_index(construction_run_dirs)

    accepted_new_by_id: dict[str, dict[str, Any]] = {}
    rejects: Counter[str] = Counter()
    for verification_row in verification_rows(construction_run_dirs):
        iid = row_id(verification_row)
        injection = injections.get(iid)
        candidate = candidates.get(iid)
        if not injection:
            rejects["missing_injection"] += 1
            continue
        if not candidate:
            rejects["missing_candidate"] += 1
            continue
        ok, reason, gate = strict_and_v2_ok(verification_row, injection, candidate, config)
        if not ok:
            rejects[reason] += 1
            continue
        accepted_new_by_id[iid] = merged_new_row(verification_row, injection, candidate, gate)

    accepted_new = list(accepted_new_by_id.values())
    selected = select_final(
        locked,
        accepted_new,
        DEFAULT_QUOTAS,
        args.repo_cap,
        allow_repo_cap_replacement=args.allow_repo_cap_replacement,
    )
    if len(selected) < args.target_size:
        write_jsonl(final_dir / "partial_selected.jsonl", selected)
        write_jsonl(final_dir / "accepted_new.jsonl", accepted_new)
        summary = {
            "status": "insufficient_rows",
            "selected": len(selected),
            "target_size": args.target_size,
            "locked": len(locked),
            "accepted_new": len(accepted_new),
            "accepted_new_by_dataset": dict(Counter(str(row.get("source_dataset") or "") for row in accepted_new)),
            "selected_by_dataset": dict(Counter(str(row.get("source_dataset") or "") for row in selected)),
            "selected_by_repo_top": dict(Counter(str(row.get("repo") or "") for row in selected).most_common(30)),
            "repo_cap_replacements": sum(1 for row in selected if row.get("v2_repo_cap_replacement")),
            "rejects": dict(rejects),
        }
        (final_dir / "assembly_failed_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise SystemExit(2)

    selected = selected[: args.target_size]
    copy_diff_assets(selected, final_dir)
    for idx, row in enumerate(selected, 1):
        row["pr_number"] = idx
        if not row.get("case_id"):
            row["case_id"] = f"V2_SELECTED_{idx:03d}"
        row["benchmark_group"] = "B_injected_v2_balanced_500"
        row["injected_diff_hash"] = diff_hash(resolve_text(row.get("injected_diff", ""), ROOT))

    write_jsonl(final_dir / "injection_results.jsonl", selected)
    write_jsonl(final_dir / "selected.jsonl", selected)
    write_dataset_slices(final_dir, selected)
    summary = {
        "status": "ok",
        "final_dir": str(final_dir),
        "selected": len(selected),
        "locked": len(locked),
        "accepted_new": len(accepted_new),
        "accepted_new_by_dataset": dict(Counter(str(row.get("source_dataset") or "") for row in accepted_new)),
        "selected_by_dataset": dict(Counter(str(row.get("source_dataset") or "") for row in selected)),
        "selected_by_repo_top": dict(Counter(str(row.get("repo") or "") for row in selected).most_common(30)),
        "selected_by_source": dict(Counter(str(row.get("v2_final_source") or "") for row in selected)),
        "repo_cap_replacements": sum(1 for row in selected if row.get("v2_repo_cap_replacement")),
        "rejects": dict(rejects),
    }
    (final_dir / "assembly_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_rq2_pairing_table.py"), "--final-dir", str(final_dir)],
        cwd=ROOT,
        check=True,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
