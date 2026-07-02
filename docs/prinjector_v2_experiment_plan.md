# PR-INJECTOR v2 Experiment Plan

## Goal

PR-INJECTOR v2 should be evaluated as a counterfactual repository-level agent
evaluation instrument, not as a script that merely creates more injected bugs.
The central claim is that a real historical defect can be migrated into a
modern healthy repository while preserving enough behavioral and structural
complexity to support paired A/B agent evaluation.

## Core Artifacts

Each accepted B instance must preserve a one-to-one link to its original A
instance and expose enough metadata for RQ1-RQ3.

Required per-case fields:

- `case_id`
- `source_dataset`
- `repo`
- `A_instance_id`
- `A_patch`
- `A_FAIL_TO_PASS`
- `A_PASS_TO_PASS`
- `B_instance_id`
- `B_healthy_head`
- `B_injected_diff`
- `B_golden_patch`
- `B_FAIL_TO_PASS`
- `B_PASS_TO_PASS_CLEAN`
- `B_injection_level`
- `complexity_profile`
- `fidelity_gate`
- `construction_metrics`
- `verification_metrics`
- `agent_eval_metrics`

## RQ1: Construction Capability

RQ1 asks how often PR-INJECTOR can construct verified counterfactual tasks from
historical issues.

Report a full funnel, not only final accepted cases:

- raw candidates by dataset and repo
- preflight pass/fail
- L1/L2/L3 attempts and successes
- P2F validation
- P2P/no-regression validation
- golden repair validation
- rejection reasons
- accepted-vs-failed complexity distributions

Every metric must report its denominator. If a metric covers only instrumented
subsets, the table must say so explicitly.

## RQ2: Counterfactual Behavioral Fidelity

RQ2 asks whether the same agent behaves consistently on the original historical
task A and the injected modern-context task B.

Required labels:

- `A_official_solved`
- `A_harness_status`
- `B_target_solved`
- `B_strict_solved`
- `B_status`
- `agent_timeout`
- `turn_limit`
- `harness_error`
- `forbidden_file_edit`

Primary analysis is case-wise quadrants:

- `A1B1`: original solved, injected solved
- `A0B0`: original unsolved, injected unsolved
- `A1B0`: original solved, injected unsolved
- `A0B1`: original unsolved, injected solved

Break quadrants down by dataset, repo, complexity bin, injection level,
fidelity tag, target/P2P test count, timeout, harness error, and forbidden
edits.

## RQ3: Cost, Robustness, and Failure Modes

RQ3 is not a separate benchmark run. It analyzes construction and evaluation
noise across RQ1/RQ2.

Track:

- construction runtime
- L1/L2/L3 runtime
- L3 token and cost records
- retry count
- verification runtime
- test command count
- target remapping failures
- harness failures
- agent elapsed time
- agent turns and cost
- forbidden edits
- timeout / infrastructure failures

Report metric coverage, for example `injection_metrics=173/500`, rather than
implicitly treating all fields as complete.

## v2 Fidelity Gate

The v2 construction loop must compare A and B before accepting a B case into
the main RQ2 set.

Patch complexity profile:

- touched files
- touched source files
- touched test/config files
- hunks
- added lines
- removed lines
- total line changes
- changed symbols/classes/functions when detectable
- target test count
- regression test count

Default gate:

- B line-change ratio must be at least `0.50` of A and at most `2.50` of A.
- B hunk ratio must be at least `0.50` of A.
- B source-file ratio must be at least `0.50` of A.
- B regression/P2P surface must not collapse when A has meaningful P2P.
- Combined fidelity score must be at least `0.65`.

Cases failing this gate are not thrown away silently. They become:

- L3 feedback-loop retry candidates, if injection may be improved
- construction failure analysis rows, if migration is not feasible
- non-main supplemental cases, if they are valid but simplified

## L3 Feedback Loop

Level 3 should not simply generate the smallest failing bug. It receives gate
feedback and is asked to preserve the original bug's structural footprint.

Feedback examples:

- `localized_simplified`: B has too few changed lines or hunks relative to A.
- `file_scope_simplified`: B touches fewer source files than A.
- `low_regression_surface`: B has too little P2P/adjacent test coverage.
- `hard_to_easy_collapse`: A was medium/large but B became tiny/small.

The loop stops when:

- P2F/P2P/golden repair pass and fidelity gate passes
- retry budget is exhausted
- target tests cannot be remapped
- modern API drift makes semantic migration unsafe

## Sampling Policy

