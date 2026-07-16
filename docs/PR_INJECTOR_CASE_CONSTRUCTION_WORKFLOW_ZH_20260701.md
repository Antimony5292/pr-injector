# PR-INJECTOR 造 Case 工作流中文说明

当前 PR-INJECTOR v2 在构造 B 组 benchmark instance 时的实际 workflow。这里的 B 组指把 SWE-bench、SWE-bench Verified、SWE-bench Pro 中的历史 bug-fix case，迁移到现代 repo revision 上，构造一个新的、现代代码上下文中的 injected bug 任务。

## 总目标

我们不是简单生成一个会失败的测试，也不是只把旧 patch 反向应用到新代码。目标是构造一个高质量 B instance，使它满足下面条件：

1. 它来自一个官方 A instance，有一一对应的 `source_instance_id`。
2. 它在现代 repo HEAD 或现代目标 revision 上工作。
3. 它表达的 bug 语义和 A instance 大致一致。
4. 它的复杂度不能比 A instance 明显塌缩成简单 toy bug。
5. 干净现代代码上 target tests 要通过。
6. 注入 bug 后 target tests 要失败，也就是 pass-to-fail。
7. 注入 bug 后 P2P/adjacent tests 不应该因为无关破坏而失败。
8. 反向 gold patch 修复后 target tests 和 P2P tests 都应该恢复通过。
9. 最终 B500 要满足 dataset quota、repo cap、复杂度和元数据完整性要求。

## 输入数据

每个候选 case 通常来自以下三个官方来源：

- `princeton-nlp/SWE-bench`
- `princeton-nlp/SWE-bench_Verified`
- `ScaleAI/SWE-bench_Pro`

每条候选至少需要这些字段：

- `source_instance_id` 或 `instance_id`：官方 A case 的 ID。
- `source_dataset`：来自哪个官方 benchmark。
- `repo`：例如 `django/django`、`astropy/astropy`。
- `base_commit`：官方 A case 的历史 base commit。
- `patch`：官方 gold bug-fix patch。
- `test_patch`：官方测试 patch。
- `problem_statement`：官方 issue/task 描述。
- `FAIL_TO_PASS` 或 `fail_to_pass`：官方目标失败测试。
- `PASS_TO_PASS` 或 `pass_to_pass`：官方保持通过测试。

当前主要候选池位置包括：

- `experiments/rq2_500/pro_verified_raw_pool_20260701/`
- `experiments/rq2_500/v2_wave2_fresh_pool_20260701/`
- 早期历史候选池和 retry pool 在 `experiments/rq2_500/` 下。

## Step 1: 候选池筛选

候选池不是直接全部跑。我们先根据当前 B500 缺口做筛选：

- dataset quota：目标是 SWE 250、Verified 125、Pro 125。
- repo cap：默认单个 repo 最多 50 条，避免 Django 或 pytest 过度支配。
- 去重：已经被选入 partial/final set 的 ID 不再进入新队列。
- 已尝试排除：上一波已尝试但明显失败的 ID 可以从 fresh wave 中排除。
- repo 排除：例如当前发现 `internetarchive/openlibrary` 因 Python `>=3.14.5,<3.14.6` 在本机不可用，暂时排除。
- 复杂度底线：例如 wave2 fresh pool 要求 A patch 至少 10 行变化、2 个 hunk、1 个源码文件。

当前 fresh wave2 使用脚本：

- `scripts/build_prinjector_v2_wave2_fresh_pool.py`

输出：

- `experiments/rq2_500/v2_wave2_fresh_pool_20260701/v2_wave2_fresh_candidates.jsonl`
- `experiments/rq2_500/v2_wave2_fresh_pool_20260701/summary.json`

这个 summary 记录：

- 输入候选数。
- 排除 ID 数。
- 因复杂度太低被过滤的数量。
- 最终 queued 数量。
- dataset/repo 分布。
- 平均 patch line changes、hunks、source files。

## Step 2: 准备现代 repo

PR-INJECTOR 使用本地 repo cache：

- `.pri-workspace/repos/`

每个候选会在工作目录中创建独立 worktree，避免污染 cache。construction runner 会把每个 shard 的工作目录放到：

- `.pri-workspace/<run_slug>_worktrees/`

如果 repo cache 不存在，case 会失败或被记录为 cache missing。这个失败属于基础设施/本地缓存问题，不属于 bug transplantation 语义失败。

## Step 3: Preflight 健康检查

preflight 的目的，是确认现代目标 repo 在注入 bug 前本身是健康的。核心检查包括：

