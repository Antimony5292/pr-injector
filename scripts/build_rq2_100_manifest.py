"""Build the 50 SWE-bench Pro + 50 SWE-bench Verified P2F manifest.

This consumes injection + verification runs produced by the local PR-INJECTOR
experiments and emits a UI-ready report directory.
"""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "experiments" / "rq2_100"
OUT = EXP / "rq2_b_p2f_100_final"


PRO_VERIFICATIONS = [
    "pro_qute_expand_verification_results_net2.jsonl",
    "pro_ansible_expand_verification_results_net_20.jsonl",
    "pro_ansible_expand_remaining_verification_results_net.jsonl",
]
PRO_INJECTIONS = [
    "pro_qute_expand_injection_results.jsonl",
    "pro_ansible_expand_injection_results.jsonl",
    "pro_ansible_expand_remaining_injection_results.jsonl",
]
PRO_CANDIDATES = [
    "pro_qute_expand_candidates.jsonl",
    "pro_ansible_expand_candidates.jsonl",
    "pro_ansible_expand_remaining_candidates.jsonl",
]

VERIFIED_VERIFICATIONS = [
    "verified_existing_142_verification_results.jsonl",
    "verified_xarray_verification_results_net2.jsonl",
    "verified_django_80_verification_results_net2.jsonl",
]
VERIFIED_INJECTIONS = [
    "verified_existing_142_injection_results.jsonl",
    "verified_xarray_injection_results.jsonl",
    "verified_django_80_injection_results.jsonl",
]
VERIFIED_CANDIDATES = [
    "candidate_verified_existing_priority.jsonl",
    "verified_xarray_candidates.jsonl",
    "verified_django_80_candidates.jsonl",
]


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" #\t")
        if line:
            return line[:180]
    return ""


def test_files_from_nodeids(nodeids: list[str]) -> list[str]:
    files: list[str] = []
    for nodeid in nodeids:
        if "::" in nodeid:
            path = nodeid.split("::", 1)[0]
            if path not in files:
                files.append(path)
        elif " (" in nodeid and nodeid.endswith(")"):
            qual = nodeid.rsplit("(", 1)[1].rstrip(")")
            module = qual.rsplit(".", 2)[0]
            path = "tests/" + module.replace(".", "/") + ".py"
            if path not in files:
                files.append(path)
    return files


def load_maps(injection_files: list[str], candidate_files: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
    injections: dict[str, dict] = {}
    candidates: dict[str, dict] = {}
    for name in injection_files:
        for row in read_jsonl(EXP / name):
            injections[row["instance_id"]] = row
    for name in candidate_files:
        for row in read_jsonl(EXP / name):
            candidates[row["instance_id"]] = row
    return injections, candidates


def collect_p2f(
    verification_files: list[str],
    injection_files: list[str],
    candidate_files: list[str],
    source_dataset: str,
    limit: int,
) -> list[dict]:
    injections, candidates = load_maps(injection_files, candidate_files)
    selected: list[dict] = []
    seen: set[str] = set()

    for name in verification_files:
        for verification_row in read_jsonl(EXP / name):
            iid = verification_row["instance_id"]
            if iid in seen:
                continue
            verification = verification_row.get("verification") or {}
            if not verification.get("pass_to_fail"):
                continue
            injection = injections.get(iid)
            candidate = candidates.get(iid, {})
            if not injection:
                continue

            seen.add(iid)
            fail_to_pass = candidate.get("fail_to_pass") or []
            pass_to_pass = candidate.get("pass_to_pass") or []
            title = first_line(candidate.get("problem_statement", "")) or iid

            merged = dict(injection)
            merged.update({
                "success": True,
                "source_dataset": candidate.get("source_dataset") or source_dataset,
                "source_instance_id": candidate.get("source_instance_id") or iid,
                "title": title,
                "problem_statement": candidate.get("problem_statement", ""),
                "patch": candidate.get("patch", ""),
                "test_patch": candidate.get("test_patch", ""),
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "test_files": candidate.get("selected_test_files_to_run") or test_files_from_nodeids(fail_to_pass),
                "verification": verification,
                "verification_source": name,
            })
            selected.append(merged)
            if len(selected) == limit:
                return selected

    return selected


def main() -> None:
    pro = collect_p2f(
        PRO_VERIFICATIONS,
        PRO_INJECTIONS,
        PRO_CANDIDATES,
        "ScaleAI/SWE-bench_Pro",
        50,
    )
    verified = collect_p2f(
        VERIFIED_VERIFICATIONS,
        VERIFIED_INJECTIONS,
        VERIFIED_CANDIDATES,
        "princeton-nlp/SWE-bench_Verified",
        50,
    )

    if len(pro) != 50 or len(verified) != 50:
        raise SystemExit(f"not enough P2F candidates: pro={len(pro)} verified={len(verified)}")

    for i, row in enumerate(pro + verified, 1):
        row["pr_number"] = i
        row["benchmark_group"] = "B_injected"

    OUT.mkdir(parents=True, exist_ok=True)
    write_jsonl(OUT / "pro_50.jsonl", pro)
    write_jsonl(OUT / "verified_50.jsonl", verified)
    write_jsonl(OUT / "injection_results.jsonl", pro + verified)
    write_jsonl(OUT / "sampled.jsonl", [
        {
            "pr_number": row["pr_number"],
            "patch": row.get("patch", ""),
            "html_url": row.get("source_instance_id", ""),
        }
        for row in pro + verified
    ])

    index_rows = [
        {
            "source_dataset": row["source_dataset"],
            "source_instance_id": row["source_instance_id"],
            "injected_instance_id": row["instance_id"],
            "repo": row["repo"],
            "healthy_head": row.get("healthy_head", ""),
            "injection_level": row.get("injection_level", ""),
            "injected_diff": row.get("injected_diff", ""),
            "fail_to_pass": row.get("fail_to_pass", []),
            "pass_to_pass": row.get("pass_to_pass", []),
            "verification_source": row.get("verification_source", ""),
        }
        for row in pro + verified
    ]
    write_jsonl(OUT / "pairing_index.jsonl", index_rows)

    print(f"wrote {OUT}")
    print(f"pro={len(pro)} verified={len(verified)} total={len(pro) + len(verified)}")


if __name__ == "__main__":
    main()
