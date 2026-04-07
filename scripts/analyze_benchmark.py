"""Analyze benchmark.jsonl and display summary statistics."""

import json
import sys
import os
from collections import Counter
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def load_instances(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def print_divider(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def analyze(path: str) -> None:
    records = load_instances(path)
    if not records:
        print("No records found.")
        return

    # ── Overview ──
    print_divider("Overview")
    print(f"  Total instances : {len(records)}")
    print(f"  Unique repos    : {len(set(r['repo'] for r in records))}")
    has_verification = sum(
        1 for r in records
        if r.get("verification") and r["verification"].get("blast_radius_ok") is not None
    )
    print(f"  With verification: {has_verification}/{len(records)}")

    # ── Per-Repo Breakdown ──
    print_divider("Per-Repo Breakdown")
    repo_counter = Counter(r["repo"] for r in records)
    repo_levels: dict[str, Counter] = {}
    for r in records:
        repo = r["repo"]
        level = r.get("injection_level", "unknown")
        if repo not in repo_levels:
            repo_levels[repo] = Counter()
        repo_levels[repo][level] += 1

    header = f"{'Repo':<35} {'Total':>5}  {'L1':>4}  {'L2':>4}  {'L3':>4}"
    print(header)
    print("-" * len(header))
    for repo, total in repo_counter.most_common():
        lv = repo_levels[repo]
        l1 = lv.get("Level_1_Clean_Revert", 0)
        l2 = lv.get("Level_2_AST_Surgery", 0)
        l3 = lv.get("Level_3_LLM_Semantic", 0)
        print(f"  {repo:<33} {total:>5}  {l1:>4}  {l2:>4}  {l3:>4}")

    # ── Injection Level Distribution ──
    print_divider("Injection Level Distribution")
    level_counter = Counter(r.get("injection_level", "unknown") for r in records)
    level_order = [
        "Level_1_Clean_Revert",
        "Level_2_AST_Surgery",
        "Level_3_LLM_Semantic",
    ]
    total = len(records)
    for lv in level_order:
        cnt = level_counter.get(lv, 0)
        pct = cnt / total * 100
        bar = "█" * int(pct / 2)
        label = lv.replace("Level_1_Clean_Revert", "Level 1 (Git Revert)") \
                  .replace("Level_2_AST_Surgery", "Level 2 (AST Surgery)") \
                  .replace("Level_3_LLM_Semantic", "Level 3 (LLM Revert)")
        print(f"  {label:<25} {cnt:>3} ({pct:5.1f}%)  {bar}")

    # ── Verification Summary (if available) ──
    verified = [r for r in records if r.get("verification")]
    if verified:
        print_divider("Verification Summary")
        blast_ok = sum(1 for r in verified if r["verification"].get("blast_radius_ok"))
        blast_fail = len(verified) - blast_ok
        print(f"  Blast radius OK   : {blast_ok}")
        print(f"  Blast radius FAIL : {blast_fail}")
        print(f"  Verified rate     : {blast_ok}/{len(verified)} ({blast_ok/len(verified)*100:.1f}%)")

        # Average test duration
        durations = [
            r["verification"]["test_duration_seconds"]
            for r in verified
            if r["verification"].get("test_duration_seconds") is not None
        ]
        if durations:
            print(f"  Avg test duration : {sum(durations)/len(durations):.1f}s")

    # ── Instance List ──
    print_divider("Instance List")
    header = f"{'#':>3}  {'Instance ID':<45} {'Level':<25} {'Date':<12}"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(records, 1):
        iid = r["instance_id"][:44]
        level = r.get("injection_level", "?") \
                 .replace("Level_1_Clean_Revert", "L1 Git") \
                 .replace("Level_2_AST_Surgery", "L2 AST") \
                 .replace("Level_3_LLM_Semantic", "L3 LLM")
        date = r.get("created_at", "")[:10]
        print(f"  {i:>2}  {iid:<45} {level:<25} {date}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "benchmark_dataset/benchmark.jsonl"
    if not Path(path).exists():
        print(f"File not found: {path}")
        sys.exit(1)
    analyze(path)
