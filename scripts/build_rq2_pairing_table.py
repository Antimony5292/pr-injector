"""Build RQ2 A/B pairing assets from the final 100 P2F injected cases.

Outputs:
  - rq2_pairing_table.jsonl: rich one-row-per-case A/B index
  - rq2_pairing_table.csv: spreadsheet-friendly flat table
  - A_original_official_instances.jsonl: original benchmark tasks for group A
  - B_prinjector_injected_instances.jsonl: PR-INJECTOR tasks for group B
"""

from __future__ import annotations

import csv
import argparse
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
FINAL_DIR = ROOT / "experiments" / "rq2_100" / "rq2_b_p2f_100_final"
INPUT = FINAL_DIR / "injection_results.jsonl"
GOLDEN_DIR = FINAL_DIR / "golden_patches"


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def j(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def dataset_short(source_dataset: str) -> str:
    if "Pro" in source_dataset:
        return "PRO"
    if "Verified" in source_dataset:
        return "VERIFIED"
    return re.sub(r"[^A-Za-z0-9]+", "_", source_dataset).strip("_").upper()


def injected_instance_id(case_id: str, source_instance_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_instance_id)
    return f"{case_id.lower()}__prinjector__{slug}"


def first_nonempty_list(*values) -> list:
    for value in values:
        if isinstance(value, list) and value:
            return value
    return []


def original_fail_to_pass(row: dict) -> list:
    return first_nonempty_list(row.get("fail_to_pass"), row.get("FAIL_TO_PASS"), row.get("A_FAIL_TO_PASS"))


def original_pass_to_pass(row: dict) -> list:
    return first_nonempty_list(row.get("pass_to_pass"), row.get("PASS_TO_PASS"), row.get("A_PASS_TO_PASS"))


def injected_fail_to_pass(row: dict) -> list:
    verification = row.get("verification") or {}
    return first_nonempty_list(
        verification.get("actual_failed_tests"),
        row.get("B_FAIL_TO_PASS"),
        row.get("fail_to_pass"),
        row.get("FAIL_TO_PASS"),
        row.get("A_FAIL_TO_PASS"),
    )


def injected_pass_to_pass(row: dict) -> list:
    verification = row.get("verification") or {}
    return first_nonempty_list(
        row.get("B_PASS_TO_PASS_CLEAN"),
        verification.get("clean_pass_to_pass"),
        row.get("B_PASS_TO_PASS"),
        row.get("pass_to_pass"),
        row.get("PASS_TO_PASS"),
        row.get("A_PASS_TO_PASS"),
    )


def reverse_unified_diff(diff_text: str) -> str:
    """Return a forward fix patch for the injected buggy revision."""
    out: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        body = line[:-1] if line.endswith("\n") else line
        newline = "\n" if line.endswith("\n") else ""
        if body.startswith("index ") and ".." in body:
            left, right = body.split(" ", 1)
            if " " in right:
                hashes, mode = right.split(" ", 1)
                old, new = hashes.split("..", 1)
                out.append(f"{left} {new}..{old} {mode}{newline}")
            else:
                old, new = right.split("..", 1)
                out.append(f"{left} {new}..{old}{newline}")
        elif body.startswith("--- "):
            out.append("+++ " + body[4:] + newline)
        elif body.startswith("+++ "):
            out.append("--- " + body[4:] + newline)
        elif body.startswith("+") and not body.startswith("+++"):
            out.append("-" + body[1:] + newline)
        elif body.startswith("-") and not body.startswith("---"):
            out.append("+" + body[1:] + newline)
        else:
            out.append(line)
    return "".join(out)


def golden_patch_for(row: dict, case_id: str, final_dir: Path) -> tuple[str, str]:
    diff_rel = row.get("injected_diff", "")
    diff_path = ROOT / diff_rel
    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    golden = reverse_unified_diff(diff_text)
    golden_dir = final_dir / "golden_patches"
    golden_dir.mkdir(parents=True, exist_ok=True)
    golden_rel = str((golden_dir / f"{case_id}.diff").relative_to(ROOT))
    (ROOT / golden_rel).write_text(golden, encoding="utf-8")
    return golden, golden_rel


def original_task(row: dict, case_id: str) -> dict:
    fail_to_pass = original_fail_to_pass(row)
    pass_to_pass = original_pass_to_pass(row)
    return {
        "case_id": case_id,
        "group": "A_original_official",
        "source_dataset": row["source_dataset"],
        "instance_id": row["source_instance_id"],
        "repo": row["repo"],
        "base_commit": row.get("base_commit", ""),
        "problem_statement": row.get("problem_statement", ""),
        "patch": row.get("patch", ""),
        "test_patch": row.get("test_patch", ""),
        "FAIL_TO_PASS": fail_to_pass,
        "PASS_TO_PASS": pass_to_pass,
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
    }


def injected_task(row: dict, case_id: str, b_id: str, golden_patch: str, golden_patch_file: str) -> dict:
    fail_to_pass = injected_fail_to_pass(row)
    pass_to_pass = injected_pass_to_pass(row)
    return {
        "case_id": case_id,
        "group": "B_prinjector_injected",
        "source_dataset": row["source_dataset"],
        "source_instance_id": row["source_instance_id"],
        "instance_id": b_id,
        "repo": row["repo"],
        "healthy_head": row.get("healthy_head", ""),
        "base_commit": row.get("healthy_head", ""),
        "buggy_revision_kind": "healthy_head_plus_injected_diff",
        "injected_diff": row.get("injected_diff", ""),
        "golden_patch": golden_patch,
        "golden_patch_file": golden_patch_file,
        "patch": golden_patch,
        "historical_source_patch": row.get("patch", ""),
        "problem_statement": row.get("problem_statement", ""),
        "test_patch": row.get("test_patch", ""),
        "FAIL_TO_PASS": fail_to_pass,
        "PASS_TO_PASS": pass_to_pass,
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "injection_level": row.get("injection_level", ""),
        "verification": row.get("verification", {}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--final-dir", default=str(FINAL_DIR))
    args = parser.parse_args()

    final_dir = Path(args.final_dir).resolve()
    input_path = final_dir / "injection_results.jsonl"
    rows = read_jsonl(input_path)
    counters: dict[str, int] = {}
    pair_rows: list[dict] = []
    a_rows: list[dict] = []
    b_rows: list[dict] = []

    for row in rows:
        ds_short = dataset_short(row["source_dataset"])
        counters[ds_short] = counters.get(ds_short, 0) + 1
        case_id = f"RQ2_{ds_short}_{counters[ds_short]:03d}"
        b_id = injected_instance_id(case_id, row["source_instance_id"])
        verification = row.get("verification") or {}
        golden_patch, golden_patch_file = golden_patch_for(row, case_id, final_dir)

        a = original_task(row, case_id)
        b = injected_task(row, case_id, b_id, golden_patch, golden_patch_file)
        a_fail_to_pass = original_fail_to_pass(row)
        a_pass_to_pass = original_pass_to_pass(row)
        b_fail_to_pass = injected_fail_to_pass(row)
        b_pass_to_pass = injected_pass_to_pass(row)
        a_rows.append(a)
        b_rows.append(b)

        pair_rows.append({
            "case_id": case_id,
            "source_dataset": row["source_dataset"],
            "repo": row["repo"],
            "title": row.get("title", ""),
            "A_group": "original_official_benchmark",
            "A_dataset": row["source_dataset"],
            "A_instance_id": row["source_instance_id"],
            "A_repo": row["repo"],
            "A_base_commit": row.get("base_commit", ""),
            "A_problem_statement": row.get("problem_statement", ""),
            "A_patch": row.get("patch", ""),
            "A_test_patch": row.get("test_patch", ""),
            "A_FAIL_TO_PASS": a_fail_to_pass,
            "A_PASS_TO_PASS": a_pass_to_pass,
            "B_group": "prinjector_injected_benchmark",
            "B_instance_id": b_id,
            "B_source_instance_id": row["source_instance_id"],
            "B_repo": row["repo"],
            "B_healthy_head": row.get("healthy_head", ""),
            "B_buggy_revision_kind": "apply_injected_diff_to_healthy_head",
            "B_injected_diff": row.get("injected_diff", ""),
            "B_golden_patch": golden_patch,
            "B_golden_patch_file": golden_patch_file,
            "B_historical_source_patch": row.get("patch", ""),
            "B_problem_statement": row.get("problem_statement", ""),
            "B_test_patch": row.get("test_patch", ""),
            "B_FAIL_TO_PASS": b_fail_to_pass,
            "B_PASS_TO_PASS": b_pass_to_pass,
            "B_PASS_TO_PASS_CLEAN": b_pass_to_pass,
            "B_PASS_TO_PASS_CLEAN_COUNT": len(b_pass_to_pass),
            "B_injection_level": row.get("injection_level", ""),
            "B_verification_source": row.get("verification_source", ""),
            "B_p2f_validated": bool(verification.get("pass_to_fail")),
            "B_no_regression": verification.get("no_regression"),
            "B_healthy_passed": verification.get("healthy_passed"),
            "B_buggy_failed": verification.get("buggy_failed"),
            "B_golden_repair_pass": verification.get("golden_repair_pass"),
            "B_p2p_repaired_pass": verification.get("p2p_repaired_pass"),
            "rq2_eval_pair_key": f"{row['source_dataset']}::{row['source_instance_id']}",
        })

    write_jsonl(final_dir / "rq2_pairing_table.jsonl", pair_rows)
    write_jsonl(final_dir / "A_original_official_instances.jsonl", a_rows)
    write_jsonl(final_dir / "B_prinjector_injected_instances.jsonl", b_rows)

    csv_fields = [
        "case_id",
        "source_dataset",
        "repo",
        "title",
        "A_dataset",
        "A_instance_id",
        "A_base_commit",
        "A_FAIL_TO_PASS",
        "A_PASS_TO_PASS",
        "B_instance_id",
        "B_source_instance_id",
        "B_healthy_head",
        "B_injected_diff",
        "B_golden_patch_file",
        "B_injection_level",
        "B_p2f_validated",
        "B_no_regression",
        "rq2_eval_pair_key",
    ]
    with open(final_dir / "rq2_pairing_table.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields)
        writer.writeheader()
        for row in pair_rows:
            writer.writerow({
                key: j(row[key]) if isinstance(row.get(key), (list, dict)) else row.get(key, "")
                for key in csv_fields
            })

    print(f"wrote {final_dir / 'rq2_pairing_table.jsonl'} ({len(pair_rows)} rows)")
    print(f"wrote {final_dir / 'rq2_pairing_table.csv'}")
    print(f"wrote {final_dir / 'A_original_official_instances.jsonl'} ({len(a_rows)} rows)")
    print(f"wrote {final_dir / 'B_prinjector_injected_instances.jsonl'} ({len(b_rows)} rows)")


if __name__ == "__main__":
    main()
