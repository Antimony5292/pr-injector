#!/usr/bin/env python3
"""Continuously assemble strict construction results on top of a frozen B baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parent


def result_signature(run_dirs: list[Path]) -> tuple[int, int, int]:
    files = rows = bytes_total = 0
    for run_dir in run_dirs:
        for path in run_dir.rglob("verified_verification_results.jsonl"):
            files += 1
            bytes_total += path.stat().st_size
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rows += 1
    return files, rows, bytes_total


def read_summary(output_dir: Path) -> dict:
    for name in ("assembly_summary.json", "assembly_failed_summary.json"):
        path = output_dir / name
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def assemble(args: argparse.Namespace) -> tuple[int, dict, str]:
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "assemble_prinjector_v2_b500.py"),
        "--locked-selection", str(args.baseline),
        "--old-b", str(args.baseline),
        "--final-dir", str(args.output_dir),
        "--repo-cap", str(args.repo_cap),
        "--target-size", str(args.target_size),
        "--allow-repo-cap-replacement",
    ]
    for candidate_file in args.candidate_file:
        cmd.extend(["--candidate-file", str(candidate_file)])
    for run_dir in args.run_dir:
        cmd.extend(["--construction-run-dir", str(run_dir)])
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.returncode, read_summary(args.output_dir), (proc.stdout + proc.stderr)[-4000:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, action="append", type=Path)
    parser.add_argument("--run-dir", required=True, action="append", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--target-size", type=int, default=500)
    parser.add_argument("--repo-cap", type=int, default=50)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    progress_path = args.output_dir / "monitor_progress.jsonl"
    last_signature: tuple[int, int, int] | None = None
    while True:
        signature = result_signature(args.run_dir)
        if signature != last_signature:
            returncode, summary, output_tail = assemble(args)
            selected = int(summary.get("selected") or 0)
            status = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "verification_files": signature[0],
                "verification_rows": signature[1],
                "verification_bytes": signature[2],
                "assembler_returncode": returncode,
                "selected": selected,
                "target_size": args.target_size,
                "remaining": max(0, args.target_size - selected),
                "accepted_new": summary.get("accepted_new"),
                "selected_by_dataset": summary.get("selected_by_dataset") or {},
                "rejects": summary.get("rejects") or {},
                "output_tail": output_tail,
            }
            (args.output_dir / "monitor_status.json").write_text(
                json.dumps(status, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with progress_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(status, ensure_ascii=False) + "\n")
            print(json.dumps(status, ensure_ascii=False), flush=True)
            last_signature = signature
            if selected >= args.target_size:
                return
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    main()
