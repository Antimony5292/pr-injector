"""Run official A-group evaluation from a prepared input directory."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SWE_PYTHON = ROOT / ".venvs" / "swebench-official" / "bin" / "python"
PRO_ROOT = ROOT / ".external" / "SWE-bench_Pro-os"


def run(cmd: list[str], cwd: Path) -> None:
    print(" ".join(cmd), flush=True)
    completed = subprocess.run(cmd, cwd=str(cwd), check=False)
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def read_instance_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--which", choices=["verified", "pro", "both"], default="both")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--run-id-prefix", default="rq2_a_matched95_official")
    parser.add_argument("--pro-output-dir", required=True)
    parser.add_argument("--verified-workers", type=int, default=1)
    parser.add_argument("--pro-workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--namespace", default="none")
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--redo-pro", action="store_true")
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    summary_path = input_dir / "summary.json"
    if summary_path.exists():
        print(summary_path.read_text(encoding="utf-8"), flush=True)

    if args.which in {"verified", "both"}:
        ids = read_instance_ids(input_dir / "verified_instance_ids.txt")
        if ids:
            run([
                str(SWE_PYTHON),
                "-m",
                "swebench.harness.run_evaluation",
                "--dataset_name",
                "princeton-nlp/SWE-bench_Verified",
                "--predictions_path",
                str(input_dir / "verified_predictions.jsonl"),
                "--max_workers",
                str(args.verified_workers),
                "--instance_ids",
                *ids,
                "--run_id",
                f"{args.run_id_prefix}_verified",
                "--namespace",
                args.namespace,
                "--timeout",
                str(args.timeout),
            ], ROOT)
        else:
            print("No verified A predictions to evaluate.", flush=True)

    if args.which in {"pro", "both"}:
        patches = json.loads((input_dir / "pro_patches.json").read_text(encoding="utf-8"))
        if patches:
            cmd = [
                str(SWE_PYTHON),
                "swe_bench_pro_eval.py",
                "--raw_sample_path",
                str(input_dir / "pro_raw_samples.jsonl"),
                "--patch_path",
                str(input_dir / "pro_patches.json"),
                "--output_dir",
                str((ROOT / args.pro_output_dir).resolve()),
                "--scripts_dir",
                "run_scripts",
                "--num_workers",
                str(args.pro_workers),
                "--dockerhub_username",
                args.dockerhub_username,
                "--use_local_docker",
            ]
            if args.redo_pro:
                cmd.append("--redo")
            run(cmd, PRO_ROOT)
        else:
            print("No Pro A predictions to evaluate.", flush=True)


if __name__ == "__main__":
    main()
