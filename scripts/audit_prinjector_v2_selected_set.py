"""Audit a PR-INJECTOR v2 selected set for quality and metadata coverage."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import patch_profile, read_jsonl, resolve_text
except ImportError:
    from scripts.prinjector_v2_metrics import patch_profile, read_jsonl, resolve_text


ROOT = Path(__file__).resolve().parent.parent
QUOTAS = {
    "princeton-nlp/SWE-bench": 250,
    "princeton-nlp/SWE-bench_Verified": 125,
    "ScaleAI/SWE-bench_Pro": 125,
}


REQUIRED_EVAL_FIELDS = [
    "case_id",
    "source_dataset",
    "repo",
    "source_instance_id",
    "instance_id",
    "healthy_head",
    "injected_diff",
    "golden_patch",
    "patch",
    "test_patch",
    "problem_statement",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "injection_level",
    "verification",
]

REQUIRED_V2_FIELDS = [
    "v2_final_source",
    "v2_fidelity_gate",
    "v2_fidelity_gate_final",
    "v2_fidelity_gate_pass",
    "v2_fidelity_gate_pass_final",
    "complexity_profile",
    "injected_diff_hash",
    "benchmark_group",
]


def present(value: Any) -> bool:
    return value not in (None, "", [], {})


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("source_instance_id") or row.get("A_instance_id") or row.get("instance_id") or "")


def strict_ok(row: dict[str, Any]) -> bool:
    verification = row.get("verification") or {}
    return (
        verification.get("pass_to_fail") is True
        and verification.get("golden_repair_pass") is True
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and verification.get("p2p_repaired_pass") is True
    )


def gate_score(row: dict[str, Any]) -> float | None:
    gate = row.get("v2_fidelity_gate_final") or row.get("v2_fidelity_gate") or {}
    value = gate.get("score")
    if value is None:
        return None
    return float(value)


def gate_pass(row: dict[str, Any]) -> bool:
    gate = row.get("v2_fidelity_gate_final") or row.get("v2_fidelity_gate") or {}
    return bool(row.get("v2_fidelity_gate_pass_final") or gate.get("pass_gate"))


def diff_exists(row: dict[str, Any]) -> bool:
    rel = str(row.get("injected_diff") or "")
    if not rel:
        return False
    path = Path(rel)
    return path.exists() or (ROOT / rel).exists()


def diff_touches_tests(row: dict[str, Any]) -> bool:
    diff_text = resolve_text(str(row.get("injected_diff") or ""), ROOT)
    if not diff_text.strip():
        return False
    return bool(patch_profile(diff_text).test_files)


def summarize(rows: list[dict[str, Any]], repo_cap: int) -> dict[str, Any]:
    dataset_counts = Counter(str(row.get("source_dataset") or "") for row in rows)
    repo_counts = Counter(str(row.get("repo") or "") for row in rows)
    scores = [score for row in rows if (score := gate_score(row)) is not None]
    field_coverage = {
        field: sum(1 for row in rows if present(row.get(field)))
        for field in REQUIRED_EVAL_FIELDS + REQUIRED_V2_FIELDS
    }
    verification = Counter()
    for row in rows:
        v = row.get("verification") or {}
        if v.get("pass_to_fail") is not True:
            verification["p2f_miss"] += 1
        if v.get("golden_repair_pass") is not True:
            verification["golden_repair_not_pass"] += 1
        if int(v.get("p2p_buggy_failed") or 0) != 0:
            verification["p2p_buggy_regression"] += 1
        if v.get("p2p_repaired_pass") is not True:
            verification["p2p_repaired_not_pass"] += 1
    return {
        "rows": len(rows),
        "unique_source_ids": len({row_id(row) for row in rows if row_id(row)}),
        "dataset_counts": dict(dataset_counts),
        "dataset_deficits_to_b500_quota": {
            dataset: max(0, quota - dataset_counts.get(dataset, 0))
            for dataset, quota in QUOTAS.items()
        },
        "repo_counts_top30": dict(repo_counts.most_common(30)),
        "repo_cap": repo_cap,
        "repo_cap_violations": {
            repo: count for repo, count in repo_counts.items() if count > repo_cap
        },
        "source_counts": dict(Counter(str(row.get("v2_final_source") or "") for row in rows)),
        "injection_level_counts": dict(Counter(str(row.get("injection_level") or "") for row in rows)),
        "strict_verification_pass": sum(1 for row in rows if strict_ok(row)),
        "strict_verification_failures": dict(verification),
        "v2_gate_pass": sum(1 for row in rows if gate_pass(row)),
        "v2_score": {
            "count": len(scores),
            "min": min(scores) if scores else None,
            "max": max(scores) if scores else None,
            "avg": round(sum(scores) / len(scores), 4) if scores else None,
        },
        "field_coverage": field_coverage,
        "diff_paths_exist": sum(1 for row in rows if diff_exists(row)),
        "diff_touches_tests": sum(1 for row in rows if diff_touches_tests(row)),
        "eval_agent_metrics_present": sum(1 for row in rows if present(row.get("agent_eval_metrics"))),
        "notes": [
            "Rows pass current strict P2F/P2P/golden and v2 complexity-fidelity gate if strict_verification_pass == rows and v2_gate_pass == rows.",
            "Dataset quotas are not yet satisfied until rows reaches B500 with SWE=250, Verified=125, Pro=125.",
            "agent_eval_metrics are expected to be absent before running the downstream RQ2 agent evaluation.",
        ],
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# PR-INJECTOR v2 Selected Set Audit",
        "",
        f"- rows: `{summary['rows']}`",
        f"- unique_source_ids: `{summary['unique_source_ids']}`",
        f"- strict_verification_pass: `{summary['strict_verification_pass']}`",
        f"- v2_gate_pass: `{summary['v2_gate_pass']}`",
        f"- diff_paths_exist: `{summary['diff_paths_exist']}`",
        f"- diff_touches_tests: `{summary['diff_touches_tests']}`",
        f"- repo_cap: `{summary['repo_cap']}`",
        f"- repo_cap_violations: `{summary['repo_cap_violations']}`",
        "",
        "## Dataset Counts",
        "",
    ]
    for key, value in summary["dataset_counts"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines += ["", "## Dataset Deficits To B500 Quota", ""]
    for key, value in summary["dataset_deficits_to_b500_quota"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines += ["", "## Repo Counts Top30", ""]
    for key, value in summary["repo_counts_top30"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines += ["", "## Field Coverage", ""]
    for key, value in summary["field_coverage"].items():
        lines.append(f"- `{key}`: `{value}/{summary['rows']}`")
    lines += ["", "## Notes", ""]
    for note in summary["notes"]:
        lines.append(f"- {note}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selected", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo-cap", type=int, default=50)
    args = parser.parse_args()

    rows = read_jsonl(Path(args.selected))
    summary = summarize(rows, args.repo_cap)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "quality_audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_markdown(summary, output_dir / "quality_audit.md")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