1. 能否 checkout/准备 repo。
2. 能否找到合适 Python/runtime。
3. 能否安装或复用依赖环境。
4. 官方 target tests 能否 remap 到现代 repo。
5. target tests 在干净现代代码上能否 collect。
6. target tests 在干净现代代码上能否通过。

常见 preflight 失败原因：

- `python_version_unavailable`：现代 repo 要求的 Python 版本本机没有。
- `healthy_target_failed`：干净现代代码上 target tests 已经失败。
- `target_nodeids_not_remappable`：旧测试 ID 无法映射到现代测试。
- `healthy_target_not_executed`：测试没有被真正执行。
- `target_nodeids_not_collectable`：pytest collect 阶段就找不到测试。

preflight 失败的 case 不应该进入 B500，因为它无法证明是注入 bug 导致 target fail。

## Step 4: L1 注入

L1 是最接近机械方法的注入方式。

直观理解：

- A patch 是官方修 bug 的 patch。
- 注入 bug 相当于把这个修复在现代代码上反向还原。
- 如果旧 patch 和现代代码仍然高度一致，L1 可能直接成功。

优点：

- 可解释性强。
- gold repair 通常就是 injected diff 的反向 patch。
- 不容易引入过多无关改动。

缺点：

- 现代代码一旦重构，旧 patch 很可能无法直接应用。
- 容易只覆盖“代码变化很小”的 case，导致 B 组复杂度偏低。

## Step 5: L2 AST/hunk surgery

L2 用更灵活的结构化方式处理旧 patch 和现代代码的差异。

它会尝试：

- 根据 hunk 上下文找到现代对应位置。
- 在同名或相关函数/类附近做局部手术。
- 尽量保留旧 bug 的语义，而不是完全照搬旧行号。

当前配置偏保守：

- `PRI_LEVEL2_MODE=conservative_hunk_first`
- `PRI_ALLOW_WHOLE_FUNCTION_LEVEL2=0`

这样做是为了避免 L2 直接替换整个函数，制造过宽、过简单或不可信的 bug。

## Step 6: L3 语义注入

如果 L1/L2 失败，或者生成的 B diff 没有通过复杂度 gate，可以进入 L3。

L3 当前使用 AWS Bedrock Sonnet 4.6：

- `PRI_ALLOW_L3_MODEL_CALLS=1`
- `PRI_BEDROCK_MODEL=arn:aws:bedrock:us-west-2:497589205881:inference-profile/global.anthropic.claude-sonnet-4-6`

L3 会把历史 patch、现代代码上下文、目标测试、失败反馈、v2 gate 反馈给模型，让模型生成一个现代代码上的 semantic injected diff。

L3 不是无条件接受。它必须满足：

- diff 能干净 apply。
- diff 不能碰测试文件。
- diff 不能明显越界到无关文件。
- diff 不能是 no-op。
- diff 不能把 bug 简化到低于复杂度要求。
- diff 必须通过 target/P2P/golden repair 验证。

常见 L3 失败：

- 生成 diff 无法 apply。
- 生成 diff 触碰了不允许的文件。
- 生成 diff 过于简单，v2 fidelity gate fail。
- target tests 没有 fail，说明 bug 没注进去。
- target fail 了，但 P2P 也坏了，说明 bug 太宽。

## Step 7: v2 Complexity/Fidelity Gate

这是本轮迭代最关键的质量门。

我们不希望 B 组变成比 A 组简单很多的任务，所以每造出一个 B，就比较 A patch 和 B injected diff。

比较维度包括：

- source files 数量。
- hunk 数量。
- line changes 数量。
- target FAIL_TO_PASS 数量。
- PASS_TO_PASS/P2P surface。
- regression surface ratio。
- injection level。
- 是否触碰测试文件。
- 是否过度简化。

核心脚本：

- `scripts/prinjector_v2_metrics.py`

当前 assembler 默认要求：

- 最低 v2 score 约 `0.65`。
- line ratio、hunk ratio、file ratio、regression ratio 都有底线。

如果 B 的复杂度明显低于 A，就会被拒绝，或者触发 L3 retry。

这一步解决的是你之前指出的核心问题：不能等 B500 造完才发现 B 组整体太简单。现在应该在 construction loop 内即时 gate。

## Step 8: 行为验证

通过 v2 gate 还不够，必须做行为验证。

验证脚本：

- `scripts/verify_swebench_pro.py`

验证内容：

1. Clean target pass：
   干净现代代码上 target tests 通过。

2. Pass-to-fail：
   注入 bug 后 target tests 失败。

3. P2P clean/pass：
   干净现代代码上 P2P tests 通过。

4. P2P buggy no regression：
   注入 bug 后 P2P tests 不应该失败。否则说明 bug 太宽或破坏无关功能。

5. Golden repair pass：
   应用 injected diff 的反向 patch 后 target tests 通过。

