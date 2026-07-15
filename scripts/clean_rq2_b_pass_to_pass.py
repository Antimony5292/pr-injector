"""Clean B-group PASS_TO_PASS tests for PR-INJECTOR RQ2.

The injected benchmark candidates were selected by P2F target behavior. This
script validates regression tests for B on the modern target revision:
  1. checkout healthy HEAD and run candidate PASS_TO_PASS tests;
  2. apply the injected diff to create buggy h^-;
  3. keep only tests that pass on both healthy h and buggy h^-.

It writes an updated pairing table with B_PASS_TO_PASS_CLEAN. The original
B_PASS_TO_PASS is preserved for traceability.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_rq2_claude_bedrock_eval import (  # noqa: E402
    apply_patch_file,
    create_venv,
    create_worktree,
    install_project,
    normalize_tests,
    read_jsonl,
    remove_worktree,
    repo_dir,
    run_tests,
)

PAIRING = ROOT / "experiments" / "rq2_100" / "rq2_b_p2f_100_final" / "rq2_pairing_table.jsonl"
OUT_DIR = ROOT / "experiments" / "rq2_100" / "b_tpp_cleaning"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def run(cmd: list[str], cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def passing_tests(worktree: Path, repo: str, tests: list[str], timeout: int, python: str) -> tuple[list[str], dict]:
    if not tests:
        return [], {"mode": "empty", "result": None}
    batch = run_tests(worktree, repo, tests, timeout, python)
    if batch.get("returncode") == 0:
        return tests, {"mode": "batch", "result": batch}

    kept: list[str] = []
    individual: list[dict] = []
    for test in tests:
        result = run_tests(worktree, repo, [test], timeout, python)
        ok = result.get("returncode") == 0
        if ok:
            kept.append(test)
        individual.append({
            "test": test,
            "ok": ok,
            "returncode": result.get("returncode"),
            "tail": result.get("output_tail", "")[-800:],
        })
    return kept, {"mode": "individual", "batch_result": batch, "individual": individual}


def discover_regression_candidates(
    worktree: Path,
    repo: str,
    excluded_tests: list[str],
    max_candidates: int,
) -> list[str]:
    proc = run(["git", "ls-files"], worktree, timeout=120)
    if proc.returncode != 0:
        return []

    excluded_paths = {t.split("::", 1)[0] for t in excluded_tests}
    files = [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    if repo == "ansible/ansible":
        preferred_prefixes = (
            "test/units/module_utils/",
            "test/units/plugins/",
            "test/units/utils/",
            "test/units/parsing/",
        )
    elif repo == "qutebrowser/qutebrowser":
        preferred_prefixes = (
            "tests/unit/",
            "tests/end2end/features/test_",
        )
    else:
        preferred_prefixes = ("tests/", "test/")

    candidates: list[str] = []
    for prefix in preferred_prefixes:
        for path in files:
            if path in excluded_paths:
                continue
            if not path.endswith(".py"):
                continue
            name = Path(path).name
            if not (name.startswith("test_") or name.endswith("_test.py")):
                continue
            if not path.startswith(prefix):
                continue
            candidates.append(path)

    return candidates[:max_candidates]


def clean_case(case: dict, args: argparse.Namespace, repos_dirs: list[Path]) -> dict:
    case_id = case["case_id"]
    repo = case["repo"]
    out_dir = Path(args.output_dir) / case_id
    result_path = out_dir / "clean_result.json"
    if result_path.exists() and not args.force:
        return json.loads(result_path.read_text(encoding="utf-8"))

    repo_path = repo_dir(repo, repos_dirs)
    branch = f"rq2-clean-b-tpp-{case_id.lower()}"
    worktree = (ROOT / args.worktrees_dir).resolve() / f"{case_id}-{repo.replace('/', '__')}"
    started = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict
    create_worktree(repo_path, worktree, case["B_healthy_head"], branch)
    try:
        candidates = list(dict.fromkeys((case.get("B_PASS_TO_PASS") or [])[: args.max_candidates]))
        vpy = create_venv(worktree)
        normalized_healthy = normalize_tests(worktree, repo, candidates)
        fallback_candidates = []
        if args.discover_fallback:
            fallback_candidates = discover_regression_candidates(
                worktree,
                repo,
                case.get("B_FAIL_TO_PASS") or [],
                args.max_fallback_candidates,
            )
        install_project(worktree, repo, vpy, normalized_healthy or fallback_candidates)
        healthy_pass, healthy_detail = passing_tests(worktree, repo, normalized_healthy, args.test_timeout_s, vpy)
        fallback_healthy_pass: list[str] = []
        fallback_healthy_detail: dict = {"mode": "disabled", "result": None}
        if args.discover_fallback and fallback_candidates:
            fallback_healthy_pass, fallback_healthy_detail = passing_tests(
                worktree,
                repo,
                fallback_candidates,
                args.test_timeout_s,
                vpy,
            )

        injected_apply = apply_patch_file(worktree, case["B_injected_diff"], "B_injected_diff")
        if not injected_apply.get("applied"):
            clean = []
            buggy_detail = {"mode": "injected_apply_failed", "result": injected_apply}
        else:
            buggy_pass, buggy_detail = passing_tests(worktree, repo, healthy_pass, args.test_timeout_s, vpy)
            clean = buggy_pass
            if not clean and args.discover_fallback and fallback_healthy_pass:
                clean, fallback_buggy_detail = passing_tests(
                    worktree,
                    repo,
                    fallback_healthy_pass,
                    args.test_timeout_s,
                    vpy,
                )
                buggy_detail = {
                    "mode": "fallback_discovered",
                    "inherited_buggy_detail": buggy_detail,
                    "fallback_buggy_detail": fallback_buggy_detail,
                }

        result = {
            "case_id": case_id,
            "repo": repo,
            "source_dataset": case["source_dataset"],
            "A_instance_id": case["A_instance_id"],
            "B_instance_id": case["B_instance_id"],
            "candidate_count": len(candidates),
            "normalized_healthy_count": len(normalized_healthy),
            "healthy_pass_count": len(healthy_pass),
            "clean_pass_to_pass_count": len(clean),
            "B_PASS_TO_PASS_CLEAN": clean,
            "healthy_detail": healthy_detail,
            "fallback_candidate_count": len(fallback_candidates),
            "fallback_healthy_pass_count": len(fallback_healthy_pass),
            "fallback_healthy_detail": fallback_healthy_detail,
            "buggy_detail": buggy_detail,
            "elapsed_s": round(time.time() - started, 3),
            "status": "completed",
        }
    except Exception as exc:
        result = {
            "case_id": case_id,
            "repo": repo,
            "source_dataset": case.get("source_dataset"),
            "status": "error",
            "error": str(exc),
            "B_PASS_TO_PASS_CLEAN": [],
            "elapsed_s": round(time.time() - started, 3),
        }
    finally:
        write_json(result_path, result)
        remove_worktree(repo_path, worktree, branch, args.keep_worktrees)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", default=str(PAIRING))
    parser.add_argument("--output-dir", default=str(OUT_DIR))
    parser.add_argument("--worktrees-dir", default=".pri-workspace/rq2-b-tpp-clean-worktrees")
    parser.add_argument("--repos-dir", action="append", default=[])
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--case-id-file", action="append", default=[])
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--test-timeout-s", type=int, default=300)
    parser.add_argument("--discover-fallback", action="store_true")
    parser.add_argument("--max-fallback-candidates", type=int, default=30)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-worktrees", action="store_true")
    args = parser.parse_args()

    repos_dirs = [Path(p) for p in args.repos_dir] or [
        ROOT / ".pri-workspace" / "demo-repos",
        ROOT / ".pri-workspace" / "swebench-screen-repos",
    ]
    cases = read_jsonl(Path(args.pairing))
    file_case_ids: list[str] = []
    for path in args.case_id_file:
        file_case_ids.extend(
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if file_case_ids:
        args.case_id.extend(file_case_ids)
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in wanted]
    if args.max_cases is not None:
        cases = cases[: args.max_cases]

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    Path(args.worktrees_dir).mkdir(parents=True, exist_ok=True)

    clean_by_case: dict[str, dict] = {}
    for case in cases:
        print(f"[B-TPP] {case['case_id']} {case['repo']}", flush=True)
        result = clean_case(case, args, repos_dirs)
        clean_by_case[case["case_id"]] = result
        print(
            f"        status={result.get('status')} clean={result.get('clean_pass_to_pass_count', 0)}",
            flush=True,
        )

    all_cases = read_jsonl(Path(args.pairing))
    updated = []
    for case in all_cases:
        row = dict(case)
        clean = clean_by_case.get(case["case_id"])
        if clean is None:
            existing_path = Path(args.output_dir) / case["case_id"] / "clean_result.json"
            if existing_path.exists():
                clean = json.loads(existing_path.read_text(encoding="utf-8"))
        if clean is not None:
            row["B_PASS_TO_PASS_CLEAN"] = clean.get("B_PASS_TO_PASS_CLEAN", [])
            row["B_PASS_TO_PASS_CLEAN_COUNT"] = len(row["B_PASS_TO_PASS_CLEAN"])
            row["B_PASS_TO_PASS_CLEAN_STATUS"] = clean.get("status")
        updated.append(row)

    out_pairing = Path(args.output_dir) / "rq2_pairing_table.b_tpp_cleaned.jsonl"
    write_jsonl(out_pairing, updated)
    summary = {
        "processed_cases": len(cases),
        "all_cases": len(all_cases),
        "with_clean_results": sum("B_PASS_TO_PASS_CLEAN" in row for row in updated),
        "total_clean_tests": sum(len(row.get("B_PASS_TO_PASS_CLEAN") or []) for row in updated),
        "output_pairing": str(out_pairing),
    }
    write_json(Path(args.output_dir) / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