The final RQ2 set should be stratified. The current B500 is kept as v1/pilot
because it is too Django-heavy and has clear source-dataset imbalance.

Recommended controls:

- maximum per-repo share: `10%` for B500/B1000 unless there is a documented
  reason to relax it
- explicit dataset quotas
- complexity-bin balance
- injection-level/fidelity-bin reporting
- preserve a rejected pool for RQ1 analysis

`scripts/build_prinjector_v2_audit.py` produces the gate labels.
`scripts/select_prinjector_v2_cases.py` consumes the audit rows and produces a
balanced selected set.

## Construction-Loop Integration

Implemented on 2026-06-25:

- `scripts/inject_swebench_pro.py` now supports `--v2-fidelity-gate` and
  `--v2-require-fidelity-gate`.
- Each successful L1/L2/L3 injection can write `v2_fidelity_gate`,
  `v2_fidelity_gate_pass`, and `v2_fidelity_feedback_prompt`.
- With `--v2-require-fidelity-gate --enable-l3`, L1/L2 gate failures reset the
  worktree and retry via L3 using the gate feedback. L3 outputs are also checked
  inside the generation loop; gate-failing L3 diffs are reversed and retried.
- `scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py` passes the v2
  gate flags through to injection shards.
- `scripts/build_rq2_300_final.py` supports `--require-v2-fidelity-gate`, which
  recomputes the gate after strict P2F/P2P/golden verification using the clean
  P2P surface.

Recommended new construction command shape:

```bash
.venv/bin/python scripts/materialize_prinjector_v2_retry_candidates.py \
  --retry-manifest experiments/rq2_500/v2_retry_manifest_from_b500_20260625/v2_retry_manifest.jsonl \
  --candidate-root experiments/rq2_300 \
  --candidate-root experiments/rq2_500 \
  --output experiments/rq2_500/v2_retry_manifest_from_b500_20260625/v2_retry_candidates.jsonl

.venv/bin/python scripts/run_rq2_b500_fidelity_new_l1l2_shards_20260613.py \
  --output-root experiments/rq2_500/v2_construction_YYYYMMDD \
  --candidate-file experiments/rq2_500/v2_retry_manifest_from_b500_20260625/v2_retry_candidates.jsonl \
  --ignore-processed \
  --enable-l3 \
  --v2-fidelity-gate \
  --v2-require-fidelity-gate
```

Current B500 interpretation under v2:

- `372/500` pass the per-case v2 A/B complexity-fidelity gate.
- `128/500` fail the per-case gate and should be rebuilt or retried.
- Only `185/500` can be selected into a balanced target-500 set under the current
  repo/dataset quota policy. The remaining gate-passing rows are mostly valid
  supplemental/pilot rows, but they are too concentrated in Django/SWE-bench for
  the main v2 B500.

## 2026-06-25 Live v2 Construction

Main run:

- screen: `pri_v2_b500_construct_20260625`
- run dir: `experiments/rq2_500/v2_construction_20260625`
- candidate queue: `experiments/rq2_500/v2_construction_pool_20260625/v2_main_construction_candidates.jsonl`
- queued rows: `450`
- target final set: locked `185` plus new strict/v2-gated construction rows
- model: AWS Bedrock Claude Sonnet 4.6 inference profile
- L3 gate mode: reject v2-gate-failing diffs inside the L3 generation loop

Live observation tracking:

- monitor screen: `pri_v2_obs_monitor_20260625`
- report: `experiments/rq2_500/v2_construction_20260625/observations/observations.md`
- machine-readable report: `experiments/rq2_500/v2_construction_20260625/observations/observations.json`
- monitor log: `experiments/rq2_500/logs/prinjector_v2_observation_monitor_20260625.log`

Early findings at `15` injection rows:

- no accepted construction rows yet; the first shard segment is dominated by
  preflight and L3-gate failures, so the front of the queue is hard rather than
  representative
- `healthy_target_not_executed`: modern build/test setup failures, including
  Meson editable rebuild failures in `matplotlib` and `scikit-learn`
- `healthy_target_failed`: healthy HEAD target tests failing before injection,
  including `xarray` pytest plugin/config mismatch symptoms
- `target_nodeids_not_remappable`: historical target tests no longer map cleanly
  in some `sympy` cases
- `python_version_unavailable`: `internetarchive/openlibrary` currently asks for
  `>=3.14.5,<3.14.6`, beyond the local available interpreter set
- L3 can still collapse multi-file/multi-hunk historical fixes into localized
  one-file diffs; the v2 gate is catching this
