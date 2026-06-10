#!/usr/bin/env python3
"""Audit a finalized RQ2 PR-INJECTOR benchmark set.

This script checks hard construction invariants and summarizes diversity.
It is intentionally read-only: it does not run injection, verification, or
agent evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def source_id(row: dict) -> str:
    return str(row.get("source_instance_id") or row.get("instance_id") or row.get("id") or "")


def diff_path(row: dict, final_dir: Path) -> Path | None:
    rel = row.get("injected_diff") or row.get("diff_path") or row.get("injected_diff_path")
    if not rel:
        return None
    path = Path(str(rel))
    if path.is_absolute() and path.exists():
        return path
    candidates = [final_dir / path, ROOT / path, path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def diff_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        match = re.match(r"^diff --git a/(.*?) b/(.*)$", line)
        if match:
            files.append(match.group(2))
    return files


def touches_test_file(files: list[str]) -> bool:
    for path in files:
        normalized = path.replace("\\", "/")
        name = Path(normalized).name
        if normalized.startswith(("test/", "tests/", "testing/")):
            return True
        if "/test/" in normalized or "/tests/" in normalized or "/testing/" in normalized:
            return True
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            return True
    return False


def normalized_hash(text: str) -> str:
    normalized = "\n".join(line for line in text.splitlines() if not line.startswith("index "))
    return hashlib.sha1(normalized.encode("utf-8", errors="replace")).hexdigest()


def strict_ok(row: dict) -> bool:
    verification = row.get("verification") or {}
    clean_p2p = verification.get("clean_pass_to_pass") or row.get("B_PASS_TO_PASS_CLEAN") or []
    return (
        bool(verification.get("pass_to_fail") or verification.get("p2f_confirmed"))
        and verification.get("golden_repair_pass") is True
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and verification.get("p2p_repaired_pass") is True
        and int(verification.get("p2p_repaired_failed") or 0) == 0
        and len(clean_p2p) >= 1
    )


def bucket_file_count(count: int) -> str:
    if count <= 1:
        return "1"
    if count == 2:
        return "2"
    if count <= 5:
        return "3-5"
    return ">5"


def bucket_line_count(count: int) -> str:
    if count <= 20:
        return "<=20"
    if count < 80:
        return "<80"
    if count < 200:
        return "<200"
    return ">=200"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("final_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    final_dir = args.final_dir
    selected = read_jsonl(final_dir / "selected.jsonl")
    injections = read_jsonl(final_dir / "injection_results.jsonl")
    pairing = read_jsonl(final_dir / "rq2_pairing_table.jsonl")
    a_rows = read_jsonl(final_dir / "A_original_official_instances.jsonl")
    b_rows = read_jsonl(final_dir / "B_prinjector_injected_instances.jsonl")

    ids = [source_id(row) for row in selected]
    diff_hashes: list[str] = []
    missing_diffs: list[str] = []
    test_file_touches: list[dict] = []
    file_counts: list[int] = []
    line_counts: list[int] = []
    diff_file_top: Counter[str] = Counter()

    for row in selected:
        sid = source_id(row)
        path = diff_path(row, final_dir)
        if path is None:
            missing_diffs.append(sid)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        files = sorted(set(diff_files(text)))
        if touches_test_file(files):
            test_file_touches.append({"source_instance_id": sid, "files": files})
        diff_hashes.append(normalized_hash(text))
        file_counts.append(len(files))
        line_changes = sum(
            1
            for line in text.splitlines()
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        line_counts.append(line_changes)
        diff_file_top.update(files)

    dataset_counts = Counter(row.get("source_dataset") or row.get("dataset") or "unknown" for row in selected)
    repo_counts = Counter(row.get("repo") or "unknown" for row in selected)
    level_counts = Counter(row.get("injection_level") or row.get("level") or "unknown" for row in selected)
    strict_bad = [source_id(row) for row in selected if not strict_ok(row)]
    hard_errors = {
        "count_mismatches": {
            "selected": len(selected),
            "injection_results": len(injections),
            "pairing_table": len(pairing),
            "A_original": len(a_rows),
            "B_injected": len(b_rows),
        },
        "duplicate_source_ids": len(ids) - len(set(ids)),
        "strict_bad": len(strict_bad),
        "missing_diffs": len(missing_diffs),
        "diff_touches_tests": len(test_file_touches),
        "duplicate_diff_hashes": len(diff_hashes) - len(set(diff_hashes)),
    }
    ok = (
        len(set(hard_errors["count_mismatches"].values())) == 1
        and hard_errors["duplicate_source_ids"] == 0
        and hard_errors["strict_bad"] == 0
        and hard_errors["missing_diffs"] == 0
        and hard_errors["diff_touches_tests"] == 0
        and hard_errors["duplicate_diff_hashes"] == 0
    )

    payload = {
        "final_dir": str(final_dir),
        "ok": ok,
        "hard_errors": hard_errors,
        "samples": {
            "strict_bad": strict_bad[:20],
            "missing_diffs": missing_diffs[:20],
            "diff_touches_tests": test_file_touches[:20],
        },
        "distributions": {
            "datasets": dataset_counts.most_common(),
            "repos_top": repo_counts.most_common(30),
            "repo_count": len(repo_counts),
            "levels": level_counts.most_common(),
            "diff_file_count_buckets": Counter(bucket_file_count(c) for c in file_counts).most_common(),
            "diff_line_count_buckets": Counter(bucket_line_count(c) for c in line_counts).most_common(),
            "diff_files_top": diff_file_top.most_common(25),
        },
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