6. P2P repaired pass：
   golden repair 后 P2P tests 通过。

只有全部满足，才是 strict verified row。

## Step 9: 记录 injection 和 verification 结果

每个 construction run 被分成多个 shard。典型目录结构：

- `experiments/rq2_500/<run_name>/shard_new_l1l2_001_20260613/`
- `verified_injection_results.jsonl`
- `verified_verification_results.jsonl`
- `verified_injection.log`
- `verified_verification.log`

`verified_injection_results.jsonl` 记录：

- case ID。
- repo/dataset。
- injection 是否成功。
- injection level：L1、L2、L3 或 preflight failed。
- failure reason。
- injected diff 路径或 diff 文本。
- L3 debug 信息。
- v2 gate 初步结果。

`verified_verification_results.jsonl` 记录：

- pass_to_fail 是否成立。
- golden_repair_pass 是否成立。
- p2p_buggy_failed 数量。
- p2p_repaired_pass 是否成立。
- actual failed tests。
- clean pass-to-pass tests。
- verification failure reason。

## Step 10: Final Assembly

construction run 结束后，不会直接把所有成功行加入 B500。需要 final assembler 再筛一次。

脚本：

- `scripts/assemble_prinjector_v2_b500.py`

assembler 输入：

- locked existing rows。
- old B rows。
- one or more construction run dirs。
- corresponding candidate files。

assembler 再检查：

- injection success。
- pass-to-fail。
- golden repair。
- P2P no regression。
- repaired P2P pass。
- v2 fidelity gate。
- dataset quota。
- repo cap。
- 去重。

如果不足 500，会输出：

- `partial_selected.jsonl`
- `accepted_new.jsonl`
- `assembly_failed_summary.json`

如果达到 500，会输出：

- `selected.jsonl`
- `injection_results.jsonl`
- dataset slices，例如 `swebench.jsonl`、`verified.jsonl`、`pro.jsonl`
- `assembly_summary.json`
- injected diff assets。
- golden patch assets。

## Step 11: B500 和 A500 配对

B500 完成后，每条 B 都通过 `source_instance_id` 对回官方 A instance。

之后生成 A/B paired eval inputs：

- A 组：官方原始 benchmark instance。
- B 组：PR-INJECTOR 构造的现代 injected instance。

agent eval 时，A 和 B 必须使用同样配置：

- 同一个模型。
- 同一个 agent harness。
- 同样 timeout。
- 同样工具权限。
- 同样判定标准。

## Step 12: RQ1-RQ3 需要记录的指标

RQ1 关注 construction：

- candidate pool size。
- preflight pass/fail。
- L1/L2/L3 成功率。
- strict verified 数量。
- v2 gate pass/fail。
- failure taxonomy。
- repo/dataset 分布。
- runtime。
- L3 token/cost。

RQ2 关注 agent solve：

- A solved。
- B solved target-only。
- B solved strict/P2P-safe。
- A1B1、A1B0、A0B1、A0B0 四象限。
- A solved B not solved 的具体原因。
- target pass 但 P2P fail 的过拟合情况。

RQ3 关注成本和系统问题：

- 每个 case construction 时间。
- 每个 case L3 调用次数和 token。
- repo/cache/venv 问题。
- Python/runtime 缺口。
- target remap 缺口。
- agent 生成 diff 的常见弱点。
- 哪类 repo 或哪类 bug 最难迁移。


## 当前重点改进方向

1. Fresh-first 补 Pro/Verified：
   不再让低 yield 的旧 retry 过度占用 B500 主线。

2. 复杂度即时 gate：
   每个 B 造出来就和 A 比，不合格就 retry 或丢弃。

3. repo 环境缺口记录：
   把 Python/runtime/cache/venv 问题单独记为基础设施瓶颈。

4. L3 feedback 改进：
   对 v2 gate fail、patch apply fail、P2P regression 分别给不同 feedback。

5. hard-negative 子集：
   特别记录 target pass 但 P2P 易坏的 case，用于后续 B-Hard 或 regression-sensitive eval。

6. 不把污染说死：
   A1B0 可以谨慎解释为 historical cue reliance 或 memorization-consistent evidence，而不是直接证明训练污染。

## 重要原则

一个 case 只有 target fail 不够。

一个 case 只有 L3 生成了 diff 不够。

一个 case 只有 v2 gate pass 也不够。

最终能进 B500 的 case 必须同时满足：

- 语义上对应官方 A bug。
- 现代代码上可运行。
- target pass-to-fail 成立。
- P2P 不被无关破坏。
- gold repair 能恢复。
- 复杂度和 A 大致匹配。
- 元数据完整，可追溯到 A。