- L3 can emit malformed patch output under long/complex prompts, including prose
  inside a fenced diff; this is now recorded as `l3_diff_format_violation`

Framework fixes made from these observations:

- `scripts/collect_prinjector_v2_observations.py` records live construction
  issues into research-facing buckets instead of relying on ad hoc log reading.
- `scripts/inject_swebench_pro.py` now trims fenced L3 diffs with the same strict
  unified-diff parser as bare diffs, preventing prose lines from reaching
  `git apply`.
- The v2 construction launch script now defaults future L3 runs to
  `PRI_L3_MAX_TOKENS=8192` and `PRI_L3_TEMPERATURE=0`.
- `scripts/verify_swebench_pro.py` now maps pytest asyncio config/marker
  failures to `pytest-asyncio` during dependency bootstrap.

## 2026-06-26 Recovery and Supplemental Construction

Network recovery status:

- PyPI network access was restored and confirmed with an HTTP 200 probe against
  `https://pypi.org/simple/pytest/`.
- The first main construction segment produced many false infrastructure
  failures while the network was unavailable: `test_runner_unavailable` was
  dominated by PyPI/DNS resolution errors, and three L3 calls failed on the
  Bedrock endpoint.
- Those rows are not semantic PR-INJECTOR failures. They are treated as
  recoverable construction failures for RQ1 infrastructure accounting.

Active construction screens:

- `pri_v2_b500_construct_20260625`: original 450-row main v2 construction.
- `pri_v2_network_recovery_20260626`: 171-row recovery pool for PyPI/DNS and
  Bedrock endpoint failures.
- `pri_v2_l3_recovery_20260626`: 47-row recovery pool for L3 agent failures,
  including patch-apply drift, v2 gate collapse, and file-scope violations.
- `pri_v2_supplemental_relaxed_20260626`: 317-row supplemental pool using
  complexity floors and excluding already queued/current B500 IDs.

Current partial assembly preview:

- locked existing v2-balanced rows: `185`
- accepted new strict/v2 rows from completed construction wave: `48`
- current selectable preview: `233/500`
- selected by dataset: SWE-bench `202`, SWE-bench Pro `18`,
  SWE-bench Verified `13`
- remaining gap to target B500: `267`, concentrated in Pro and Verified
- final assembly rejects among verified rows: `p2f_miss=43`,
  `v2_fidelity_gate_failed=9`, `p2p_buggy_regression=20`,
  `p2p_repaired_not_pass=2`, `golden_repair_not_pass=1`,
  `injection_not_success=2`

Completed-wave construction summary as of 2026-06-28:

- total injection attempt rows across main/recovery/supplemental runs: `985`
- unique source candidates attempted: `711`
- injection successes entering verification: `145`
- strict verification rows: `145`
- final new strict/v2 accepted rows after assembly recheck and deduplication:
  `48`
- current usable v2 B set size including locked rows: `233/500`

Per-run injection success:

- main v2 construction: `34/450` (`7.56%`)
- network recovery: `34/171` (`19.88%`)
- L3 recovery: `6/47` (`12.77%`)
- supplemental relaxed: `71/317` (`22.40%`)

This wave confirms that simply adding more candidates is not sufficient. The
next pool must target Pro/Verified deficits and should be guided by environment
health and failure-mode labels, not just raw historical issue count.

New tooling added on 2026-06-26:

- `scripts/assemble_prinjector_v2_b500.py` accepts multiple
  `--construction-run-dir` values, so main construction, network recovery, L3
  recovery, and supplemental construction can be merged without manual JSONL
  stitching. It also deduplicates by source issue during final selection.
- `scripts/build_prinjector_v2_construction_pool.py` supports `--exclude-repo`,
  `--min-fresh-line-changes`, `--min-fresh-hunks`, and `--min-fresh-files` for
  targeted supplemental pools.
- `scripts/build_prinjector_v2_recovery_pool.py` supports `--exclude-id-file`
  and now classifies L3 patch-apply drift and file-scope violations as
  promptable recovery cases.
- `scripts/launch_prinjector_v2_l3_recovery_20260626.sh` starts the L3 recovery
  pool with one worker, `PRI_L3_MAX_TOKENS=8192`, temperature `0`, and a larger
  L3 apply retry budget.
- `scripts/launch_prinjector_v2_supplemental_relaxed_20260626.sh` starts the
  317-row relaxed supplemental pool with one worker.
- `scripts/launch_prinjector_v2_all_observation_monitor_20260626.sh` refreshes
  observation reports for all active v2 construction/recovery runs every five
  minutes.

Research observations to preserve:

