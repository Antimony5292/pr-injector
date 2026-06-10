"""Build a retry candidate subset for target-test harness failures.

This is intentionally narrow: it only retries instances that failed before the
bug injection step because target tests were missing or not collectable. The
candidate row is rebuilt with the current candidate-pool normalizer, so cases
whose official FAIL_TO_PASS field contains docstrings can use tests extracted
from test_patch instead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_rq2_candidate_pool import _looks_runnable_test_id, _record


HARNESS_REASONS = (
    "test_files_missing",
    "target_nodeids_not_collectable",
)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--injection-results", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--source-dataset", default="")
    args = parser.parse_args()

    candidates = read_jsonl(Path(args.candidates))
    candidate_by_id = {row.get("instance_id"): row for row in candidates}
    failures = read_jsonl(Path(args.injection_results))

    retry_rows: list[dict] = []
    seen: set[str] = set()
    for failure in failures:
        reason = str(failure.get("failure_reason") or "")
        if not any(marker in reason for marker in HARNESS_REASONS):
            continue
        iid = failure.get("instance_id") or failure.get("source_instance_id")
        if not iid or iid in seen or iid not in candidate_by_id:
            continue
        raw = candidate_by_id[iid]
        source_dataset = args.source_dataset or raw.get("source_dataset", "")
        rebuilt = _record(raw, source_dataset)
        fail_to_pass = rebuilt.get("fail_to_pass") or []
        if not any(_looks_runnable_test_id(t) for t in fail_to_pass):
            continue
        if fail_to_pass == (raw.get("fail_to_pass") or []):
            continue
        rebuilt["retry_reason"] = reason
        rebuilt["retry_source"] = str(Path(args.injection_results))
        retry_rows.append(rebuilt)
        seen.add(iid)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as f:
        for row in retry_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"retry candidates: {len(retry_rows)}")
    print(f"output: {output}")


if __name__ == "__main__":
    main()
