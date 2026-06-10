"""Build official A-group evaluator inputs from RQ2 Claude result patches.

Outputs:
  - swebench_predictions.jsonl for SWE-bench official harness.
  - swebench_instance_ids.txt for run_evaluation --instance_ids.
  - verified_predictions.jsonl for SWE-bench official harness.
  - verified_instance_ids.txt for run_evaluation --instance_ids.
  - pro_patches.json for SWE-bench Pro official evaluator.
  - pro_raw_samples.jsonl subset for SWE-bench Pro official evaluator.
  - case_index.jsonl mapping every RQ2 case to inclusion/exclusion status.
"""

from __future__ import annotations

import argparse
import json
import re
from fnmatch import fnmatch
from pathlib import Path

from run_rq2_claude_bedrock_eval import FORBIDDEN_AGENT_EDIT_PATTERNS


ROOT = Path(__file__).resolve().parent.parent
PAIRING = ROOT / "experiments" / "rq2_100" / "rq2_b_p2f_100_final" / "rq2_pairing_table.jsonl"
RESULTS_DIR = ROOT / "experiments" / "rq2_100" / "claude_bedrock_sonnet46_eval"
PRO_RAW = ROOT / ".external" / "SWE-bench_Pro-os" / "helper_code" / "sweap_eval_full_v2.jsonl"
OUT_DIR = ROOT / "experiments" / "rq2_100" / "official_a_eval_inputs"


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def changed_files_from_patch(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(.*?) b/", patch, re.M)


def remove_forbidden_file_diffs(patch: str) -> tuple[str, list[str]]:
    kept: list[str] = []
    removed: list[str] = []
    current: list[str] = []
    current_file: str | None = None

    def flush() -> None:
        nonlocal current, current_file
        if not current:
            return
        if current_file and is_forbidden(current_file):
            removed.append(current_file)
        else:
            kept.extend(current)
        current = []
        current_file = None

    for line in patch.splitlines(keepends=True):
        match = re.match(r"^diff --git a/(.*?) b/", line)
        if match:
            flush()
            current_file = match.group(1)
        current.append(line)
    flush()
    return "".join(kept), sorted(set(removed))


def is_forbidden(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return any(fnmatch(normalized, pattern) for pattern in FORBIDDEN_AGENT_EDIT_PATTERNS)


def load_result(results_dir: Path, case_id: str) -> dict | None:
    path = results_dir / case_id / "A" / "result.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def load_patch(result: dict | None) -> tuple[str, str]:
    if not result:
        return "", "missing_result"
    patch_path = result.get("agent_patch_path")
    if not patch_path:
        return "", "missing_agent_patch_path"
    path = Path(patch_path)
    if not path.exists():
        return "", "missing_agent_patch_file"
    return path.read_text(encoding="utf-8", errors="replace"), ""


def list_string(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value or [])


def official_group_for(case: dict) -> str:
    labels = {
        str(case.get("source_dataset", "")).lower(),
        str(case.get("A_dataset", "")).lower(),
        str(case.get("dataset", "")).lower(),
    }
    case_id = str(case.get("case_id", "")).lower()
    instance_id = str(case.get("A_instance_id", "")).lower()
    if any("verified" in label for label in labels) or case_id.startswith("rq2_verified_"):
        return "verified"
    if any("pro" in label for label in labels) or case_id.startswith("rq2_pro_") or instance_id.startswith("instance_"):
        return "pro"
    if any("swe-bench" in label for label in labels) or case_id.startswith("rq2_princeton_nlp_swe_bench_"):
        return "swebench"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", default=str(PAIRING))
    parser.add_argument("--results-dir", default=str(RESULTS_DIR))
    parser.add_argument("--pro-raw", default=str(PRO_RAW))
    parser.add_argument("--output-dir", default=str(OUT_DIR))
    parser.add_argument("--model-name", default="claude-bedrock-sonnet-4.6")
    parser.add_argument("--include-invalid-as-empty", action="store_true",
                        help="Include missing/empty/forbidden A patches as empty predictions so official outputs cover all cases")
    args = parser.parse_args()

    pairing = read_jsonl(Path(args.pairing))
    results_dir = Path(args.results_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pro_raw_by_id = {row["instance_id"]: row for row in read_jsonl(Path(args.pro_raw))}
    swebench_predictions: list[dict] = []
    verified_predictions: list[dict] = []
    pro_patches: list[dict] = []
    pro_raw_subset: list[dict] = []
    index_rows: list[dict] = []

    for case in pairing:
        official_group = official_group_for(case)

        result = load_result(results_dir, case["case_id"])
        patch, exclusion = load_patch(result)
        changed_files = changed_files_from_patch(patch)
        forbidden_files = sorted({p for p in changed_files if is_forbidden(p)})
        sanitized_forbidden_files: list[str] = []
        if forbidden_files:
            sanitized_patch, sanitized_forbidden_files = remove_forbidden_file_diffs(patch)
            sanitized_changed_files = changed_files_from_patch(sanitized_patch)
            sanitized_still_forbidden = sorted({p for p in sanitized_changed_files if is_forbidden(p)})
            if sanitized_patch.strip() and not sanitized_still_forbidden:
                patch = sanitized_patch
                exclusion = ""
            else:
                exclusion = "agent_modified_forbidden_files"
        invalid_exclusion = exclusion
        if not patch.strip() and not exclusion:
            exclusion = "empty_agent_patch"
            invalid_exclusion = exclusion
        if args.include_invalid_as_empty and official_group in {"swebench", "verified", "pro"} and exclusion:
            patch = ""
            exclusion = ""

        included = official_group in {"swebench", "verified", "pro"} and not exclusion
        if included and official_group == "swebench":
            swebench_predictions.append({
                "instance_id": case["A_instance_id"],
                "model_name_or_path": args.model_name,
                "model_patch": patch,
            })
        elif included and official_group == "verified":
            verified_predictions.append({
                "instance_id": case["A_instance_id"],
                "model_name_or_path": args.model_name,
                "model_patch": patch,
            })
        elif included and official_group == "pro":
            if case["A_instance_id"] in pro_raw_by_id:
                pro_raw_row = dict(pro_raw_by_id[case["A_instance_id"]])
                pro_raw_row["fail_to_pass"] = list_string(pro_raw_row.get("FAIL_TO_PASS", "[]"))
                pro_raw_row["pass_to_pass"] = list_string(pro_raw_row.get("PASS_TO_PASS", "[]"))
                pro_patches.append({
                    "instance_id": case["A_instance_id"],
                    "patch": patch,
                    "prefix": args.model_name,
                })
                pro_raw_subset.append(pro_raw_row)
            else:
                included = False
                exclusion = "missing_pro_raw_sample"

        index_rows.append({
            "case_id": case["case_id"],
            "source_dataset": case["source_dataset"],
            "official_group": official_group,
            "A_instance_id": case["A_instance_id"],
            "B_instance_id": case["B_instance_id"],
            "included_in_official_a_eval": included,
            "exclusion_reason": exclusion,
            "invalid_reason_included_as_empty": invalid_exclusion if args.include_invalid_as_empty and included else "",
            "changed_files": changed_files,
            "forbidden_files": forbidden_files,
            "sanitized_forbidden_files": sanitized_forbidden_files,
            "result_path": str(results_dir / case["case_id"] / "A" / "result.json"),
        })

    write_jsonl(out_dir / "swebench_predictions.jsonl", swebench_predictions)
    (out_dir / "swebench_instance_ids.txt").write_text(
        "\n".join(row["instance_id"] for row in swebench_predictions) + ("\n" if swebench_predictions else ""),
        encoding="utf-8",
    )
    write_jsonl(out_dir / "verified_predictions.jsonl", verified_predictions)
    (out_dir / "verified_instance_ids.txt").write_text(
        "\n".join(row["instance_id"] for row in verified_predictions) + ("\n" if verified_predictions else ""),
        encoding="utf-8",
    )
    (out_dir / "pro_patches.json").write_text(
        json.dumps(pro_patches, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_jsonl(out_dir / "pro_raw_samples.jsonl", pro_raw_subset)
    write_jsonl(out_dir / "case_index.jsonl", index_rows)

    summary = {
        "total_cases": len(pairing),
        "swebench_predictions": len(swebench_predictions),
        "verified_predictions": len(verified_predictions),
        "pro_patches": len(pro_patches),
        "excluded": sum(1 for row in index_rows if not row["included_in_official_a_eval"]),
        "output_dir": str(out_dir),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
