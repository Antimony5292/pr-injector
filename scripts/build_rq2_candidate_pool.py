"""Build RQ2 candidate manifests from SWE-bench Pro and Verified.

The output manifests are source-of-truth inputs for PR-Injector injection. They
preserve the official benchmark instance_id so injected tasks can be paired back
to original SWE-bench/SWE-bench Pro tasks during RQ2.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from datasets import load_dataset


SOURCE_PATTERNS = (
    "diff --git a/src/",
    "diff --git a/lib/",
    "diff --git a/",
)


def _coerce_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            import ast
            parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _looks_runnable_test_id(test_id: str) -> bool:
    """Return whether a benchmark test id looks like a runner label."""

    if not test_id:
        return False
    if "::" in test_id:
        return True
    if re.search(r"(?:^|[. (])test_[A-Za-z0-9_]+", test_id):
        return True
    return False


def _extract_added_tests_from_patch(test_patch: str) -> list[str]:
    """Extract conservative test nodeid candidates from added test methods."""

    out: list[str] = []
    current_file = ""
    for line in test_patch.splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            current_file = m.group(2)
            continue
        if not current_file or not _is_test_path(current_file):
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        fn = re.match(r"\s*(?:async\s+def|def)\s+(test_[A-Za-z0-9_]+)\s*\(", added)
        if fn:
            out.append(f"{current_file}::{fn.group(1)}")
    return list(dict.fromkeys(out))


def _is_test_path(path: str) -> bool:
    name = Path(path).name
    return (
        path.startswith(("tests/", "test/", "testing/"))
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _record(row: dict, source_dataset: str) -> dict:
    raw_fail_to_pass = _coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    patch_fail_to_pass = _extract_added_tests_from_patch(row.get("test_patch", ""))
    if raw_fail_to_pass and any(_looks_runnable_test_id(t) for t in raw_fail_to_pass):
        fail_to_pass = raw_fail_to_pass
    else:
        fail_to_pass = patch_fail_to_pass or raw_fail_to_pass
    pass_to_pass = _coerce_list(row.get("pass_to_pass") or row.get("PASS_TO_PASS"))
    selected_tests = _coerce_list(row.get("selected_test_files_to_run"))
    if not selected_tests:
        selected_tests = sorted({t.split("::", 1)[0] for t in fail_to_pass if "::" in t})
    return {
        "instance_id": row["instance_id"],
        "source_instance_id": row["instance_id"],
        "source_dataset": source_dataset,
        "repo": row["repo"],
        "base_commit": row["base_commit"],
        "patch": row["patch"],
        "test_patch": row.get("test_patch", ""),
        "problem_statement": row.get("problem_statement", ""),
        "fail_to_pass": fail_to_pass,
        "original_fail_to_pass": raw_fail_to_pass,
        "extracted_fail_to_pass_from_test_patch": patch_fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "repo_language": row.get("repo_language", "python"),
        "selected_test_files_to_run": selected_tests,
    }


def _looks_usable(row: dict) -> bool:
    patch = row.get("patch") or ""
    fail_to_pass = _coerce_list(row.get("fail_to_pass") or row.get("FAIL_TO_PASS"))
    if not any(_looks_runnable_test_id(t) for t in fail_to_pass):
        fail_to_pass = _extract_added_tests_from_patch(row.get("test_patch", ""))
    if not fail_to_pass:
        return False
    if row.get("repo_language") not in (None, "python"):
        return False
    return any(pattern in patch for pattern in SOURCE_PATTERNS)


def _dedupe(records: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for rec in records:
        iid = rec["instance_id"]
        if iid in seen:
            continue
        seen.add(iid)
        out.append(rec)
    return out


def load_pro(seed_path: Path | None, limit: int | None) -> list[dict]:
    records: list[dict] = []
    if seed_path and seed_path.exists():
        with seed_path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    if _looks_usable(row):
                        records.append(_record(row, "ScaleAI/SWE-bench_Pro"))

    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    for row in ds:
        if _looks_usable(row):
            records.append(_record(row, "ScaleAI/SWE-bench_Pro"))

    records = _dedupe(records)
    return records[:limit] if limit else records


def load_verified(limit: int | None) -> list[dict]:
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    records = [_record(row, "princeton-nlp/SWE-bench_Verified") for row in ds if _looks_usable(row)]
    records = _dedupe(records)
    return records[:limit] if limit else records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="experiments/rq2_100")
    parser.add_argument("--pro-seed", default="experiments/swebench_pro/sampled_35.jsonl")
    parser.add_argument("--pro-limit", type=int, default=120)
    parser.add_argument("--verified-limit", type=int, default=180)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    pro_seed = Path(args.pro_seed) if args.pro_seed else None

    pro = load_pro(pro_seed, args.pro_limit)
    verified = load_verified(args.verified_limit)

    write_jsonl(output_dir / "candidate_pool_pro.jsonl", pro)
    write_jsonl(output_dir / "candidate_pool_verified.jsonl", verified)
    write_jsonl(output_dir / "candidate_pool_all.jsonl", pro + verified)

    print(f"pro candidates: {len(pro)}")
    print(f"verified candidates: {len(verified)}")
    print(f"all candidates: {len(pro) + len(verified)}")
    print(f"output: {output_dir}")


if __name__ == "__main__":
    main()