- Environment automation is currently the dominant RQ1 bottleneck: test runner
  bootstrap, Python-version constraints, modern build backends, and healthy HEAD
  instability reject many cases before any injection is attempted.
- `internetarchive/openlibrary` is a clear modern-repo compatibility blocker on
  this machine because current repo metadata asks for Python
  `>=3.14.5,<3.14.6`.
- Verified/Pro coverage is candidate-limited and environment-heavy; after
  excluding the most problematic repos, very few fresh Verified candidates
  remain. This is evidence for the RepoLaunch complementarity framing.
- L3/LLM weaknesses are now visible as structured evidence: complexity collapse
  into localized diffs, stale patches against modern code, edits outside the
  allowed semantic file scope, and output-budget pressure on long prompts.

## 2026-06-28 Current B500 Rebuild Status

The current v2 accepted preview is:

- selected artifact: `experiments/rq2_500/v2_current233_20260628`
- selected rows: `233/500`
- strict verification pass: `233/233`
- final v2 gate pass: `233/233`
- diff assets present: `233/233`
- injected diffs touching tests: `0`
- required construction/evaluation metadata coverage: `233/233` for the
  selected-set fields audited so far
- unit tests after the construction-script changes: `57 passed`

Interpretation:

- These `233` rows are valid for the current v2 accepted set under the strict
  behavior gate and the A/B complexity-fidelity gate.
- They are not yet a complete or balanced B500. Dataset balance is still the
  limiting issue: SWE-bench `202`, SWE-bench Verified `13`, SWE-bench Pro `18`.
- Remaining deficits to the target quotas are SWE-bench `48`, Verified `112`,
  and Pro `107`.
- Repo diversity is acceptable for the current preview under the `50` per-repo
  cap. Current largest repos are Django `50`, pytest `41`, astropy `38`,
  sphinx `31`, pylint `20`, seaborn `16`, qutebrowser `12`, xarray `11`.

Two additional construction waves are running for the missing `267` rows:

- fresh gap wave:
  `experiments/rq2_500/v2_gap267_fresh_20260628`
  from
  `experiments/rq2_500/v2_gap267_pool_20260628/v2_main_construction_candidates.jsonl`
  with `204` candidates and `4` workers.
- Pro/Verified second-chance wave:
  `experiments/rq2_500/v2_second_chance_pro_verified_20260628`
  from
  `experiments/rq2_500/v2_second_chance_pro_verified_pool_20260628/second_chance_candidates.jsonl`
  with `163` candidates and `3` workers.

The second-chance pool is more important for the final target because it is
focused on the current Pro/Verified deficit: Pro `116`, Verified `47`. It uses
structured retry prompts for L3 patch-apply drift, v2-gate failures, file-scope
violations, no-op diffs, P2F misses, P2P regressions, and golden-repair misses.

Two overflow waves were added after inspecting the remaining candidate pool:

- Verified strict replacement:
  `experiments/rq2_500/v2_overflow_verified_strict_20260628`
  from
  `experiments/rq2_500/v2_overflow_verified_strict_pool_20260628/v2_main_construction_candidates.jsonl`
  with `59` candidates and `1` worker.
- Pro relaxed non-openlibrary:
  `experiments/rq2_500/v2_overflow_pro_relaxed_20260628`
  from
  `experiments/rq2_500/v2_overflow_pro_relaxed_pool_20260628/v2_main_construction_candidates.jsonl`
  with `9` candidates and `1` worker.

The Verified overflow pool is intentionally a replacement pool: all remaining
strict Verified candidates are Django cases, while Django is already at the
`50` repo cap in current233. They must replace existing Django/SWE rows during
final assembly rather than increasing Django's final share.

Live observations during the injection stage of these waves:

- fresh gap wave: `126` injection rows, `5` v2-gate-passing injection successes,
  `0` strict verification rows so far.
- Pro/Verified second chance: `58` injection rows, `11` v2-gate-passing
  injection successes, `0` strict verification rows so far.
- overflow Verified strict replacement: `28` injection rows, `8`
  v2-gate-passing injection successes, `6` verification rows, `4` strict
  verification rows.
- overflow Pro relaxed non-openlibrary: `9` injection rows, `2`
  v2-gate-passing injection successes, `2` verification rows, `1` strict
  verification row.
- multi-variant pilot L1-clean-revert segment: `12` injection rows, `7`
  v2-gate-passing injection successes, `7` verification rows satisfying the
  strict P2F/P2P/golden checks before variant-level deduplication.
