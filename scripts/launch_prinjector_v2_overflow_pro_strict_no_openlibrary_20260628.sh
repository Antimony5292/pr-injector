#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="$ROOT/experiments/rq2_500/v2_overflow_pro_strict_no_openlibrary_20260628"
POOL="$ROOT/experiments/rq2_500/v2_overflow_pro_strict_no_openlibrary_pool_20260628/v2_main_construction_candidates.jsonl"

"$ROOT/.venv/bin/python" "$ROOT/scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py" \
  --output-root "$OUT" \
  --candidate-file "$POOL" \
  --limit 34 \
  --shard-size 17 \
  --workers 1 \
  --timeout 300 \
  --max-target-tests 8 \
  --max-pass-to-pass 50 \
  --max-adjacent-pass-to-pass 25 \
  --enable-l3 \
  --v2-require-fidelity-gate \
  --ignore-processed
