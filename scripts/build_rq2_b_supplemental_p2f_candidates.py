"""Build supplemental P2F candidate pairing rows for strict B construction.

This aggregates existing injection/verification outputs and selects P2F
candidates that are not already in the current RQ2 100-case set. The output is
shaped like rq2_pairing_table rows so it can be passed to
clean_rq2_b_pass_to_pass.py for PASS->PASS validation.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RQ2 = ROOT / "experiments" / "rq2_100"
CURRENT = RQ2 / "rq2_b_p2f_100_final" / "injection_results.jsonl"
OUT = RQ2 / "b_tpp_supplemental" / "supplemental_p2f_pairing.jsonl"


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


def dataset_short(source_dataset: str) -> str:
    if "Pro" in source_dataset:
        return "PRO"
    if "Verified" in source_dataset:
        return "VERIFIED"
    return re.sub(r"[^A-Za-z0-9]+", "_", source_dataset).strip("_").upper()


def b_instance_id(case_id: str, source_instance_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_instance_id)
    return f"{case_id.lower()}__prinjector__{slug}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default=str(OUT))
    args = parser.parse_args()

    meta: dict[str, dict] = {}
    for path in list(RQ2.glob("candidate*.jsonl")) + [CURRENT]:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if iid:
                meta.setdefault(iid, {}).update(row)

    ver: dict[str, tuple[dict, str]] = {}
    for path in RQ2.glob("*verification_results*.jsonl"):
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if not iid:
                continue
            verification = row.get("verification") or {}
            if verification.get("pass_to_fail"):
                ver[iid] = (verification, path.name)

    rows: dict[tuple[str, str], dict] = {}
    for path in list(RQ2.glob("*injection_results.jsonl")) + [CURRENT, RQ2 / "report_current" / "injection_results.jsonl"]:
        for row in read_jsonl(path):
            iid = row.get("source_instance_id") or row.get("instance_id")
            if not iid:
                continue
            merged = dict(meta.get(iid, {}))
            merged.update(row)
            verification = merged.get("verification") or {}
            verification_source = path.name
            if not verification.get("pass_to_fail") and iid in ver:
                verification, verification_source = ver[iid]
            if not verification.get("pass_to_fail"):
                continue
            source_dataset = merged.get("source_dataset")
            if source_dataset not in {"ScaleAI/SWE-bench_Pro", "princeton-nlp/SWE-bench_Verified"}:
                continue
            if not merged.get("injected_diff") or not merged.get("healthy_head"):
                continue
            merged["source_instance_id"] = iid
            merged["verification"] = verification
            merged["verification_source"] = verification_source
            key = (source_dataset, iid)
            if key not in rows or len(json.dumps(merged, default=str)) > len(json.dumps(rows[key], default=str)):
                rows[key] = merged

    current_ids = {
        row.get("source_instance_id") or row.get("instance_id")
        for row in read_jsonl(CURRENT)
    }
    supplemental = [
        row for (_, iid), row in rows.items()
        if iid not in current_ids
    ]
    supplemental.sort(key=lambda r: (dataset_short(r["source_dataset"]), r["repo"], r["source_instance_id"]))

    counters: dict[str, int] = {}
    out_rows: list[dict] = []
    for row in supplemental:
        short = dataset_short(row["source_dataset"])
        counters[short] = counters.get(short, 0) + 1
        case_id = f"SUP_{short}_{counters[short]:03d}"
        out_rows.append({
            "case_id": case_id,
            "source_dataset": row["source_dataset"],
            "repo": row["repo"],
            "title": row.get("title", ""),
            "A_instance_id": row["source_instance_id"],
            "A_base_commit": row.get("base_commit", ""),
            "A_problem_statement": row.get("problem_statement", ""),
            "A_patch": row.get("patch", ""),
            "A_test_patch": row.get("test_patch", ""),
            "A_FAIL_TO_PASS": row.get("fail_to_pass", []),
            "A_PASS_TO_PASS": row.get("pass_to_pass", []),
            "B_instance_id": b_instance_id(case_id, row["source_instance_id"]),
            "B_source_instance_id": row["source_instance_id"],
            "B_healthy_head": row.get("healthy_head", ""),
            "B_injected_diff": row.get("injected_diff", ""),
            "B_problem_statement": row.get("problem_statement", ""),
            "B_FAIL_TO_PASS": row.get("fail_to_pass", []),
            "B_PASS_TO_PASS": row.get("pass_to_pass", []),
            "B_injection_level": row.get("injection_level", ""),
            "B_verification_source": row.get("verification_source", ""),
            "B_p2f_validated": True,
            "B_verification": row.get("verification", {}),
        })

    write_jsonl(Path(args.output), out_rows)
    print(json.dumps({
        "output": args.output,
        "rows": len(out_rows),
        "by_dataset": {
            ds: sum(1 for row in out_rows if row["source_dataset"] == ds)
            for ds in sorted({row["source_dataset"] for row in out_rows})
        },
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