- multi-variant pilot L2-AST segment: `12` injection rows, `7`
  v2-gate-passing injection successes, `1` verification row so far.

These early rows should not be counted into B500 until verification produces
strict P2F/P2P/golden rows and final assembly rechecks the v2 gate.

Incremental assembly preview after adding current overflow rows:

- preview dir:
  `experiments/rq2_500/v2_live_assembly_preview_replacement_20260628`
- locked baseline: current `233`
- accepted new rows after strict verification and final v2 gate recheck: `54`
  (`Verified=13`, `SWE=32`, `Pro=9`)
- selected preview with repo-cap replacement enabled: `235/500`
- selected by dataset: SWE-bench `199`, Verified `17`, Pro `19`
- repo-cap replacements: `4`, all used to improve dataset balance without
  exceeding the `50` per-repo cap.

This preview exposed an important framework issue and fix: the assembler must
support replacing locked rows within a saturated repo when the new row improves
dataset balance. `scripts/assemble_prinjector_v2_b500.py` now supports
`--allow-repo-cap-replacement` and can use full selected rows, such as
`v2_current233_20260628/selected.jsonl`, as the locked baseline for incremental
assembly.

Current high-signal failure/weakness labels:

- repo/environment: dependency bootstrap fragility, healthy HEAD target-test
  instability, unavailable Python constraints, target nodeids no longer
  remappable, and build backend/test runner failures before injection.
- PR-INJECTOR framework: candidate pools can be dataset-limited after repo caps
  and exclusions; fresh candidates alone skew back to SWE-bench and do not solve
  Pro/Verified coverage.
- PR-INJECTOR framework: the original fresh-pool ordering could starve
  Verified/Pro candidates because SWE-bench rows consumed shared repo caps first.
  `scripts/build_prinjector_v2_construction_pool.py` now prioritizes datasets by
  current deficit and supports `--allow-repo-cap-replacement` plus
  `--include-dataset` for targeted overflow pools.
- LLM/L3: semantic patch attempts often drift to stale line numbers, fail
  `git apply`, collapse multi-hunk or multi-file A patches into localized B
  diffs, or emit malformed diff/prose mixtures under long prompts.

New tooling added for this wave:

- `scripts/audit_prinjector_v2_selected_set.py` audits selected-set readiness,
  required metadata, strict verification, final v2 gates, repo caps, dataset
  deficits, and diff assets.
- `scripts/build_prinjector_v2_second_chance_pool.py` converts failed injection
  and verification rows into retry candidates with structured feedback prompts.
- `scripts/build_repolaunch_prinjector_matrix.py` creates the shared candidate
  matrix for the RepoLaunch x PR-INJECTOR complementarity experiment.
- `scripts/build_prinjector_multivariant_pilot.py` builds L1/L2/L3/expanded-P2P
  variant pools for the same source issues.
- `scripts/inject_swebench_pro.py` now supports `PRI_SKIP_L1=1` and
  `PRI_SKIP_L2=1`, allowing controlled variant generation instead of only the
  default L1->L2->L3 cascade.
- `scripts/build_repolaunch_smoke_inputs.py` creates a small, diverse
  RepoLaunch dataset/config from the coverage matrix.
- `scripts/merge_repolaunch_results.py` merges RepoLaunch `setup.jsonl` and
  per-instance `result.json` files back into the PR-INJECTOR coverage matrix.
- `scripts/launch_repolaunch_prinjector_smoke_20260628.sh` runs a Bedrock
  Sonnet 4.6 RepoLaunch smoke test and merges results when the run completes.

## RepoLaunch Complementarity Experiment

Run RepoLaunch and PR-INJECTOR on the same historical issue pool.

Required columns:

- `source_dataset`
- `instance_id`
- `repo`
- `base_commit`
- `RepoLaunch_success`
- `RepoLaunch_failure_reason`
- `PRInjector_success`
- `PRInjector_failure_reason`
- `combined_available`

Primary 2x2:

| | PR-INJECTOR success | PR-INJECTOR fail |
|---|---:|---:|
| RepoLaunch success | Both can evaluate | Historical env works, bug migration failed |
| RepoLaunch fail | PR-INJECTOR bypasses env rot | Neither works |

This frames PR-INJECTOR as complementary to environment launch, not a replacement
for RepoLaunch.

Current implementation status:

- output dir: `experiments/repolaunch_prinjector_coverage_20260628`
- matrix rows: `1168`
- PR-INJECTOR side is populated from all current candidate/run artifacts.
- RepoLaunch official runner has been cloned to `.external/RepoLaunch` and
  installed into `.venvs/repolaunch`.
