# Bug Transplantation Workflow

This workflow constructs modern bug-fix tasks from historical repair tasks. The default target is a newer revision of the **same repository**, not an unrelated cross-repository transplant.

## Pipeline

1. **Candidate pool**: load SWE-bench family rows or normalized historical repair records; deduplicate by source identity; balance datasets and repositories.
2. **Preflight/cache**: verify checkout availability, installability, target-test collection, and healthy target behavior. Known unusable targets are skipped by cache key rather than rebuilt repeatedly.
3. **Injection cascade**:
   - L1: exact textual/Git reversion;
   - L2: AST-guided structural reversion;
   - L3: semantic reconstruction for code drift, optionally forced for high-complexity tasks.
4. **L3 ranking**: generate one or more candidates, run cheap patch-shape/compatibility/fidelity gates, then spend test time only on the strongest candidate. P2P failures are fed back into later attempts.
5. **Strict verification**: healthy pass, pass-to-fail, adjacent/P2P safety, golden repair, repaired P2P safety.
6. **Assembly**: enforce complexity fidelity, source diversity, repository caps, complete diff assets, and auditable rejection reasons.

Default fidelity thresholds are score `>= 0.65`, line ratio `0.50..2.50`, hunk ratio `>= 0.50`, file ratio `>= 0.50`, and regression-surface ratio `>= 0.25`. These are conservative defaults, not universal constants; report any changes.

## Main scripts

| Script | Role |
| --- | --- |
| `build_swe_family_candidate_pool.py` | Build and balance a normalized candidate pool |
| `build_prinjector_v2_preflight_cache.py` | Consolidate reusable environment/target preflight evidence |
| `inject_swebench_pro.py` | L1/L2/L3 injection engine, semantic contract, ranking, and fidelity checks |
| `verify_swebench_pro.py` | Strict P2F/P2P/gold-restoration verifier and shared environment cache |
| `run_rq2_b500_fidelity_new_l1l2_shards_20260613.py` | Parallel shard orchestrator (historical filename retained for reproducibility) |
| `assemble_prinjector_v2_b500.py` | Select an artifact-complete balanced set |
| `monitor_b500_assembly.py` | Incrementally assemble completed strict-verification rows |
| `run_direct_pro_agent_rescue.py` | Explicit agent rescue lane for unresolved difficult cases |

## Minimal construction run

```bash
export PYTHONPATH="$PWD/src:$PWD"

python construction_toolkit/bug_transplant/scripts/build_swe_family_candidate_pool.py \
  --dataset princeton-nlp/SWE-bench_Verified \
  --dataset ScaleAI/SWE-bench_Pro \
  --dataset SWE-bench-Live/SWE-bench-Live \
  --dataset SWE-bench/SWE-bench_Lite \
  --output-dir artifacts/bug-pool

python construction_toolkit/bug_transplant/scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py \
  --candidate-file artifacts/bug-pool/candidate_pool.jsonl \
  --output-root artifacts/bug-run \
  --workspace-slug reusable-bug-run \
  --workers 5 --shard-size 8 \
  --enable-l3 --v2-fidelity-gate --v2-require-fidelity-gate
```

Inspect the exact candidate filename emitted by the pool builder; dataset schemas may produce a source-specific name.

For semantic L3 through Agent Maestro:

```bash
export AGENT_MAESTRO_BASE_URL='http://127.0.0.1:23333'  # local host; tunnel ports may differ
export AGENT_MAESTRO_API_KEY="$(your-secret-store-command)"
export PRI_L3_PROVIDER='agent_maestro_anthropic'
export PRI_AGENT_MAESTRO_MODEL='claude-opus-4.8'
export PRI_ALLOW_L3_MODEL_CALLS=1
export PRI_L3_APPLY_ATTEMPTS=4
export PRI_L3_CANDIDATES_PER_ATTEMPT=2
export PRI_L3_REJECT_V2_GATE=1
export PRI_RETRY_V2_GATE_WITH_L3=1
```

Do not lower strict verification to increase yield. Improve candidate routing, semantic context, environment caches, and feedback instead.

## Accepted row contract

An accepted row must preserve source identity and include the target repository/revision, injection level, injected diff, golden repair, target tests, P2P tests, complexity/fidelity metrics, strict verification evidence, and machine-readable rejection provenance for failed attempts.
