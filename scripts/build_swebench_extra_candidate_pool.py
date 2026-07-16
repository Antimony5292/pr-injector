"""Build an extra official SWE-bench candidate pool for larger RQ2 sets."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from datasets import load_dataset

from build_rq2_candidate_pool import _looks_usable, _record


def read_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    iid = row.get("instance_id") or row.get("source_instance_id")
                    if iid:
                        ids.add(iid)
    return ids


def round_robin_by_repo(records: list[dict], limit: int, max_per_repo: int) -> list[dict]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in records:
        repo = row.get("repo", "")
        if len(buckets[repo]) < max_per_repo:
            buckets[repo].append(row)

    selected: list[dict] = []
    repos = sorted(buckets, key=lambda repo: (-len(buckets[repo]), repo))
    while len(selected) < limit:
        added = False
        for repo in repos:
            bucket = buckets[repo]
            if bucket:
                selected.append(bucket.pop(0))
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
    return selected


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=900)
    parser.add_argument("--max-per-repo", type=int, default=80)
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--include-repo", action="append", default=[])
    parser.add_argument("--exclude-repo", action="append", default=[])
    args = parser.parse_args()

    exclude = read_ids([Path(path) for path in args.exclude])
    include_repos = set(args.include_repo or [])
    exclude_repos = set(args.exclude_repo or [])
    ds = load_dataset("princeton-nlp/SWE-bench", split="test")

    records: list[dict] = []
    seen: set[str] = set(exclude)
    for row in ds:
        iid = row.get("instance_id")
        if not iid or iid in seen:
            continue
        repo = row.get("repo", "")
        if include_repos and repo not in include_repos:
            continue
        if repo in exclude_repos:
            continue
        if not _looks_usable(row):
            continue
        rec = _record(row, "princeton-nlp/SWE-bench")
        records.append(rec)
        seen.add(iid)

    selected = round_robin_by_repo(records, args.limit, args.max_per_repo)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in selected:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    repo_counts: dict[str, int] = defaultdict(int)
    for row in selected:
        repo_counts[row["repo"]] += 1

    print(f"usable extra records: {len(records)}")
    print(f"selected records: {len(selected)}")
    print("top repos:")
    for repo, count in sorted(repo_counts.items(), key=lambda item: (-item[1], item[0]))[:20]:
        print(f"  {repo}: {count}")
    print(f"output: {output}")


if __name__ == "__main__":
    main()