- Local runner patch: `.external/RepoLaunch/launch/agent/state.py` now uses a
  disabled-search fallback when `TAVILY_API_KEY` is absent. This allows local
  environment-launch smoke runs, but full paper-faithful RepoLaunch should still
  use Tavily.
- Bedrock LiteLLM preflight passes for
  `bedrock/global.anthropic.claude-sonnet-4-6`.
- Smoke run screen:
  `repolaunch_prinjector_smoke_20260628`.
- Smoke dataset/config:
  `experiments/repolaunch_prinjector_coverage_20260628/repolaunch_20260628_smoke`.
- Smoke sample: `psf/requests`, `mwaskom/seaborn`, `pytest-dev/pytest`.
- Smoke partial result: `psf__requests-6028` succeeded. RepoLaunch selected
  `python:3.8`, built the environment, ran verification with `529 passed`,
  `12 skipped`, and `1 xfailed`, committed Docker image
  `repolaunch/prinjector:psf__requests-6028_linux`, and wrote `result.json`.
- Partial merged matrix:
  `experiments/repolaunch_prinjector_coverage_20260628/repolaunch_20260628_smoke/partial_merged`.
  Current smoke quadrants: `both_success=1`, `RepoLaunch_not_run=1167`.
- Smoke is continuing on `mwaskom__seaborn-2457`.
- New observation from the `seaborn` smoke: RepoLaunch's setup agent can treat a
  mostly passing test suite as acceptable even when failures remain. In this
  smoke, it observed `7` failed/error lines out of `2706` parsed test-result
  lines and reasoned that this was "well over 99% passing", outputting
  `<issue>None</issue>`. This is useful evidence that environment automation
  success needs an explicit benchmark-facing success policy, not only the
  setup agent's qualitative judgment.
- current PR-INJECTOR attempt rows in the matrix: `959`
- current PR-INJECTOR strict verified rows in the matrix: `239`
- current selected rows represented in the matrix: `233`
- primary PR-INJECTOR failure buckets include not attempted, healthy target not
  executed, L3 patch-apply failure, target-nodeid remapping gap, v2 gate failure,
  healthy target failure, Python version unavailable, P2F miss, and P2P
  regression.

## Extension Experiments

Prioritize one extension after the main v2 B set is stable:

1. Same issue, multiple B variants: L1/L2/L3/expanded-P2P variants for the same
   A issue.
2. Temporal robustness: inject the same historical issue across multiple
   repository releases or commits.
3. Hard-negative / regression-sensitive cases: target tests pass easily but
   adjacent/P2P tests reveal overfitting.
4. Long-tail or wild PR construction: non-benchmark PRs.
5. Non-bug PR tasks: refactor, feature addition, API behavior change, migration.

Do not let extension experiments block the main A/B paired evidence.

Current implementation status:

- same issue / multiple B variants pilot is running in
  `experiments/rq2_500/v2_multivariant_pilot_runs_20260628`.
- source pilot pool:
  `experiments/rq2_500/v2_multivariant_pilot_20260628/pilot_source_cases.jsonl`
  with `12` source issues and `48` variant candidate rows.
- variant pools:
  `candidate_pool_l1_clean_revert.jsonl`,
  `candidate_pool_l2_ast_surgery.jsonl`,
  `candidate_pool_l3_semantic.jsonl`, and
  `candidate_pool_l3_expanded_p2p.jsonl`.
- This pilot is intentionally lower priority than finishing B500; it is running
  with one worker so it does not starve the main construction waves.

### Feature-Addition Extension: FEA-INJECTOR Pilot

FEA-Bench (`arXiv:2503.06680`, official repo
`.external/FEA-Bench`) evaluates repository-level feature implementation rather
than bug fixing. Its official dataset contains `1,401` task instances from `83`
Python repositories, with a `200`-instance Lite subset. Each task is built from a
feature-oriented GitHub pull request and includes:

- `repo`, `pull_number`, `base_commit`, and environment metadata.
- `patch`: the reference feature implementation.
- `test_patch`: tests that check the new feature.
- `FAIL_TO_PASS` and `PASS_TO_PASS`.
- natural prompts and new component definitions extracted from newly added
  functions/classes.

The right PR-INJECTOR extension is not another bug-revert pipeline. It should
construct modern feature-addition tasks:

1. Source A from FEA-Bench Lite/Standard: original feature PR, test patch, gold
   feature patch, feature request text, and new component definitions.
