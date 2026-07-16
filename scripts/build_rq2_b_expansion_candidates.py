"""Build next expansion candidate files for strict RQ2 B construction."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RQ2 = ROOT / "experiments" / "rq2_100"
OUT = RQ2 / "b_expansion_next"


PRIORITY = {
    "ScaleAI/SWE-bench_Pro": [
        "ansible/ansible",
        "internetarchive/openlibrary",
        "qutebrowser/qutebrowser",
    ],
    "princeton-nlp/SWE-bench_Verified": [
        "django/django",
        "sphinx-doc/sphinx",
        "pytest-dev/pytest",
        "pydata/xarray",
        "pylint-dev/pylint",
        "astropy/astropy",
        "sympy/sympy",
        "matplotlib/matplotlib",
        "scikit-learn/scikit-learn",
        "psf/requests",
        "pallets/flask",
    ],
}


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


def attempted_ids() -> set[str]:
    ids: set[str] = set()
    for path in list(RQ2.glob("*injection_results.jsonl")) + [
        RQ2 / "rq2_b_p2f_100_final" / "injection_results.jsonl",
        RQ2 / "b_tpp_supplemental" / "supplemental_p2f_pairing.jsonl",
    ]:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id") or row.get("A_instance_id")
            if iid:
                ids.add(iid)
    return ids


def sort_key(row: dict) -> tuple[int, str]:
    priority = PRIORITY.get(row["source_dataset"], [])
    try:
        idx = priority.index(row["repo"])
    except ValueError:
        idx = len(priority)
    return idx, row["instance_id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pro-limit", type=int, default=92)
    parser.add_argument("--verified-limit", type=int, default=220)
    parser.add_argument("--output-dir", default=str(OUT))
    args = parser.parse_args()

    attempted = attempted_ids()
    pool = read_jsonl(RQ2 / "candidate_pool_all.jsonl")
    remaining = [row for row in pool if row["instance_id"] not in attempted]
    pro = [row for row in remaining if row["source_dataset"] == "ScaleAI/SWE-bench_Pro"]
    verified = [row for row in remaining if row["source_dataset"] == "princeton-nlp/SWE-bench_Verified"]
    pro = sorted(pro, key=sort_key)[: args.pro_limit]
    verified = sorted(verified, key=sort_key)[: args.verified_limit]

    out_dir = Path(args.output_dir)
    write_jsonl(out_dir / "pro_expansion_candidates.jsonl", pro)
    write_jsonl(out_dir / "verified_expansion_candidates.jsonl", verified)
    write_jsonl(out_dir / "all_expansion_candidates.jsonl", pro + verified)
    print(json.dumps({
        "attempted_ids": len(attempted),
        "remaining_pool": len(remaining),
        "pro_candidates": len(pro),
        "verified_candidates": len(verified),
        "output_dir": str(out_dir),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
