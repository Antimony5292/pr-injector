# Feature-Addition Task Construction

This workflow turns historical feature additions into modern `feature-missing` tasks. It constructs tasks only; coding-agent evaluation is deliberately excluded.

## Pipeline

1. **Source profiling**: normalize feature PR metadata, implementation/test patches, repositories, revisions, and candidate feature tests.
2. **Source gate**: reject missing/ambiguous patches, non-executable tests, unsuitable generated files, oversized/unbounded changes, and candidates without a defensible feature intent.
3. **Modern mapping**: checkout a modern target revision and map historical files, symbols, tests, and behavior to the evolved implementation context.
4. **Feature removal**: construct a semantic removal/disable diff. Deterministic handlers may be used for known patterns; otherwise a coding agent edits implementation files under a strict feature-removal contract.
5. **Feature fidelity**: compare the historical feature patch, modern feature-removal diff, test surface, changed symbols/files, and intent anchors. Weak pairings are rejected rather than labeled as equivalent.
6. **Strict verification**:
   - feature tests pass on the healthy modern revision;
   - feature tests fail on the feature-missing revision;
   - adjacent/P2P tests remain safe;
   - applying the gold restore makes feature tests and P2P tests pass again.
7. **Freeze/merge**: deduplicate strict rows, retain patch assets and provenance, and freeze the requested construction set.

Feature construction does not reuse bug-specific L1/L2/L3 labels. Agent-based semantic removal is analogous to the semantic transformation stage, but it has feature-specific fidelity and verification gates.

## Main scripts

| Script | Role |
| --- | --- |
| `profile_feainjector_candidates.py` | Source-level complexity and suitability profile |
| `build_feainjector_poc_manifest.py` | Select normalized source-gate candidates |
| `build_feature_external_source_manifest.py` | Build a source pool from compatible Hugging Face benchmarks |
| `build_feainjector_external_construction_queue.py` | Rank and diversify modern construction candidates |
| `construct_feainjector_modern_poc.py` | Deterministic/reviewed modern feature-removal handlers |
| `construct_feainjector_modern_semantic_model.py` | Agent-backed semantic feature removal with fidelity gate |
| `run_direct_feature_agent_rescue.py` | Coding-agent rescue for unresolved feature-removal cases |
| `verify_feainjector_feature_tasks.py` | Feature fail/P2P/gold-restore strict verifier |
| `freeze_feainjector_strict_set.py` | Freeze an artifact-complete strict set |
| `merge_feainjector_strict_cases.py` | Merge strict sources with fidelity deduplication |

## Minimal construction run

```bash
export PYTHONPATH="$PWD/src:$PWD"

python construction_toolkit/feature_addition/scripts/profile_feainjector_candidates.py \
  --input-jsonl inputs/feature_candidates.jsonl \
  --output-dir artifacts/feature-profile

python construction_toolkit/feature_addition/scripts/build_feainjector_poc_manifest.py \
  --profiles artifacts/feature-profile/candidate_profiles.jsonl \
  --full-rows inputs/feature_candidates.jsonl \
  --output-dir artifacts/feature-manifest \
  --limit 100

python construction_toolkit/feature_addition/scripts/construct_feainjector_modern_semantic_model.py \
  --manifest artifacts/feature-manifest/poc_candidates.jsonl \
  --output-dir artifacts/feature-construction \
  --repo-cache-root .pri-workspace/repos \
  --clone-missing-repos --workers 3

python construction_toolkit/feature_addition/scripts/verify_feainjector_feature_tasks.py \
  --manifest artifacts/feature-construction/modern_semantic_construction_results.jsonl \
  --output-dir artifacts/feature-verification \
  --repo-cache-root .pri-workspace/repos \
  --clone-missing-repos --shared-venv --workers 3 --resume

python construction_toolkit/feature_addition/scripts/freeze_feainjector_strict_set.py \
  --input artifacts/feature-verification/feature_verification_results.jsonl \
  --output-dir artifacts/feature-strict \
  --target-size 100
```

Use `--help` and inspect emitted filenames because source benchmarks use different schemas. Never freeze rows merely because an agent produced a diff; only strict verifier output is eligible.

## Accepted row contract

Each accepted task records source benchmark/issue/PR, repository and target revision, feature intent, source implementation/test patches, modern feature-missing patch, gold restore patch, feature tests, P2P tests, complexity/fidelity evidence, strict verifier results, transformation method/model metadata, and auditable rejection history.