2. Choose a modern target revision where the repository can build. RepoLaunch
   can be used first to provide a containerized modern environment.
3. Transplant the feature tests and request to the modern revision.
4. Withhold or remove the feature implementation from the modern target so the
   task requires additive implementation, not repair.
5. Validate gates:
   - target feature tests fail before implementation; missing symbol failures
     such as `ImportError` or `AttributeError` are acceptable for absent new
     components.
   - gold feature patch or ported gold patch passes target feature tests.
   - adjacent/P2P tests pass before and after gold patch.
   - feature complexity/fidelity matches A: added component count, added files,
     added source lines, edited files, hunks, and PASS/FAIL test surface must
     stay within thresholds.
6. Evaluate agents on A-feature vs B-feature pairs using the same model,
   harness, timeout, and hidden tests.

This does not need to wait for B500 to start. Recommended schedule:

- Start a `20-50` case FEA-INJECTOR pilot now from FeaBench Lite, using
  lightweight repos first and recording all feature-complexity fields.
- Keep the full `Feature-B500` scale-up behind the main bug-fix B500 because it
  will need separate construction quotas, environment handling, and paper
  space.
- Treat this as a parallel capability claim: PR-INJECTOR can produce benchmark
  tasks beyond bug fixing. Do not mix feature-addition solve rates into the main
  RQ2 bug-fix A/B numbers.

Current pilot preparation:

- script: `scripts/build_feainjector_pilot_manifest.py`
- output dir: `experiments/feainjector_pilot_20260628`
- full Lite IDs parsed: `200`
- Lite repos: `48`
- pilot manifest: `50` rows across `19` repos
- pilot overlap with current PR-INJECTOR B repos: `16` rows
- pilot priority distribution: priority `0` = `15`, priority `1` = `24`,
  priority `2` = `1`, priority `3` = `10`
- pilot top repos: `astropy/astropy=8`, `joke2k/faker=8`,
  `matplotlib/matplotlib=7`, `sphinx-doc/sphinx=4`, `Textualize/rich=4`
- FeaBench official input list:
  `experiments/feainjector_pilot_20260628/instances_pilot50.json`

The pilot manifest is intentionally only a planning/scrape manifest. Rows are
not feature-addition benchmark instances until FeaBench full fields are
materialized and the modern feature-addition gates above pass.

FeaBench scrape smoke:

- script: `scripts/run_feainjector_pilot_scrape.py`
- venv: `.venvs/feainjector`
- output dir: `experiments/feainjector_pilot_20260628/scrape_smoke2`
- requested/processed: `2/2`
- processed IDs: `astropy__astropy-13094`, `astropy__astropy-7970`
- generated datasets:
  `FEA-INJECTOR-pilot-Standard`, `FEA-INJECTOR-pilot-Oracle`,
  `FEA-INJECTOR-pilot-Lite-Standard`, and
  `FEA-INJECTOR-pilot-Lite-Oracle`
- field audit:
  - `astropy__astropy-13094`: patch chars `1083`, test patch chars `1150`,
    `FAIL_TO_PASS=1`, `PASS_TO_PASS=63`, new component count `3`
  - `astropy__astropy-7970`: patch chars `5641`, test patch chars `1527`,
    `FAIL_TO_PASS=1`, `PASS_TO_PASS=62`, new component count `1`

This confirms that the FeaBench data path is operational locally without
installing heavy `vllm` dependencies. The next implementation step is a
feature-addition construction gate that ports/removes modern feature
implementations and verifies target-fail/gold-pass/P2P behavior.

## 2026-06-28 07:55 KST Refresh

Active background screens:

- `repolaunch_prinjector_smoke_20260628`
- `pri_v2_current_wave_monitor_20260628`
- `pri_v2_overflow_monitor_20260628`
- `pri_v2_gap267_fresh_20260628`
- `pri_v2_second_chance_pro_verified_20260628`
- `pri_v2_multivariant_pilot_20260628`
- `pri_v2_overflow_pro_strict_no_openlibrary_20260628`
- `feainjector_pilot20_scrape_20260628`

Latest B500 assembly preview:

- preview dir:
  `experiments/rq2_500/v2_live_assembly_preview_replacement_refresh2_20260628`
- status: `insufficient_rows`
- selected: `235/500`
- locked baseline: `233`
- accepted new strict/v2 rows available to the assembler: `59`
- accepted new by dataset: Verified `18`, SWE-bench `32`, Pro `9`
- selected by dataset after repo-cap replacement: SWE-bench `194`, Verified
  `22`, Pro `19`
