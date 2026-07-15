"""Filter JSONL candidate pools by excluding instance ids from other JSONL files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def read_ids(paths: list[Path]) -> set[str]:
    ids: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                iid = row.get("instance_id") or row.get("source_instance_id")
                if iid:
                    ids.add(iid)
    return ids


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()

    excluded = read_ids([Path(path) for path in args.exclude])
    kept = 0
    dropped = 0
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with Path(args.input).open(encoding="utf-8", errors="replace") as src, output.open(
        "w", encoding="utf-8"
    ) as dst:
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            iid = row.get("instance_id") or row.get("source_instance_id")
            if iid in excluded:
                dropped += 1
                continue
            dst.write(json.dumps(row, ensure_ascii=False) + "\n")
            kept += 1

    print(json.dumps({
        "input": args.input,
        "output": str(output),
        "excluded_ids": len(excluded),
        "kept": kept,
        "dropped": dropped,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
