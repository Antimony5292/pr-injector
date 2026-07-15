from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from construction_toolkit.bug_transplant.scripts.run_rq2_b500_fidelity_new_l1l2_shards_20260613 import diverse_queue  # noqa: E402


def row(instance_id: str, repo: str, changed_lines: int) -> dict:
    changes = "\n".join(f"+line_{idx}" for idx in range(changed_lines))
    return {
        "instance_id": instance_id,
        "source_instance_id": instance_id,
        "source_dataset": "ScaleAI/SWE-bench_Pro",
        "repo": repo,
        "patch": f"diff --git a/a.py b/a.py\n@@ -1,1 +1,{changed_lines} @@\n{changes}\n",
    }


def test_viability_queue_prioritizes_medium_patches_and_round_robins_repos() -> None:
    rows = [
        row("a-xlarge", "a/repo", 400),
        row("a-medium", "a/repo", 40),
        row("b-large", "b/repo", 140),
        row("b-medium", "b/repo", 60),
    ]

    ordered = diverse_queue(rows, order="viability")

    assert [item["instance_id"] for item in ordered] == [
        "a-medium",
        "b-medium",
        "a-xlarge",
        "b-large",
    ]
