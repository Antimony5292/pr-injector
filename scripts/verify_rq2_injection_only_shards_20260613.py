"""Verify RQ2 injection-only shards with the current strict verifier.

This helper is intentionally orchestration-only. Some earlier construction
shards finished PR-INJECTOR injection but never ran the full verification step.
To rebuild the B benchmark under the latest gates, this script finds those
shards, reconstructs per-shard sampled-data rows from candidate pools, and runs
``verify_swebench_pro.py`` with golden repair plus expanded P2P checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RQ2_ROOTS = (ROOT / "experiments" / "rq2_300", ROOT / "experiments" / "rq2_500")
WORKTREES_ROOT = ROOT / ".pri-workspace" / "rq2_500_fidelity_verify_injection_only_20260613"


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


def load_candidate_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in candidate_pool_paths():
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid and iid not in index:
                index[iid] = row
    return index


def injection_only_jobs() -> list[tuple[Path, str, Path, Path]]:
    jobs: list[tuple[Path, str, Path, Path]] = []
    for root in RQ2_ROOTS:
        if not root.exists():
            continue
        for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            for group in ("pro", "verified"):
                injection = run_dir / f"{group}_injection_results.jsonl"
                verification = run_dir / f"{group}_verification_results.jsonl"
                if injection.exists() and not verification.exists():
                    jobs.append((run_dir, group, injection, verification))
    return jobs


def main() -> int:
    candidate_index = load_candidate_index()
    jobs = injection_only_jobs()
    print(json.dumps({
        "candidate_rows": len(candidate_index),
        "jobs": len(jobs),
        "worktrees_root": str(WORKTREES_ROOT),
    }, ensure_ascii=False, indent=2), flush=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    for idx, (run_dir, group, injection, verification) in enumerate(jobs, 1):
        injection_rows = read_jsonl(injection)
        sampled_rows: list[dict] = []
        missing: list[str] = []
        for row in injection_rows:
            iid = row.get("source_instance_id") or row.get("instance_id")
            if not iid:
                continue
            candidate = candidate_index.get(iid)
            if candidate:
                sampled_rows.append(candidate)
            else:
                missing.append(iid)
        sampled = run_dir / f"{group}_sampled_for_verification_20260613.jsonl"
        write_jsonl(sampled, sampled_rows)
        print(json.dumps({
            "job": idx,
            "total_jobs": len(jobs),
            "run_dir": str(run_dir.relative_to(ROOT)),
            "group": group,
            "injection_rows": len(injection_rows),
            "sampled_rows": len(sampled_rows),
            "missing_candidate_rows": len(missing),
        }, ensure_ascii=False), flush=True)
        if not sampled_rows:
            print(f"skip {run_dir}: no sampled rows", flush=True)
            continue
        log_path = run_dir / f"{group}_verification_20260613.log"
        cmd = [
            project_python(),
            str(ROOT / "scripts" / "verify_swebench_pro.py"),
            "--injection-results", str(injection),
            "--sampled-data", str(sampled),
            "--output", str(verification),
            "--repos-dir", str(ROOT / ".pri-workspace" / "repos"),
            "--worktrees-dir", str(WORKTREES_ROOT / run_dir.name / group),
            "--timeout", "300",
            "--check-pass-to-pass",
            "--clean-pass-to-pass",
            "--require-clean-pass-to-pass",
            "--max-pass-to-pass", "50",
            "--max-adjacent-pass-to-pass", "25",
            "--check-golden-repair",
            "--max-target-tests", "8",
        ]
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        print(json.dumps({
            "run_dir": str(run_dir.relative_to(ROOT)),
            "group": group,
            "returncode": proc.returncode,
            "log": str(log_path.relative_to(ROOT)),
            "verification": str(verification.relative_to(ROOT)),
        }, ensure_ascii=False), flush=True)
        if proc.returncode != 0:
            return proc.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
