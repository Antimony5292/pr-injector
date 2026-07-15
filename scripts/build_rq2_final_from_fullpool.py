"""Build final 50+50 RQ2 B assets from one fullpool construction run.

This is intentionally a post-processing helper. It does not perform injection,
verification, or any model call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RQ2 = ROOT / "experiments" / "rq2_100"


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


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" #\t")
        if line:
            return line[:180]
    return ""


def test_files_from_nodeids(nodeids: list[str]) -> list[str]:
    files: list[str] = []
    for nodeid in nodeids:
        path = nodeid.split("::", 1)[0]
        if path and path not in files:
            files.append(path)
    return files


def strict_ok(row: dict, injection: dict) -> bool:
    verification = row.get("verification") or {}
    level = str(injection.get("injection_level", ""))
    return (
        not level.startswith("Level_3")
        and bool(injection.get("success"))
        and bool(verification.get("pass_to_fail"))
        and verification.get("golden_repair_pass") is True
        and verification.get("p2p_repaired_pass") is True
    )


def collect_group(
    run_dir: Path,
    candidates_path: Path,
    group: str,
    source_dataset: str,
    limit: int,
) -> list[dict]:
    injections = {
        row["instance_id"]: row
        for row in read_jsonl(run_dir / f"{group}_injection_results.jsonl")
        if row.get("instance_id")
    }
    candidates = {
        row["instance_id"]: row
        for row in read_jsonl(candidates_path)
        if row.get("instance_id")
    }

    selected: list[dict] = []
    seen: set[str] = set()
    for verification_row in read_jsonl(run_dir / f"{group}_verification_results.jsonl"):
        iid = verification_row.get("instance_id")
        if not iid or iid in seen:
            continue
        injection = injections.get(iid)
        if not injection or not strict_ok(verification_row, injection):
            continue
        candidate = candidates.get(iid, {})
        seen.add(iid)
        fail_to_pass = (
            verification_row.get("verification", {}).get("actual_failed_tests")
            or injection.get("fail_to_pass")
            or candidate.get("fail_to_pass")
            or []
        )
        pass_to_pass = (
            verification_row.get("verification", {}).get("clean_pass_to_pass")
            or candidate.get("pass_to_pass")
            or []
        )
        title = first_line(candidate.get("problem_statement", "")) or iid
        merged = dict(injection)
        merged.update({
            "success": True,
            "source_dataset": candidate.get("source_dataset") or source_dataset,
            "source_instance_id": candidate.get("source_instance_id") or iid,
            "title": title,
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
            "verification": verification_row.get("verification") or {},
            "verification_source": f"{group}_verification_results.jsonl",
        })
        selected.append(merged)
        if len(selected) == limit:
            break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument(
        "--final-dir",
        default=str(RQ2 / "rq2_b_l1_l2_100_final"),
    )
    parser.add_argument("--limit-per-group", type=int, default=50)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    final_dir = Path(args.final_dir).resolve()
    pro = collect_group(
        run_dir,
        RQ2 / "candidate_pool_pro.jsonl",
        "pro",
        "ScaleAI/SWE-bench_Pro",
        args.limit_per_group,
    )
    verified = collect_group(
        run_dir,
        RQ2 / "candidate_pool_verified.jsonl",
        "verified",
        "princeton-nlp/SWE-bench_Verified",
        args.limit_per_group,
    )

    if len(pro) != args.limit_per_group or len(verified) != args.limit_per_group:
        raise SystemExit(
            "not enough strict L1/L2 candidates: "
            f"pro={len(pro)} verified={len(verified)}"
        )

    for i, row in enumerate(pro + verified, 1):
        row["pr_number"] = i
        row["benchmark_group"] = "B_injected_l1_l2_only"

    write_jsonl(final_dir / "pro_50.jsonl", pro)
    write_jsonl(final_dir / "verified_50.jsonl", verified)
    write_jsonl(final_dir / "injection_results.jsonl", pro + verified)
    write_jsonl(final_dir / "sampled.jsonl", [
        {
            "pr_number": row["pr_number"],
            "patch": row.get("patch", ""),
            "html_url": row.get("source_instance_id", ""),
        }
        for row in pro + verified
    ])

    print(json.dumps({
        "final_dir": str(final_dir),
        "pro": len(pro),
        "verified": len(verified),
        "total": len(pro) + len(verified),
        "level3_rows": sum(
            str(row.get("injection_level", "")).startswith("Level_3")
            for row in pro + verified
        ),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