- remaining quota deficits: SWE-bench `56`, Verified `103`, Pro `106`
- repo-cap replacements: `9`
- dominant rejects after final assembly recheck: `p2f_miss=57`,
  `p2p_buggy_regression=23`, `v2_fidelity_gate_failed=11`,
  `injection_not_success=6`, `p2p_repaired_not_pass=3`,
  `golden_repair_not_pass=1`

Completed/updated construction waves:

- `v2_overflow_verified_strict_20260628` completed all `3` shards:
  `59` injection rows and `20` verification rows.
- Final assembly only gained one additional Verified selected row because this
  pool is Django-only and Django is already at the `50` repo cap. The
  replacement logic is working, but a Django-only Verified pool cannot solve the
  Verified deficit by itself.
- `v2_gap267_fresh_20260628` now has `190` injection rows and remains useful
  mainly for SWE coverage, not the Pro/Verified deficit.
- `v2_second_chance_pro_verified_20260628` now has `90` injection rows and is
  still the most important active wave for the Pro/Verified gap.

New Pro-focused pool and run:

- `experiments/rq2_500/v2_overflow_pro_strict_pool_20260628` can produce `122`
  Pro candidates, but `internetarchive/openlibrary` would take `50` slots and
  reintroduce a weak, over-concentrated repo.
- A no-openlibrary version was created instead:
  `experiments/rq2_500/v2_overflow_pro_strict_no_openlibrary_pool_20260628`.
- It contains `34` Pro candidates: `ansible/ansible=26`,
  `qutebrowser/qutebrowser=8`.
- The low-concurrency run
  `experiments/rq2_500/v2_overflow_pro_strict_no_openlibrary_20260628` is now
  active with `2` shards and `1` worker, using v2 required fidelity gate plus
  L3 retry.

Multivariant pilot refresh:

- summary dir:
  `experiments/rq2_500/v2_multivariant_pilot_runs_20260628/summary`
- L1 clean revert: `12` injection rows, `7` successes, `7` verification rows,
  `7` strict verified.
- L2 AST surgery: `12` injection rows, `7` successes, `7` verification rows,
  `6` strict verified and `1` P2P regression.
- L3 semantic: `12` injection rows, `9` successes, `5` verification rows, `4`
  strict verified, `1` golden repair failure.
- Current strict variant count distribution across source issues:
  `3 variants=3`, `2 variants=3`, `1 variant=2`, `0 variants=4`.
- This already supports the pilot claim that same historical issue, different
  construction surface changes strict verifiability and regression behavior.

RepoLaunch x PR-INJECTOR refresh:

- partial merged matrix remains:
  `experiments/repolaunch_prinjector_coverage_20260628/repolaunch_20260628_smoke/partial_merged`
- current smoke quadrants: `both_success=1`, `RepoLaunch_not_run=1167`.
- `psf__requests-6028` is the completed success.
- The next RepoLaunch smoke is still running through Docker-based setup. The
  earlier `mwaskom__seaborn-2457` log remains a useful weakness observation:
  RepoLaunch can over-accept a mostly passing suite unless we enforce an
  explicit benchmark-facing success policy.

FeaBench / FEA-INJECTOR refresh:

- New script:
  `scripts/profile_feainjector_candidates.py`.
- It computes feature source patch profile, feature test patch profile,
  FAIL_TO_PASS/PASS_TO_PASS counts, new-component counts, hard-negative
  candidate tags, and a lightweight feature pilot gate.
- Smoke profile output:
  `experiments/feainjector_pilot_20260628/scrape_smoke2/profiles`.
- The two smoke rows both pass the feature pilot gate and both are
  hard-negative candidates.
- New background pilot:
  `scripts/launch_feainjector_pilot20_scrape_20260628.sh` running in screen
  `feainjector_pilot20_scrape_20260628`.
- Pilot20 output dir:
  `experiments/feainjector_pilot_20260628/scrape_pilot20`.

Live observation artifacts:

- `experiments/rq2_500/live_observations_20260628/v2_gap267_fresh`
- `experiments/rq2_500/live_observations_20260628/v2_second_chance_pro_verified`
- `experiments/rq2_500/live_observations_20260628/v2_overflow_verified_strict`
- `experiments/rq2_500/live_observations_20260628/v2_overflow_pro_relaxed`

The observation buckets are now part of the experiment record. Current repeated
weaknesses include healthy HEAD instability, target remapping gaps, dependency
bootstrap fragility, L3 patch-apply drift, L3 complexity collapse, L3 malformed
diff/prose mixtures, and output-budget pressure.
