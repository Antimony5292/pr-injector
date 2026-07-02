"""Audit completed RQ2 Claude result files and classify unsolved runs."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path


DEFAULT_DIR = Path("experiments/rq2_100/claude_bedrock_sonnet46_eval")


def read_results(base: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(base.glob("RQ2_*/*/result.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        row["_result_path"] = str(path)
        rows.append(row)
    return rows


def changed_files(row: dict) -> list[str]:
    recorded = row.get("agent_changed_files")
    if isinstance(recorded, list):
        return recorded
    path = row.get("agent_patch_path")
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    return re.findall(r"^diff --git a/(.*?) b/", p.read_text(encoding="utf-8", errors="replace"), re.M)


def combined_tail(row: dict) -> str:
    ev = row.get("evaluation") or {}
    ftp = ev.get("fail_to_pass") or {}
    p2p = ev.get("pass_to_pass") or {}
    return "\n".join([
        ftp.get("output_tail") or "",
        p2p.get("output_tail") or "",
        json.dumps(ev.get("test_patch_apply") or {}, ensure_ascii=False),
    ])


def classify(row: dict) -> tuple[str, str]:
    ev = row.get("evaluation") or {}
    status = ev.get("status")
    if ev.get("solved"):
        return "solved", "target and pass_to_pass checks passed"

    files = changed_files(row)
    tail = combined_tail(row)
    ftp = ev.get("fail_to_pass") or {}
    p2p = ev.get("pass_to_pass") or {}

    if row.get("agent", {}).get("returncode") == 124:
        return "agent_timeout", "Claude Code timed out"
    if row.get("agent_patch_size", 0) == 0:
        return "agent_no_patch", "Claude produced no repository diff"

    if status == "agent_modified_forbidden_files":
        return "agent_modified_forbidden_files", "agent edited tests/fixtures/evaluation-protected files"

    if status == "test_patch_apply_failed":
        if any(f.startswith("tests/") or f.startswith("test/") for f in files):
            return "agent_modified_tests_caused_test_patch_conflict", "agent patch edits tests; official test_patch cannot apply"
        return "test_patch_apply_failed_other", "official test_patch cannot apply after agent patch"

    env_patterns = [
        "No module named 'pkg_resources'",
        "No Qt wrapper found",
        "DeprecationWarning:",
        "No module named 'ansible.module_utils.six.moves'",
        "astroid.decorators' has no attribute 'cached'",
        "Failed to build",
        "Could not install project",
        "No module named pytest",
    ]
    if any(p in tail for p in env_patterns):
        return "workflow_env_or_dependency_issue", "local evaluator environment/dependency incompatibility"

    harness_noise_patterns = [
        "ERROR collecting",
        "ERROR: file or directory not found",
        "not found:",
        "no tests ran",
        "no match in any of",
        "ImportError while importing test module",
        "ModuleNotFoundError:",
        "No such file or directory",
        "is not a valid Python module",
        "requires pytest",
        "minversion",
        "unknown config option",
        "unrecognized arguments:",
        "does not refer to a test",
        "Could not find test",
        "Failed to import test module",
    ]
    if any(p in tail for p in harness_noise_patterns):
        if ftp.get("returncode") not in (None, 0):
            return "workflow_fail_to_pass_harness_noise", "FAIL_TO_PASS evaluation hit nodeid/env/test collection noise"
        if p2p.get("returncode") not in (None, 0):
            return "workflow_pass_to_pass_harness_noise", "PASS_TO_PASS evaluation hit nodeid/env/test collection noise"
        return "workflow_harness_noise", "evaluation harness emitted nodeid/env/test collection noise"

    if (p2p.get("returncode") not in (None, 0)) and (
        "not found:" in tail or "no match in any of" in tail or "no tests ran" in tail
    ):
        if ftp.get("returncode") == 0:
            return "workflow_pass_to_pass_test_drift", "FAIL_TO_PASS passed, but pass_to_pass nodeids are unavailable/drifted"
        return "workflow_test_nodeid_drift", "some evaluation nodeids are unavailable/drifted"

    if ftp and ftp.get("returncode") != 0:
        return "agent_failed_fail_to_pass", "target FAIL_TO_PASS tests still fail"
    if p2p and p2p.get("returncode") != 0:
        return "agent_introduced_regression", "target tests passed but pass_to_pass failed"

    return "unsolved_other", "unsolved but no specific known pattern matched"


def main() -> None:
    base = DEFAULT_DIR
    rows = read_results(base)
    audit_rows = []
    for row in rows:
        cause, evidence = classify(row)
        ev = row.get("evaluation") or {}
        ftp = ev.get("fail_to_pass") or {}
        p2p = ev.get("pass_to_pass") or {}
        files = changed_files(row)
        audit_rows.append({
            "case_id": row.get("case_id"),
            "group": row.get("group"),
            "repo": row.get("repo"),
            "source_dataset": row.get("source_dataset"),
            "solved": bool(ev.get("solved")),
            "status": ev.get("status"),
            "cause": cause,
            "evidence": evidence,
            "agent_returncode": row.get("agent", {}).get("returncode"),
            "agent_patch_size": row.get("agent_patch_size"),
            "agent_changed_files": files,
            "fail_to_pass_returncode": ftp.get("returncode"),
            "fail_to_pass_passed": ftp.get("passed"),
            "fail_to_pass_failed": ftp.get("failed"),
            "pass_to_pass_returncode": p2p.get("returncode"),
            "pass_to_pass_passed": p2p.get("passed"),
            "pass_to_pass_failed": p2p.get("failed"),
            "result_path": row.get("_result_path"),
        })

    out_jsonl = base / "audit_results.jsonl"
    out_csv = base / "audit_results.csv"
    with out_jsonl.open("w", encoding="utf-8") as f:
        for row in audit_rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    fields = [
        "case_id", "group", "repo", "source_dataset", "solved", "status", "cause",
        "evidence", "agent_returncode", "agent_patch_size", "fail_to_pass_returncode",
        "pass_to_pass_returncode", "result_path",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    cause_counts = Counter((r["group"], r["cause"]) for r in audit_rows)
    summary = {
        "total_runs": len(audit_rows),
        "by_group": dict(Counter(r["group"] for r in audit_rows)),
        "solved_by_group": {
            group: sum(1 for r in audit_rows if r["group"] == group and r["solved"])
            for group in ["A", "B"]
        },
        "cause_counts": [
            {"group": group, "cause": cause, "count": count}
            for (group, cause), count in cause_counts.most_common()
        ],
    }
    (base / "audit_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out_jsonl}")
    print(f"wrote {out_csv}")


if __name__ == "__main__":
    main()
