"""Run additional PR-INJECTOR L1/L2 construction shards for the B500 set.

The script builds a diverse unprocessed candidate queue from existing official
candidate pools, then runs injection and strict verification per shard. It does
not call any agent or model by default; Level 3 is opt-in via ``--enable-l3``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RQ2_ROOTS = (ROOT / "experiments" / "rq2_300", ROOT / "experiments" / "rq2_500")


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(ROOT))
    except ValueError:
        return str(path)


def project_python() -> str:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if venv_python.exists() and os.access(venv_python, os.X_OK):
        probe = subprocess.run(
            [str(venv_python), "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode == 0:
            return str(venv_python)
    return sys.executable


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


def candidate_pool_paths() -> list[Path]:
    paths: list[Path] = []
    for root in RQ2_ROOTS:
        if not root.exists():
            continue
        paths.extend(sorted(root.glob("candidate_pool*.jsonl")))
        paths.extend(sorted(root.glob("*candidates*.jsonl")))
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
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


def load_candidates() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in candidate_pool_paths():
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid and iid not in out:
                out[iid] = row
    return out


def load_candidates_from(paths: list[Path]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path in paths:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid and iid not in out:
                out[iid] = row
    return out


def read_ids(paths: list[Path]) -> set[str]:
    out: set[str] = set()
    for path in paths:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid:
                out.add(iid)
    return out


def processed_ids() -> set[str]:
    ids: set[str] = set()
    for root in RQ2_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*injection_results.jsonl"):
            for row in read_jsonl(path):
                iid = row.get("source_instance_id") or row.get("instance_id")
                if iid:
                    ids.add(iid)
    return ids


def patch_shape(row: dict) -> tuple[int, int, int]:
    patch = row.get("patch", "") or ""
    files = sum(1 for line in patch.splitlines() if line.startswith("diff --git "))
    hunks = sum(1 for line in patch.splitlines() if line.startswith("@@ "))
    line_changes = sum(
        1
        for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    return files, hunks, line_changes


def diverse_queue(candidates: list[dict]) -> list[dict]:
    buckets: dict[tuple[str, str], deque[dict]] = defaultdict(deque)
    for row in candidates:
        dataset = row.get("source_dataset", "unknown")
        repo = row.get("repo", "unknown")
        buckets[(dataset, repo)].append(row)
    # Harder rows first inside each repo bucket, but repo/dataset round-robin
    # globally to avoid overfitting construction to Django-like cases.
    for key, rows in list(buckets.items()):
        buckets[key] = deque(sorted(
            rows,
            key=lambda row: (
                -patch_shape(row)[0],
                -patch_shape(row)[1],
                -patch_shape(row)[2],
                row.get("source_instance_id") or row.get("instance_id") or "",
            ),
        ))
    keys = sorted(buckets, key=lambda key: (-len(buckets[key]), key[0], key[1]))
    selected: list[dict] = []
    while keys:
        next_keys: list[tuple[str, str]] = []
        for key in keys:
            rows = buckets[key]
            if rows:
                selected.append(rows.popleft())
            if rows:
                next_keys.append(key)
        keys = next_keys
    return selected


def run_one_shard(args: argparse.Namespace, shard_idx: int, shard_path: Path) -> dict:
    run_dir = Path(args.output_root) / f"shard_new_l1l2_{shard_idx:03d}_20260613"
    run_dir.mkdir(parents=True, exist_ok=True)
    workspace_slug = Path(args.output_root).resolve().name
    workspace_root = ROOT / ".pri-workspace" / f"{workspace_slug}_worktrees"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if args.enable_l3:
        env.setdefault("PRI_L3_REJECT_OVERSIMPLIFIED", "1")
    injection = run_dir / "verified_injection_results.jsonl"
    verification = run_dir / "verified_verification_results.jsonl"
    inject_log = run_dir / "verified_injection.log"
    verify_log = run_dir / "verified_verification.log"
    inject_cmd = [
        project_python(),
        str(ROOT / "scripts" / "inject_swebench_pro.py"),
        "--input", str(shard_path),
        "--output", str(injection),
        "--repos-dir", str(ROOT / ".pri-workspace" / "repos"),
        "--worktrees-dir", str(workspace_root / f"inject_{shard_idx:03d}"),
        "--timeout", str(args.timeout),
        "--preflight-target-tests",
        "--max-target-tests", str(args.max_target_tests),
    ]
    if args.v2_fidelity_gate:
        inject_cmd.append("--v2-fidelity-gate")
    if args.v2_require_fidelity_gate:
        inject_cmd.append("--v2-require-fidelity-gate")
    if args.v2_fidelity_gate or args.v2_require_fidelity_gate:
        inject_cmd.extend([
            "--v2-min-score", str(args.v2_min_score),
            "--v2-min-line-ratio", str(args.v2_min_line_ratio),
            "--v2-max-line-ratio", str(args.v2_max_line_ratio),
            "--v2-min-hunk-ratio", str(args.v2_min_hunk_ratio),
            "--v2-min-file-ratio", str(args.v2_min_file_ratio),
            "--v2-min-regression-ratio", str(args.v2_min_regression_ratio),
        ])
    inject_cmd.append("--enable-l3" if args.enable_l3 else "--no-l3")
    started = time.time()
    with inject_log.open("w", encoding="utf-8") as log:
        inject_proc = subprocess.run(inject_cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if inject_proc.returncode != 0:
        return {
            "shard": shard_idx,
            "run_dir": display_path(run_dir),
            "stage": "inject",
            "returncode": inject_proc.returncode,
            "elapsed_sec": round(time.time() - started, 2),
        }
    verify_cmd = [
        project_python(),
        str(ROOT / "scripts" / "verify_swebench_pro.py"),
        "--injection-results", str(injection),
        "--sampled-data", str(shard_path),
        "--output", str(verification),
        "--repos-dir", str(ROOT / ".pri-workspace" / "repos"),
        "--worktrees-dir", str(workspace_root / f"verify_{shard_idx:03d}"),
        "--timeout", str(args.timeout),
        "--check-pass-to-pass",
        "--clean-pass-to-pass",
        "--require-clean-pass-to-pass",
        "--max-pass-to-pass", str(args.max_pass_to_pass),
        "--max-adjacent-pass-to-pass", str(args.max_adjacent_pass_to_pass),
        "--check-golden-repair",
        "--max-target-tests", str(args.max_target_tests),
    ]
    with verify_log.open("w", encoding="utf-8") as log:
        verify_proc = subprocess.run(verify_cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    return {
        "shard": shard_idx,
        "run_dir": display_path(run_dir),
        "stage": "complete" if verify_proc.returncode == 0 else "verify",
        "returncode": verify_proc.returncode,
        "elapsed_sec": round(time.time() - started, 2),
        "injection_rows": len(read_jsonl(injection)),
        "verification_rows": len(read_jsonl(verification)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default=str(ROOT / "experiments" / "rq2_500" / "fidelity_new_l1l2_20260613"))
    parser.add_argument("--limit", type=int, default=590)
    parser.add_argument("--shard-size", type=int, default=60)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-target-tests", type=int, default=8)
    parser.add_argument("--max-pass-to-pass", type=int, default=50)
    parser.add_argument("--max-adjacent-pass-to-pass", type=int, default=25)
    parser.add_argument("--enable-l3", action="store_true")
    parser.add_argument("--v2-fidelity-gate", action="store_true",
                        help="Annotate construction rows with the v2 A/B complexity gate")
    parser.add_argument("--v2-require-fidelity-gate", action="store_true",
                        help="Reject rows that fail the v2 gate; with --enable-l3 this triggers L3 retry")
    parser.add_argument("--v2-min-score", type=float, default=0.65)
    parser.add_argument("--v2-min-line-ratio", type=float, default=0.50)
    parser.add_argument("--v2-max-line-ratio", type=float, default=2.50)
    parser.add_argument("--v2-min-hunk-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-file-ratio", type=float, default=0.50)
    parser.add_argument("--v2-min-regression-ratio", type=float, default=0.25)
    parser.add_argument("--candidate-file", action="append", default=[],
                        help="Use only these candidate JSONL files instead of all known pools")
    parser.add_argument("--exclude-file", action="append", default=[],
                        help="Exclude candidate ids listed in these JSONL files")
    parser.add_argument("--ignore-processed", action="store_true",
                        help="Do not exclude ids that already appear in existing injection outputs")
    args = parser.parse_args()

    output_root = Path(args.output_root)
    shard_root = output_root / "shards"
    output_root.mkdir(parents=True, exist_ok=True)
    candidate_files = [Path(path) for path in args.candidate_file]
    candidates_by_id = load_candidates_from(candidate_files) if candidate_files else load_candidates()
    done = set() if args.ignore_processed else processed_ids()
    done |= read_ids([Path(path) for path in args.exclude_file])
    unprocessed = [
        row for iid, row in candidates_by_id.items()
        if iid not in done
    ]
    ordered = diverse_queue(unprocessed)[:args.limit]
    write_jsonl(output_root / "candidate_pool_unprocessed_diverse_20260613.jsonl", ordered)
    shards: list[Path] = []
    for idx in range(0, len(ordered), args.shard_size):
        shard_rows = ordered[idx:idx + args.shard_size]
        if not shard_rows:
            continue
        shard_path = shard_root / f"candidate_pool_shard_new_l1l2_{len(shards) + 1:03d}.jsonl"
        write_jsonl(shard_path, shard_rows)
        shards.append(shard_path)
    summary = {
        "candidate_pool_paths": [
            str(p.relative_to(ROOT) if p.is_absolute() and ROOT in p.parents else p)
            for p in (candidate_files or candidate_pool_paths())
        ],
        "exclude_files": args.exclude_file,
        "all_candidate_ids": len(candidates_by_id),
        "already_processed_ids": len(done),
        "unprocessed_ids": len(unprocessed),
        "queued": len(ordered),
        "shards": len(shards),
        "workers": args.workers,
        "enable_l3": args.enable_l3,
        "v2_fidelity_gate": args.v2_fidelity_gate or args.v2_require_fidelity_gate,
        "v2_require_fidelity_gate": args.v2_require_fidelity_gate,
        "v2_gate_config": {
            "min_score": args.v2_min_score,
            "min_line_ratio": args.v2_min_line_ratio,
            "max_line_ratio": args.v2_max_line_ratio,
            "min_hunk_ratio": args.v2_min_hunk_ratio,
            "min_file_ratio": args.v2_min_file_ratio,
            "min_regression_ratio": args.v2_min_regression_ratio,
        },
        "queued_by_dataset": dict(Counter(row.get("source_dataset") for row in ordered)),
        "queued_by_repo_top20": Counter(row.get("repo") for row in ordered).most_common(20),
    }
    (output_root / "launch_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not shards:
        return 0

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(run_one_shard, args, idx, shard): idx
            for idx, shard in enumerate(shards, 1)
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            with (output_root / "progress.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
            print(json.dumps(result, ensure_ascii=False), flush=True)
    failures = [row for row in results if row.get("returncode") != 0]
    (output_root / "run_summary.json").write_text(json.dumps({
        "results": sorted(results, key=lambda row: row["shard"]),
        "failures": failures,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
