"""Summarize same-issue multi-variant PR-INJECTOR pilot runs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import read_jsonl, write_jsonl


VARIANT_SUFFIX = "__variant__"


def source_id(instance_id: str) -> str:
    if VARIANT_SUFFIX in instance_id:
        return instance_id.split(VARIANT_SUFFIX, 1)[0]
    return instance_id


def variant_from_id(instance_id: str, fallback: str) -> str:
    if VARIANT_SUFFIX in instance_id:
        return instance_id.split(VARIANT_SUFFIX, 1)[1]
    return fallback


def strict_ok(row: dict[str, Any]) -> bool:
    verification = row.get("verification") or {}
    return (
        verification.get("pass_to_fail") is True
        and verification.get("golden_repair_pass") is True
        and int(verification.get("p2p_buggy_failed") or 0) == 0
        and verification.get("p2p_repaired_pass") is True
    )


def load_injections(variant_dir: Path, variant: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(variant_dir.glob("shard_new_l1l2_*_20260613/verified_injection_results.jsonl")):
        for row in read_jsonl(path):
            iid = str(row.get("instance_id") or "")
            if not iid:
                continue
            copied = dict(row)
            copied["variant"] = variant_from_id(iid, variant)
            copied["source_instance_id_base"] = source_id(iid)
            copied["injection_source_file"] = str(path)
            rows[iid] = copied
    return rows


def load_verifications(variant_dir: Path, variant: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(variant_dir.glob("shard_new_l1l2_*_20260613/verified_verification_results.jsonl")):
        for row in read_jsonl(path):
            iid = str(row.get("instance_id") or "")
            if not iid:
                continue
            copied = dict(row)
            copied["variant"] = variant_from_id(iid, variant)
            copied["source_instance_id_base"] = source_id(iid)
            copied["verification_source_file"] = str(path)
            rows[iid] = copied
    return rows


def make_cell(
    source: str,
    variant: str,
    injection: dict[str, Any] | None,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    v = (verification or {}).get("verification") or {}
    gate = (injection or {}).get("v2_fidelity_gate") or {}
    return {
        "source_instance_id": source,
        "variant": variant,
        "repo": (injection or verification or {}).get("repo", ""),
        "injection_attempted": injection is not None,
        "injection_success": bool((injection or {}).get("success")),
        "injection_level": (injection or {}).get("injection_level", ""),
        "v2_gate_pass": bool(gate.get("pass_gate")),
        "v2_gate_score": gate.get("score"),
        "verification_attempted": verification is not None,
        "strict_verified": strict_ok(verification or {}),
        "pass_to_fail": v.get("pass_to_fail"),
        "golden_repair_pass": v.get("golden_repair_pass"),
        "p2p_buggy_failed": int(v.get("p2p_buggy_failed") or 0),
        "p2p_repaired_pass": v.get("p2p_repaired_pass"),
        "target_failed_count": len(v.get("actual_failed_tests") or []),
        "clean_p2p_count": int(v.get("clean_pass_to_pass_count") or 0),
        "failure_reason": (injection or {}).get("failure_reason", ""),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    run_root = Path(args.run_root)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    variant_dirs = {
        path.name: path
        for path in sorted(run_root.iterdir())
        if path.is_dir() and list(path.glob("shard_new_l1l2_*_20260613"))
    }

    cells: list[dict[str, Any]] = []
    all_sources: set[str] = set()
    by_variant_summary: dict[str, dict[str, Any]] = {}
    for variant, variant_dir in variant_dirs.items():
        injections = load_injections(variant_dir, variant)
        verifications = load_verifications(variant_dir, variant)
        sources = {
            source_id(str(row.get("instance_id") or ""))
            for row in list(injections.values()) + list(verifications.values())
            if row.get("instance_id")
        }
        all_sources.update(sources)
        for source in sorted(sources):
            injection = next(
                (row for row in injections.values() if row.get("source_instance_id_base") == source),
                None,
            )
            verification = next(
                (row for row in verifications.values() if row.get("source_instance_id_base") == source),
                None,
            )
            cells.append(make_cell(source, variant, injection, verification))

        by_variant_summary[variant] = {
            "injection_rows": len(injections),
            "injection_success": sum(1 for row in injections.values() if row.get("success") is True),
            "verification_rows": len(verifications),
            "strict_verified": sum(1 for row in verifications.values() if strict_ok(row)),
            "p2p_buggy_regression": sum(
                1
                for row in verifications.values()
                if int((row.get("verification") or {}).get("p2p_buggy_failed") or 0) != 0
            ),
            "golden_repair_fail": sum(
                1
                for row in verifications.values()
                if (row.get("verification") or {}).get("golden_repair_pass") is not True
            ),
        }

    by_source: dict[str, dict[str, Any]] = {}
    cells_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        cells_by_source[str(cell["source_instance_id"])].append(cell)
    for source, source_cells in sorted(cells_by_source.items()):
        strict_variants = sorted(cell["variant"] for cell in source_cells if cell["strict_verified"])
        by_source[source] = {
            "repo": next((cell["repo"] for cell in source_cells if cell.get("repo")), ""),
            "variants_attempted": sorted(cell["variant"] for cell in source_cells if cell["injection_attempted"]),
            "variants_verified": sorted(cell["variant"] for cell in source_cells if cell["verification_attempted"]),
            "strict_variants": strict_variants,
            "strict_variant_count": len(strict_variants),
        }

    summary = {
        "run_root": str(run_root),
        "variants": sorted(variant_dirs),
        "source_issue_count": len(all_sources),
        "by_variant": by_variant_summary,
        "strict_variant_count_distribution": dict(
            Counter(str(item["strict_variant_count"]) for item in by_source.values())
        ),
        "sources_with_multiple_strict_variants": sum(
            1 for item in by_source.values() if int(item["strict_variant_count"]) >= 2
        ),
    }
    write_jsonl(output_dir / "variant_matrix.jsonl", cells)
    write_csv(output_dir / "variant_matrix.csv", cells)
    (output_dir / "by_source.json").write_text(
        json.dumps(by_source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
