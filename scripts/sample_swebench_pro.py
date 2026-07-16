"""Sample 35 Python instances from SWE-bench Pro for PR-Injector experiment.

Samples ~12 instances from each of the 3 Python repos:
  - ansible/ansible (96 instances)
  - internetarchive/openlibrary (91 instances)
  - qutebrowser/qutebrowser (79 instances)

Output: experiments/swebench_pro/sampled_35.jsonl
"""

import json
import os
import random
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

from datasets import load_dataset


def extract_pr_number(instance: dict) -> int | None:
    """Try to extract PR number from instance_id or base_commit context.

    SWE-bench Pro instance_ids look like:
      instance_ansible__ansible-<hash>-<suffix>
    They don't contain PR numbers directly, so we need the patch info.
    """
    # The patch field contains the fix diff. We need to figure out
    # which PR this corresponds to. For now, store the instance as-is
    # and let the injection script resolve it.
    return None


def main():
    random.seed(42)

    print("Loading SWE-bench Pro dataset...")
    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")

    # Filter Python repos
    python_instances = [row for row in ds if row.get("repo_language") == "python"]
    print(f"Total Python instances: {len(python_instances)}")

    # Group by repo
    by_repo: dict[str, list] = {}
    for inst in python_instances:
        repo = inst["repo"]
        by_repo.setdefault(repo, []).append(inst)

    for repo, instances in by_repo.items():
        print(f"  {repo}: {len(instances)}")

    # Sample: 12 from each repo (12*3 = 36, drop 1 to get 35)
    sampled = []
    for repo, instances in sorted(by_repo.items()):
        n = 12 if len(sampled) < 24 else 11  # 12 + 12 + 11 = 35
        sample = random.sample(instances, min(n, len(instances)))
        sampled.extend(sample)
        print(f"  Sampled {len(sample)} from {repo}")

    sampled = sampled[:35]
    print(f"\nTotal sampled: {len(sampled)}")

    # Write output
    output_path = Path("experiments/swebench_pro/sampled_35.jsonl")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for inst in sampled:
            record = {
                "instance_id": inst["instance_id"],
                "repo": inst["repo"],
                "base_commit": inst["base_commit"],
                "patch": inst["patch"],
                "test_patch": inst["test_patch"],
                "problem_statement": inst["problem_statement"],
                "fail_to_pass": inst["fail_to_pass"],
                "pass_to_pass": inst["pass_to_pass"],
                "repo_language": inst["repo_language"],
                "selected_test_files_to_run": inst.get("selected_test_files_to_run", []),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(f"\nWritten to: {output_path}")

    # Summary
    print("\nSampled instances:")
    repo_counts: dict[str, int] = {}
    for inst in sampled:
        repo = inst["repo"]
        repo_counts[repo] = repo_counts.get(repo, 0) + 1
    for repo, cnt in sorted(repo_counts.items()):
        print(f"  {repo}: {cnt}")


if __name__ == "__main__":
    main()
