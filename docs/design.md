# PR-Injector 技术设计文档

**版本**: v0.1.0
**日期**: 2026-03-04
**语言**: Python 3.10+

---

## 目录

1. [项目概述](#1-项目概述)
2. [系统架构](#2-系统架构)
3. [核心理论：代码衰变与多级注入策略](#3-核心理论代码衰变与多级注入策略)
4. [技术栈选型](#4-技术栈选型)
5. [数据模型设计](#5-数据模型设计)
6. [流水线各阶段详细设计](#6-流水线各阶段详细设计)
7. [AST 引擎设计](#7-ast-引擎设计)
8. [LLM 集成设计](#8-llm-集成设计)
9. [并发模型](#9-并发模型)
10. [输出格式](#10-输出格式)
11. [配置管理](#11-配置管理)
12. [异常处理层级](#12-异常处理层级)
13. [项目结构](#13-项目结构)

---

## 1. 项目概述

### 1.1 定位

PR-Injector 是一个面向 **AI 编码智能体评估** 的新一代自动化缺陷注入框架。其核心使命是：将真实的历史业务逻辑缺陷，跨越时间维度重新注入到当前最新、最健康的现代代码库（`main` 分支）中，并自动提取绝对准确的 `Golden Patch`（标准修复方案）。

### 1.2 核心价值

| 维度 | 传统静态基准 (SWE-bench) | LLM 合成注入 (SWE-smith) | **PR-Injector** |
|------|--------------------------|--------------------------|-----------------|
| 代码运行环境 | 充满陈旧依赖的历史快照 | 当前最新代码库 | **当前最新代码库 (main)** |
| 缺陷真实性 | 极高（真实历史 Bug） | 低（模型幻觉与随机突变） | **极高（真实历史 Bug）** |
| 标准答案 | 具备唯一正确解 | 经常缺乏有效修复方案 | **具备唯一正确解 (PR Diff)** |
| 核心挑战 | 环境重构与沙盒维护 | 突变导致的代码完全崩溃 | **解决代码衰变与跨版本冲突** |

### 1.3 解决的问题

传统基准面临**"环境配置地狱"**——陈旧的依赖、失效的构建工具链使评测框架极其笨重。PR-Injector 提出了第三种范式：**跨越时空，在最新的 `main` 分支上执行智能化、多层级的历史 PR 反向回滚（Revert）**。

---

## 2. 系统架构

### 2.1 四阶段漏斗流水线架构图

```
                        ┌─────────────────────────────────────────────┐
                        │           GitHub Repository (远端)           │
                        └──────────────────┬──────────────────────────┘
                                           │ API 调用
                                           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                        STAGE 1: MINER（挖掘器）                          │
│                                                                          │
│  ┌──────────────┐   时间衰减过滤   ┌──────────────┐   测试文件检测       │
│  │  GitHub API  │ ─────────────► │  PR 元数据   │ ─────────────►      │
│  │  (httpx)     │                │  PRMetadata  │                      │
│  └──────────────┘                └──────────────┘                      │
│                                        │                                │
│                              yield CandidatePR                          │
└────────────────────────────────────────┼────────────────────────────────┘
                                         │ AsyncIterator[CandidatePR]
                                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      STAGE 2: REVERTER（回滚器）                         │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Level 1: Git Revert                                              │  │
│  │  git revert --no-commit <merge_commit_sha>                        │  │
│  │  ✅ 成功 → RevertResult(level=LEVEL_1_CLEAN_REVERT)               │  │
│  │  ❌ 冲突 → 重置 worktree，降级到 Level 2                           │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                  │                                       │
│                                  ▼ (Level 1 失败)                        │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Level 2: AST Surgery                                             │  │
│  │  tree-sitter 解析 → 节点匹配 → 代码块替换                          │  │
│  │  ✅ 成功 → RevertResult(level=LEVEL_2_AST_SURGERY)                │  │
│  │  ❌ 失败 → 抛出 RevertFailed，降级到 Level 3                       │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└────────────────────────────────────────┼────────────────────────────────┘
                                         │ RevertResult | RevertFailed
                                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      STAGE 3: RESOLVER（解析器）                         │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Level 4 检测: Architecture Deprecated                            │  │
│  │  测试文件是否存在？源文件是否存在？                                   │  │
│  │  ❌ 不存在 → 抛出 ArchitectureDeprecated（漏斗丢弃）                │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                  │ (通过 Level 4 检测)                   │
│                                  ▼                                       │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  Level 3: LLM Semantic Injection                                  │  │
│  │  litellm → claude-sonnet / gpt-4o                                 │  │
│  │  Issue描述 + 原始Diff + 当前代码 → 语义等价缺陷                     │  │
│  │  ✅ 成功 → LLMInjectionResult(level=LEVEL_3_LLM_SEMANTIC)         │  │
│  │  ❌ 失败 → 抛出 SemanticInjectionFailed（漏斗丢弃）                 │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└────────────────────────────────────────┼────────────────────────────────┘
                                         │ LLMInjectionResult
                                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                      STAGE 4: VERIFIER（验证器）                         │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  爆炸半径控制（Blast Radius Control）                               │  │
│  │                                                                   │  │
│  │  Step 1: 运行目标测试（来自原始 PR 的 test_files）                  │  │
│  │          ✅ 必须 FAIL → 证明缺陷注入生效                            │  │
│  │                                                                   │  │
│  │  Step 2: 运行全量测试套件                                          │  │
│  │          ✅ 无关测试失败率 ≤ blast_radius_threshold (10%)          │  │
│  │                                                                   │  │
│  │  ✅ 双重验证通过 → VerificationResult(blast_radius_ok=True)        │  │
│  │  ❌ 爆炸半径失控 → 样本作废                                         │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                          │
└────────────────────────────────────────┼────────────────────────────────┘
                                         │ VerificationResult
                                         ▼
                        ┌─────────────────────────────────┐
                        │    OUTPUT: BenchmarkInstance    │
                        │    JSONL (SWE-bench 兼容格式)    │
                        └─────────────────────────────────┘
```

### 2.2 模块依赖关系

```
                         ┌───────────────┐
                         │  cli/app.py   │  入口点 (Typer)
                         └──────┬────────┘
                                │
                         ┌──────▼────────┐
                         │  pipeline/    │
                         │  orchestrator │  调度层
                         └──┬──┬──┬──┬──┘
                            │  │  │  │
              ┌─────────────┘  │  │  └──────────────┐
              │                │  │                  │
       ┌──────▼──────┐  ┌──────▼──────┐  ┌──────────▼──────┐
       │   miner.py  │  │ reverter.py │  │   resolver.py   │
       └──────┬──────┘  └──────┬──────┘  └──────────┬──────┘
              │                │                      │
       ┌──────▼──────┐  ┌──────▼──────┐  ┌──────────▼──────┐
       │  core/      │  │ ast_engine/ │  │   llm/client.py │
       │  models.py  │  │  engine.py  │  │  (litellm)      │
       │  config.py  │  │  surgeon.py │  └─────────────────┘
       │  git_ops.py │  └─────────────┘
       │  exceptions │
       └─────────────┘
```

---

## 3. 核心理论：代码衰变与多级注入策略

### 3.1 代码衰变（Code Decay）问题定义

将一个数月甚至数年前的历史 PR 强行 Revert 到最新代码库，核心挑战是**上下文漂移（Context Drift）**：

- **物理级漂移**: 周围代码中空格、变量名、无关语句的变更导致 `git apply` 失败
- **语义级漂移**: 函数被重构、模块被重命名，但核心业务逻辑依然存在
- **架构级漂移**: 底层依赖被替换或功能被彻底下线

### 3.2 四级注入模型

#### Level 1: 完美回滚 (Clean Revert)

```
状态: 原修复代码及上下文未发生改变
工具: git revert --no-commit <merge_commit_sha>
前提: 无 merge conflict
产出: 极高质量的 Golden Patch（PR diff 的精确逆操作）
```

- 使用 `git revert --no-commit -m 1 <sha>` 处理 merge commit
- 若非 merge commit，直接用 `git revert --no-commit <sha>`
- 通过 `git diff --cached` 提取注入的 diff
- Golden Patch = `reverse_diff(injected_diff)`

#### Level 2: AST 外科手术 (AST-level Surgery)

```
状态: 核心逻辑存在，但周围代码已变更导致 Git 冲突
工具: tree-sitter AST 解析 + 精准代码块替换
算法:
  1. 解析当前文件 AST → 提取所有函数/方法节点
  2. 解析 commit 父版本文件 AST → 提取对应函数/方法节点
  3. 比较同名函数：current_text ≠ pre_fix_text → 执行替换
  4. 生成 unified diff 作为 injected_diff
```

#### Level 3: LLM 语义注入 (Semantic Injection)

> **默认启用**: Level 3 LLM 注入在 AUTO 策略下默认启用。当 Level 1 和 Level 2 均失败时，系统会自动尝试 LLM 语义注入。可通过 `--no-l3` 命令行参数禁用。

```
状态: 代码结构已发生较大重构，物理级匹配完全失效
工具: litellm → 任意高阶推理模型
输入:
  - 原始 Issue 描述（PR title + body）
  - 原始 PR Diff（fix 的逆向参考）
  - 当前相关模块的完整源代码
输出:
  - 在当前架构下"重新制造出相同的逻辑漏洞"的 unified diff
  - confidence_score: 注入可信度 (0.0~1.0)
```

#### Level 4: 架构废弃 (Architecture Deprecated)

```
状态: 底层依赖替换或功能彻底下线
检测:
  - 原 PR 对应的测试文件在当前代码库中是否存在？
  - 原 PR 修改的源文件是否还存在？
处理: 漏斗机制自动识别并抛弃，抛出 ArchitectureDeprecated 异常
```

### 3.3 降级策略 (Fallback Strategy)

```
                    CandidatePR
                        │
                        ▼
              ┌─── Level 1 尝试 ───┐
              │  git revert        │
              │  ✅ → 输出         │
              │  ❌ 冲突            │
              └────────┬───────────┘
                       │
                       ▼
              ┌─── Level 2 尝试 ───┐
              │  AST Surgery       │
              │  ✅ → 输出         │
              │  ❌ 节点匹配失败    │
              └────────┬───────────┘
                       │
                       ▼
              ┌─── Level 4 检测 ───┐
              │  文件存在性检查     │
              │  ❌ 全不存在 → 丢弃 │
              │  ✅ 部分存在        │
              └────────┬───────────┘
                       │
                       ▼
              ┌─── Level 3 尝试 ───┐
              │  LLM 语义注入      │
              │  ✅ → 输出         │
              │  ❌ → 丢弃         │
              └────────────────────┘
```

---

## 4. 技术栈选型

### 4.1 核心依赖及选型理由

| 库 | 版本 | 用途 | 选型理由 |
|----|------|------|----------|
| **typer[all]** | `>=0.9,<1.0` | CLI 框架 | 基于 Click，原生支持类型注解；`[all]` 包含 `rich` 集成，提供彩色输出和进度条 |
| **tree-sitter** | `>=0.24` | AST 解析 | 支持 40+ 语言、增量解析、C 扩展保障性能；v0.24+ API 稳定，支持 Python bindings |
| **gitpython** | `>=3.1,<4.0` | Git 操作 | 完整 Git 操作的 Python 封装；worktree 管理、commit diff 提取；比 subprocess 更安全 |
| **httpx** | `>=0.27,<1.0` | HTTP 客户端 | 原生 async/await 支持；与 GitHub API 交互；支持连接池和超时控制 |
| **litellm** | `>=1.40` | LLM 统一接口 | 统一调用 100+ 模型（Claude、GPT-4、Gemini）；无需修改代码即可切换模型 |
| **pydantic** | `>=2.0,<3.0` | 数据验证 | v2 使用 Rust 核心，性能大幅提升；提供严格类型验证和自动序列化 |
| **pydantic-settings** | `>=2.0,<3.0` | 配置管理 | 从环境变量和 `.env` 文件自动加载配置；与 Pydantic 模型无缝集成 |
| **structlog** | `>=24.0` | 结构化日志 | JSON/console 双模式输出；天然支持上下文绑定（`bind()`）；async 友好 |
| **unidiff** | `>=0.7,<1.0` | Diff 解析 | 专门解析 unified diff 格式；提取 hunks、行号、文件路径 |
| **orjson** | `>=3.9,<4.0` | JSON 序列化 | Rust 实现，比标准库快 10x；原生支持 `datetime`、`Enum` 序列化 |
| **anyio** | `>=4.0,<5.0` | 异步抽象层 | 兼容 asyncio/trio；subprocess 管理；为 async generator pipeline 提供基础 |
| **tenacity** | `>=8.0,<10.0` | 重试机制 | 声明式重试装饰器；指数退避；用于 GitHub API 和 LLM API 调用容错 |
| **rich** | `>=13.0,<14.0` | 终端 UI | 进度条、表格、彩色输出；由 typer[all] 间接引入 |

### 4.2 可选 AST 语言包

```toml
[project.optional-dependencies]
languages = [
    "tree-sitter-python>=0.23",      # Python AST
    "tree-sitter-javascript>=0.23",  # JavaScript/JSX AST
    "tree-sitter-typescript>=0.23",  # TypeScript/TSX AST
    "tree-sitter-java>=0.23",        # Java AST
    "tree-sitter-go>=0.23",          # Go AST
    "tree-sitter-rust>=0.23",        # Rust AST
]
```

各语言包以独立 Python 模块形式分发，按需安装，避免不必要的依赖膨胀。

---

## 5. 数据模型设计

所有核心模型使用 **Pydantic v2** 定义，位于 `src/pr_injector/core/models.py`。

### 5.1 枚举类型

```python
class InjectionLevel(str, Enum):
    """注入级别枚举，对应四级降级策略"""
    LEVEL_1_CLEAN_REVERT = "Level_1_Clean_Revert"
    LEVEL_2_AST_SURGERY  = "Level_2_AST_Surgery"
    LEVEL_3_LLM_SEMANTIC = "Level_3_LLM_Semantic"
    LEVEL_4_DEPRECATED   = "Level_4_Architecture_Deprecated"

class InjectionStrategy(str, Enum):
    """用户可选的注入策略（CLI 参数）"""
    AUTO     = "auto"     # 依次尝试 Git → AST → LLM（L3 默认启用，可用 --no-l3 禁用）
    GIT_ONLY = "git"      # 仅尝试 Level 1
    AST_ONLY = "ast"      # 仅尝试 Level 2
    LLM_ONLY = "llm"      # 仅尝试 Level 3
```

### 5.2 PRMetadata

```python
class PRMetadata(BaseModel):
    """GitHub API 返回的原始 PR 数据"""
    repo: str                          # "pallets/flask"
    pr_number: int                     # 5001
    title: str                         # PR 标题
    body: str | None                   # PR 描述（原始 Issue 描述）
    merge_commit_sha: str              # merge commit 的完整 SHA
    base_sha: str                      # 目标分支 HEAD SHA（PR 合并前）
    head_sha: str                      # PR 分支 HEAD SHA
    merged_at: datetime                # 合并时间（用于时间衰减计算）
    diff_url: str                      # GitHub diff URL
    changed_files: list[str]           # 所有变更文件路径
    test_files: list[str]              # 变更的测试文件路径（过滤后）
    additions: int                     # 新增行数
    deletions: int                     # 删除行数
```

### 5.3 CandidatePR

```python
class CandidatePR(BaseModel):
    """通过 Miner 阶段过滤的 PR 候选"""
    metadata: PRMetadata
    time_decay_score: float            # 时间衰减得分 [0.0, 1.0]
                                       # score = exp(-0.693 * days / half_life)
                                       # half_life = 180 天
    change_frequency_score: float      # 文件变更频率得分 [0.0, 1.0]
    test_files_exist: bool             # 测试文件在当前代码库中是否存在
    estimated_level: InjectionLevel | None  # 预估注入难度
```

### 5.4 RevertResult

```python
class RevertResult(BaseModel):
    """Level 1 或 Level 2 注入成功后的输出"""
    candidate: CandidatePR
    level: InjectionLevel              # LEVEL_1 或 LEVEL_2
    injected_diff: str                 # 注入的 unified diff（缺陷引入）
    golden_patch: str                  # 标准修复方案（injected_diff 的逆操作）
    worktree_path: str                 # 隔离 worktree 的本地路径
    conflict_files: list[str]          # AST 手术中无法处理的冲突文件
```

### 5.5 LLMInjectionResult

```python
class LLMInjectionResult(BaseModel):
    """Level 3 语义注入成功后的输出"""
    candidate: CandidatePR
    level: InjectionLevel = InjectionLevel.LEVEL_3_LLM_SEMANTIC
    injected_diff: str                 # LLM 生成的注入 diff
    golden_patch: str                  # 标准修复方案
    worktree_path: str                 # 隔离 worktree 路径
    model_used: str                    # 实际使用的 LLM 模型 ID
    prompt_tokens: int                 # 消耗的 prompt tokens
    completion_tokens: int             # 消耗的 completion tokens
    confidence_score: float            # 注入可信度 [0.0, 1.0]
```

### 5.6 VerificationResult

```python
class VerificationResult(BaseModel):
    """Verifier 阶段的验证结果"""
    target_tests_failed: bool          # 目标测试是否失败（必须为 True）
    unrelated_tests_passed: bool       # 无关测试是否通过（必须为 True）
    blast_radius_ok: bool              # 爆炸半径检查结果（两者的 AND）
    target_test_names: list[str]       # 目标测试文件/函数名
    failed_test_names: list[str]       # 实际失败的测试名
    total_tests_run: int               # 总测试数量
    total_failures: int                # 总失败数量
    test_duration_seconds: float       # 测试执行时长（秒）
```

### 5.7 BenchmarkInstance（最终输出模型）

```python
class BenchmarkInstance(BaseModel):
    """最终 JSONL 输出记录，兼容 SWE-bench 格式"""
    instance_id: str                   # "pallets-flask-pr-5001"
    repo: str                          # "pallets/flask"
    base_commit: str                   # 注入时的 HEAD SHA（最新 main）
    problem_statement: str             # PR title + body（提供给 Agent 的问题描述）
    injection_level: InjectionLevel    # 实际使用的注入级别
    golden_patch: str                  # 标准修复方案 unified diff
    test_patch: str                    # 测试代码 diff（从原始 PR diff 提取）
    hints_text: str                    # 提示文本（可选）
    created_at: datetime               # 生成时间
    verification: VerificationResult | None  # 验证结果（可为 None）
```

---

## 6. 流水线各阶段详细设计

### 6.1 Stage 1: Miner（挖掘器）

**位置**: `src/pr_injector/pipeline/miner.py`

#### 6.1.1 GitHub API 交互

```
GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc
GET /repos/{owner}/{repo}/pulls/{pr_number}/files

认证: Bearer token（PRI_GITHUB_TOKEN 环境变量）
API 版本: X-GitHub-Api-Version: 2022-11-28
并发控制: max_concurrent=10（防止速率限制）
```

#### 6.1.2 时间衰减算法

使用**指数衰减**（Exponential Decay）计算 PR 的时效性得分：

$$\text{score} = e^{-\frac{\ln 2 \cdot \Delta t}{t_{1/2}}}$$

其中：
- $\Delta t$ = 从 PR 合并到现在的天数
- $t_{1/2}$ = 半衰期，设定为 **180 天**

含义：180 天前的 PR 得分降至 0.5，360 天前的 PR 得分降至 0.25。

```python
@staticmethod
def _compute_time_decay(merge_time: datetime) -> float:
    now = datetime.now(timezone.utc)
    days_ago = (now - merge_time.replace(tzinfo=timezone.utc)).days
    half_life = 180  # days
    return math.exp(-0.693 * days_ago / half_life)
```

#### 6.1.3 过滤策略

| 过滤条件 | 参数 | 默认值 | 说明 |
|----------|------|--------|------|
| 时间范围 | `since` | 无限制 | 仅处理 `since` 之后 merge 的 PR |
| 必须有测试 | `require_tests` | `True` | PR 必须包含测试文件的变更 |
| Patch 大小 | `max_patch_size` | 5000 行 | 超大 PR 注入意义不大且代价高 |
| Revert PR 过滤 | 自动 | 始终 | 跳过标题以 "Revert" 开头的 PR |

#### 6.1.4 测试文件检测规则

```python
DEFAULT_TEST_PATTERNS = [
    "test_",      # pytest 风格: test_app.py
    "_test.",     # Go 风格: app_test.go
    ".test.",     # Jest 风格: app.test.js
    ".spec.",     # Jest/RSpec 风格: app.spec.ts
    "tests/",     # 目录: tests/test_app.py
    "test/",      # 目录: test/AppTest.java
    "__tests__/", # Jest 目录: __tests__/App.test.js
]
```

### 6.2 Stage 2: Reverter（回滚器）

**位置**: `src/pr_injector/pipeline/reverter.py`

#### 6.2.1 Level 1: Git Revert 流程

```
1. workspace.create_worktree(repo_path, suffix=f"pr-{pr_number}")
   → 创建隔离的 git worktree，不影响主仓库
2. repo.git.revert("--no-commit", "-m", "1", commit_sha)
   → 尝试 merge commit revert（-m 1 表示保留第一父提交视角）
   → 若非 merge commit，重试不带 -m 的版本
3. repo.git.diff("--cached")
   → 提取 staged 状态的 diff（这是实际注入的缺陷内容）
4. reverse_diff(injected_diff)
   → 通过交换 --- 和 +++ 行生成 Golden Patch
```

#### 6.2.2 Level 2: AST 外科手术流程

```
1. workspace.get_commit_diff(repo_path, merge_commit_sha)
   → 获取原始 fix 的 diff，分析修改了哪些源文件
2. 对每个源文件:
   a. 读取当前文件内容（HEAD 版本）
   b. 用 gitpython 读取 commit 父节点的文件内容（pre-fix 版本）
   c. tree-sitter 解析两个版本的 AST
   d. 提取两个版本中所有函数节点（function_definition, method_declaration 等）
   e. 对比同名函数：若内容不同，表示该函数在 fix 中被修改
   f. 用 ASTSurgeon 将当前版本中的函数替换为 pre-fix 版本
3. 生成 unified diff 并汇总
4. 若任何文件成功替换 → 输出 RevertResult(level=LEVEL_2)
```

### 6.3 Stage 3: Resolver（解析器）

**位置**: `src/pr_injector/pipeline/resolver.py`

#### 6.3.1 Level 4 检测逻辑

在进行任何 LLM 调用之前，先执行廉价的文件存在性检查：

```python
# 检查测试文件
test_files_exist = any(
    workspace.file_exists_at_head(repo_path, f)
    for f in candidate.metadata.test_files
)

# 检查源文件
source_files_exist = any(
    workspace.file_exists_at_head(repo_path, f)
    for f in candidate.metadata.changed_files
)

if not test_files_exist or not source_files_exist:
    raise ArchitectureDeprecated(...)  # 漏斗丢弃
```

#### 6.3.2 Level 3 LLM 语义注入流程

```
1. 收集当前代码库中被原始 PR 修改的源文件内容
2. 构建 issue_description = f"PR #{number}: {title}\n\n{body}"
3. 调用 LLMClient.generate_semantic_injection():
   - 输入: issue_description + original_diff + current_files
   - 输出: unified_diff + confidence_score
4. workspace.apply_patch(worktree_path, diff)
   - 先 git apply --check（验证）
   - 再 git apply（实际应用）
5. reverse_diff(diff) → Golden Patch
```

### 6.4 Stage 4: Verifier（验证器）

**位置**: `src/pr_injector/pipeline/verifier.py`

#### 6.4.1 爆炸半径控制机制

**核心不变式（Invariant）**：

> 注入缺陷后，目标测试（来自原始 PR 的测试文件）**必须失败**，而无关测试的失败率**必须低于阈值**。

```
blast_radius_ok = target_tests_failed AND (unrelated_failure_rate ≤ threshold)

其中:
  unrelated_failure_rate = (total_failures - target_failure_count) / total_tests
  threshold = PRI_BLAST_RADIUS_THRESHOLD（默认 10%）
```

#### 6.4.2 测试运行器自动检测

| 检测文件 | 使用的测试命令 |
|----------|---------------|
| `pytest.ini` / `pyproject.toml` / `setup.py` | `python -m pytest -x --tb=short -q` |
| `package.json` | `npm test --` |
| `go.mod` | `go test ./...` |
| `Cargo.toml` | `cargo test` |
| `Gemfile` | `bundle exec rspec` |
| `pom.xml` | `mvn test -q` |

#### 6.4.3 测试输出解析

支持多种测试框架的输出格式解析：

```python
# pytest: "X passed, Y failed"
pytest_match = re.search(r"(\d+) passed(?:.*?(\d+) failed)?", output)

# 通用: 统计 FAIL/PASS/FAILED/PASSED 关键字行数
fail_lines = re.findall(r"(?:FAIL|ERROR|FAILED)\s+(\S+)", output)
pass_lines = re.findall(r"(?:PASS|OK|PASSED)\s+(\S+)", output)
```

---

## 7. AST 引擎设计

### 7.1 多语言支持架构

```
src/pr_injector/ast_engine/
├── engine.py         # ASTEngine: 统一解析入口
├── languages.py      # 语言-扩展名-包名 三级映射表
├── node_matcher.py   # 函数/类节点提取
└── surgeon.py        # 代码块替换（外科手术）
```

### 7.2 三级映射表设计

```python
# 文件扩展名 → 语言名
EXTENSION_TO_LANGUAGE = {".py": "python", ".ts": "typescript", ...}

# 语言名 → tree-sitter 包名
LANGUAGE_TO_PACKAGE = {"python": "tree_sitter_python", ...}

# 语言名 → 函数节点类型列表
FUNCTION_NODE_TYPES = {
    "python":     ["function_definition", "decorated_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "java":       ["method_declaration", "constructor_declaration"],
    "go":         ["function_declaration", "method_declaration"],
    "rust":       ["function_item"],
}

# 语言名 → 类节点类型列表
CLASS_NODE_TYPES = {
    "python": ["class_definition"],
    "java":   ["class_declaration", "interface_declaration"],
    ...
}
```

### 7.3 ASTEngine：懒加载解析器

```python
class ASTEngine:
    """按需初始化各语言解析器，缓存已初始化的 Parser 实例"""

    def _get_parser(self, language: str) -> tree_sitter.Parser | None:
        # 1. 检查缓存
        # 2. 动态 import 语言包（tree_sitter_python 等）
        # 3. 创建 Language 和 Parser 对象
        # 4. 缓存并返回

    def parse_source(self, source_code: str, language: str) -> tree_sitter.Tree | None:
        # 将 source_code 编码为 bytes 后解析
        # 返回 tree-sitter Tree 对象
```

### 7.4 节点匹配算法

```python
def find_functions(tree: tree_sitter.Tree, language: str) -> list[FunctionNode]:
    """遍历 AST，提取所有顶层和类方法函数节点"""

    node_types = FUNCTION_NODE_TYPES.get(language, [])
    results = []

    def traverse(node):
        if node.type in node_types:
            name = _extract_name(node, language)
            if name:
                results.append(FunctionNode(
                    name=name,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                    start_point=node.start_point,  # (行, 列)
                    end_point=node.end_point,
                ))
        for child in node.children:
            traverse(child)

    traverse(tree.root_node)
    return results
```

### 7.5 ASTSurgeon：代码块替换

```python
class ASTSurgeon:
    """精准替换 AST 中的代码块"""

    def compute_replacement(
        self,
        current_content: str,
        new_function_text: str,
        function_name: str,
        file_path: str,
    ) -> str:
        """
        1. 在 current_content 中定位 function_name 的字节偏移
        2. 用 new_function_text 替换对应的代码片段
        3. 返回修改后的完整文件内容
        """

    def apply_surgery(
        self,
        worktree_path: str,
        file_path: str,
        new_content: str,
    ) -> str | None:
        """
        1. 写入修改后的文件内容
        2. 调用 git diff 生成 unified diff
        3. 返回 diff 字符串
        """
```

---

## 8. LLM 集成设计

### 8.1 litellm 多模型支持

通过 `litellm` 实现模型无关的统一调用接口：

```python
response = await litellm.acompletion(
    model="claude-sonnet-4-20250514",  # 或 "gpt-4o", "gemini/gemini-1.5-pro"
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
    temperature=0.2,  # 低温度保证输出确定性
    max_tokens=4096,
)
```

切换模型只需修改 `PRI_LLM_MODEL` 环境变量，无需更改代码。

### 8.2 Prompt 模板设计

#### System Prompt（角色定义）

```
You are an expert software engineer tasked with recreating a historical bug
in a modern codebase. Your goal is to precisely reintroduce the same logical
defect that was originally fixed, adapted to the current code structure.

Rules:
1. Only modify the specific functions/methods that correspond to the original bug fix.
2. The bug must be a LOGICAL defect, not a syntax error.
3. Output ONLY a valid unified diff (no explanations, no markdown).
4. The diff must apply cleanly to the current source files.
5. Do NOT introduce any new imports or dependencies.
6. Preserve all existing functionality EXCEPT for the specific bug being reintroduced.
```

#### User Prompt 结构

```markdown
## Original Bug Context

### Issue Description
{issue_description}        ← PR title + body（最多 3000 字符）

### Original Fix (PR Diff)
```diff
{original_diff}            ← fix 的 diff（仅源文件，去除测试文件）
```

## Current Codebase (Latest Version)
### {file_path}
```
{file_content}             ← 当前代码（超过 10000 字符则截断）
```

## Task
Create a unified diff that reintroduces the SAME logical bug into the CURRENT code.
Output ONLY the unified diff, starting with "diff --git".
```

### 8.3 输出验证

```python
# 1. 从 LLM 响应中提取 diff（支持 markdown 代码块和裸 diff）
def extract_diff_from_response(response_text: str) -> str | None:
    # 尝试提取 ```diff ... ``` 代码块
    # 若无代码块，查找以 "diff --git" 开头的行

# 2. 验证 diff 语法
def validate_diff_syntax(diff: str) -> tuple[bool, list[str]]:
    # 检查 diff --git 头
    # 检查 --- / +++ 行
    # 检查 @@ 行号信息
    # 检查 +/- 内容行

# 3. 估算置信度
def estimate_confidence(generated_diff: str, original_diff: str) -> float:
    # 比较修改行数、涉及文件数、Jaccard 相似度
    # 返回 0.0 ~ 1.0 的置信度分数
```

### 8.4 重试机制

```python
@retry(
    stop=stop_after_attempt(3),           # 最多重试 3 次
    wait=wait_exponential(                 # 指数退避：2s, 4s, 8s
        multiplier=1, min=2, max=30
    ),
    reraise=True,                          # 最终失败时重新抛出
)
async def _call_llm(self, system_prompt, user_prompt): ...
```

---

## 9. 并发模型

### 9.1 async generator 流水线

整个流水线基于 `asyncio` 和 **async generator** 模式构建：

```python
# Miner 输出 AsyncIterator[CandidatePR]
async for candidate in miner.mine(repo, ...):
    ...

# Orchestrator 的 run_batch 输出 AsyncIterator[BenchmarkInstance]
async for instance in orchestrator.run_batch(repo, ...):
    writer.write(instance)
```

### 9.2 生产者-消费者模型（批量模式）

```python
# 生产者: mine PR 候选，放入队列（带背压限制）
queue = asyncio.Queue(maxsize=max_workers * 2)  # 背压: 最多积压 2×workers

async def producer():
    async for candidate in miner.mine(...):
        await queue.put(candidate)    # 当队列满时自动阻塞（背压）
    for _ in range(max_workers):
        await queue.put(None)         # 发送终止信号

# 消费者（N 个并行 worker）
async def worker():
    while True:
        candidate = await queue.get()
        if candidate is None:
            break
        instance = await _process_candidate(candidate, ...)
        if instance:
            await results.put(instance)
    await results.put(None)

# 启动
producer_task = asyncio.create_task(producer())
worker_tasks = [asyncio.create_task(worker()) for _ in range(max_workers)]
```

### 9.3 Git Worktree 隔离

每个注入尝试在独立的 **git worktree** 中执行，实现并行安全：

```
.pri-workspace/
├── repos/
│   └── pallets__flask/          ← 共享主仓库克隆（只读，用于 fetch）
└── worktrees/
    ├── wt-pr-5001-a3f9b2c1/     ← Worker 1 的注入工作区
    ├── wt-pr-5002-b4e8c3d2/     ← Worker 2 的注入工作区
    └── wt-llm-pr-5003-c5d7e4f3/ ← Level 3 Worker 的工作区
```

- `create_worktree()`: 执行 `git worktree add -b injection-{id} <path>`
- `remove_worktree()`: 注入完成后执行 `git worktree remove --force`
- 每个 worktree 有独立的工作目录，互不干扰

### 9.4 背压机制（Back Pressure）

```python
# Queue(maxsize=max_workers * 2) 实现自然背压
# 当所有 worker 都在处理时，producer 会在 await queue.put() 处阻塞
# 防止 Miner 无限制地将 GitHub API 数据堆积到内存中
```

### 9.5 同步 I/O 的线程化

Git 操作（`gitpython`）是同步的，使用 `asyncio.to_thread` 包装以避免阻塞事件循环：

```python
await asyncio.to_thread(repo.git.revert, "--no-commit", commit_sha)
await asyncio.to_thread(repo.remotes.origin.fetch)
```

---

## 10. 输出格式

### 10.1 SWE-bench 兼容的 JSONL 规范

每行为一个独立的 JSON 对象（JSON Lines 格式），对应一个 `BenchmarkInstance`：

```json
{
  "instance_id": "pallets-flask-pr-5001",
  "repo": "pallets/flask",
  "base_commit": "a3f9b2c1d4e5f6789abcdef0123456789abcdef0",
  "problem_statement": "Fix routing edge case with trailing slash\n\nWhen using url_for() with strict_slashes=False...",
  "injection_level": "Level_2_AST_Surgery",
  "golden_patch": "diff --git a/src/flask/routing.py b/src/flask/routing.py\nindex a1b2c3..d4e5f6 100644\n--- a/src/flask/routing.py\n+++ b/src/flask/routing.py\n@@ -45,7 +45,7 @@ class Map:\n ...",
  "test_patch": "diff --git a/tests/test_routing.py b/tests/test_routing.py\n...",
  "hints_text": "",
  "created_at": "2026-03-04T12:34:56.789000"
}
```

### 10.2 字段说明

| 字段 | 类型 | 说明 | SWE-bench 对应 |
|------|------|------|----------------|
| `instance_id` | `str` | 唯一标识符，格式 `{repo-slug}-pr-{number}` | `instance_id` |
| `repo` | `str` | 仓库全名 `owner/name` | `repo` |
| `base_commit` | `str` | 注入时的 HEAD commit SHA（最新 main） | `base_commit` |
| `problem_statement` | `str` | 提供给 AI Agent 的问题描述 | `problem_statement` |
| `injection_level` | `str` | 实际使用的注入级别枚举值 | 扩展字段 |
| `golden_patch` | `str` | 标准修复方案 unified diff | `patch` |
| `test_patch` | `str` | 验证所需的测试代码 diff | `test_patch` |
| `hints_text` | `str` | 可选提示（默认为空） | `hints_text` |
| `created_at` | `str` | ISO 8601 时间戳 | 扩展字段 |

### 10.3 输出写入器

```python
class JSONLWriter:
    """线程安全的 JSONL 输出写入器"""

    def __init__(self, output_dir: str, filename: str = "benchmark.jsonl"):
        self._path = Path(output_dir) / filename
        self._lock = asyncio.Lock()  # 防止并发写入乱序

    def write(self, instance: BenchmarkInstance) -> None:
        """使用 orjson 序列化，写入一行 JSON"""
        record = BenchmarkOutput.from_benchmark_instance(...)
        line = orjson.dumps(record.model_dump()) + b"\n"
        with open(self._path, "ab") as f:  # 追加模式
            f.write(line)
```

---

## 11. 配置管理

### 11.1 环境变量规范

所有配置项使用 `PRI_` 前缀，通过 `pydantic-settings` 自动加载：

```bash
# GitHub
PRI_GITHUB_TOKEN=ghp_xxxxxxxxxxxx     # GitHub Personal Access Token（必填）
PRI_GITHUB_API_BASE=https://api.github.com  # API 基地址（支持 GHE）
PRI_GITHUB_MAX_CONCURRENT=10          # 最大并发 API 请求数

# LLM
PRI_LLM_MODEL=claude-sonnet-4-20250514  # litellm 模型标识符
PRI_LLM_API_KEY=sk-ant-xxxxxxxxxxxx    # API 密钥（或通过 ANTHROPIC_API_KEY）
PRI_LLM_TEMPERATURE=0.2               # 生成温度（低温保证确定性）
PRI_LLM_MAX_TOKENS=4096               # 最大输出 tokens
PRI_LLM_MAX_RETRIES=3                 # API 调用最大重试次数
# 注意: Level 3 LLM 注入默认启用，使用 --no-l3 命令行参数可禁用

# 流水线
PRI_WORKSPACE_DIR=.pri-workspace      # 工作区根目录（存放克隆仓库和 worktrees）
PRI_MAX_WORKERS=4                     # 并行 worker 数量
PRI_DEFAULT_STRATEGY=auto             # 默认注入策略

# Verifier
PRI_TEST_TIMEOUT_SECONDS=300          # 测试运行超时（秒）
PRI_BLAST_RADIUS_THRESHOLD=0.1        # 允许的最大无关测试失败率（10%）

# AST
PRI_TREE_SITTER_GRAMMAR_DIR=          # 自定义 grammar 目录（可选）

# 输出
PRI_OUTPUT_DIR=./benchmark_dataset    # JSONL 输出目录

# 日志
PRI_LOG_LEVEL=INFO                    # 日志级别: DEBUG/INFO/WARNING/ERROR
PRI_LOG_FORMAT=console                # 日志格式: console（彩色）/ json
```

### 11.2 .env 文件支持

项目提供 `.env.example` 模板：

```bash
# .env.example
PRI_GITHUB_TOKEN=your_github_pat_here
PRI_LLM_MODEL=claude-sonnet-4-20250514
PRI_LLM_API_KEY=your_api_key_here
PRI_MAX_WORKERS=4
PRI_BLAST_RADIUS_THRESHOLD=0.1
```

pydantic-settings 自动读取 `.env` 文件，优先级：环境变量 > `.env` 文件 > 默认值。

### 11.3 配置类实现

```python
class PRInjectorSettings(BaseSettings):
    model_config = {
        "env_prefix": "PRI_",           # 所有环境变量使用 PRI_ 前缀
        "env_file": ".env",             # 自动读取 .env 文件
        "env_file_encoding": "utf-8",
    }

    github_token: str = ""
    llm_model: str = "claude-sonnet-4-20250514"
    # ... 其他字段

def get_settings() -> PRInjectorSettings:
    return PRInjectorSettings()      # 每次调用重新加载（适合测试覆写）
```

---

## 12. 异常处理层级

### 12.1 异常类型继承树

```
PRInjectorError                         ← 所有异常的基类
│
├── MinerError                          ← Stage 1: PR 发现与过滤失败
│   └── 触发: GitHub API 错误、PR 未合并
│
├── GitOperationError                   ← Git 操作失败
│   └── 触发: clone/fetch/worktree 操作失败
│
├── RevertFailed                        ← Level 1/2 注入失败
│   └── 触发: git revert 冲突 + AST 手术失败
│
├── ASTMatchFailed                      ← Level 2: AST 节点定位失败
│   └── 触发: 函数在当前代码库中已被重命名或删除
│
├── ASTSurgeryFailed                    ← Level 2: 代码替换产生无效代码
│   └── 触发: 字节偏移计算错误
│
├── SemanticInjectionFailed             ← Level 3: LLM 注入失败
│   └── 触发: LLM 未生成有效 diff / diff 无法应用
│
├── ArchitectureDeprecated              ← Level 4: 架构废弃
│   └── 触发: 原 PR 的测试文件和源文件都已从代码库中删除
│
├── BlastRadiusExceeded                 ← Verifier: 爆炸半径失控
│   └── 触发: 注入导致过多无关测试失败
│
├── VerificationFailed                  ← Verifier: 目标测试未失败
│   └── 触发: 注入后目标测试仍然通过（注入无效）
│
└── TestTimeoutError                    ← Verifier: 测试超时
    └── 触发: 测试运行超过 PRI_TEST_TIMEOUT_SECONDS
```

### 12.2 处理策略

| 异常类型 | 处理策略 | 影响 |
|----------|----------|------|
| `MinerError` | 记录警告，跳过该 PR | 继续处理下一个候选 |
| `GitOperationError` | 记录错误，尝试重试 | 可能影响整个批次 |
| `RevertFailed` | 降级到 Level 3 | 触发 LLM 调用 |
| `ASTMatchFailed` | 计入 conflict_files，继续其他文件 | 部分注入 |
| `ASTSurgeryFailed` | 计入 conflict_files | 部分注入 |
| `SemanticInjectionFailed` | 记录信息，丢弃该候选 | 样本废弃 |
| `ArchitectureDeprecated` | 记录信息，丢弃 | 样本废弃，统计 level_4_deprecated |
| `BlastRadiusExceeded` | 记录信息，丢弃 | 样本废弃 |
| `VerificationFailed` | 记录信息，样本仍保留（标记未验证） | 降低质量标记 |
| `TestTimeoutError` | 记录警告，样本标记为未验证 | 验证跳过 |

---

## 13. 项目结构

```
pr-injector/
│
├── src/
│   └── pr_injector/
│       │
│       ├── __init__.py                    # 包版本定义
│       │
│       ├── cli/                           # 命令行接口层
│       │   ├── __init__.py
│       │   ├── app.py                     # Typer app 入口，注册子命令
│       │   ├── run_cmd.py                 # `pr-injector run` 命令实现
│       │   └── mine_cmd.py                # `pr-injector mine` 命令实现
│       │
│       ├── pipeline/                      # 流水线阶段
│       │   ├── __init__.py
│       │   ├── orchestrator.py            # PipelineOrchestrator: 调度四个阶段
│       │   ├── miner.py                   # PRMiner: Stage 1 挖掘器
│       │   ├── reverter.py                # PRReverter: Stage 2 Level 1/2 注入
│       │   ├── resolver.py                # PRResolver: Stage 3 Level 3/4 处理
│       │   └── verifier.py                # TestVerifier: Stage 4 爆炸半径控制
│       │
│       ├── core/                          # 核心基础模块
│       │   ├── __init__.py
│       │   ├── models.py                  # Pydantic 数据模型（所有 DTO）
│       │   ├── config.py                  # PRInjectorSettings（pydantic-settings）
│       │   ├── exceptions.py              # 异常类型层级
│       │   ├── logging.py                 # structlog 配置与 get_logger()
│       │   ├── git_ops.py                 # GitWorkspace（worktree 管理）
│       │   ├── patch_ops.py               # worktree 重置辅助函数
│       │   └── diff_parser.py             # unidiff 包装：解析/反转/提取 diff
│       │
│       ├── ast_engine/                    # AST 解析与外科手术
│       │   ├── __init__.py
│       │   ├── engine.py                  # ASTEngine：tree-sitter 多语言解析器
│       │   ├── languages.py               # 语言注册表（扩展名/包名/节点类型）
│       │   ├── node_matcher.py            # find_functions()：AST 节点提取
│       │   └── surgeon.py                 # ASTSurgeon：代码块精准替换
│       │
│       ├── llm/                           # LLM 集成层
│       │   ├── __init__.py
│       │   ├── client.py                  # LLMClient：litellm 封装 + 重试
│       │   ├── prompts.py                 # Prompt 模板（系统 + 用户 prompt）
│       │   └── validator.py               # diff 提取、语法验证、置信度估算
│       │
│       └── output/                        # 输出层
│           ├── __init__.py
│           ├── schema.py                  # BenchmarkOutput（SWE-bench 兼容）
│           └── writer.py                  # JSONLWriter：orjson 序列化写入
│
├── tests/
│   ├── __init__.py
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_models.py                 # Pydantic 模型验证测试
│   │   ├── test_miner.py                  # PRMiner 单元测试
│   │   ├── test_reverter.py               # PRReverter 单元测试
│   │   ├── test_ast_engine.py             # ASTEngine 单元测试
│   │   └── test_diff_parser.py            # diff 解析工具测试
│   └── integration/
│       ├── __init__.py
│       └── test_pipeline.py               # 端到端流水线集成测试
│
├── docs/
│   └── design.md                          # 本文档
│
├── .omc/
│   ├── plans/
│   │   └── pr-injector-implementation.md  # OMC 实施计划
│   └── autopilot/
│       └── spec.md                        # 项目规格说明
│
├── main.py                                # 直接运行入口（uv run main.py）
├── pyproject.toml                         # 项目配置（hatch 构建系统，uv 管理依赖）
├── .env.example                           # 环境变量示例
├── .gitignore
└── README.md                              # 项目文档（中文）
```

---

## 附录：关键数据流示意

```
用户输入: uv run main.py run --repo pallets/flask --pr 5001
                │
                ▼
        CLI (typer) 解析参数
                │
                ▼
        构建 PipelineOrchestrator
        （注入所有依赖：Miner, Reverter, Resolver, Verifier, Writer）
                │
                ▼
        orchestrator.run_single(repo="pallets/flask", pr_number=5001)
                │
                ├── workspace.clone_or_update("pallets/flask")
                │       → .pri-workspace/repos/pallets__flask/
                │
                ├── miner.fetch_single_pr("pallets/flask", 5001)
                │       → CandidatePR(metadata=PRMetadata(...), time_decay_score=0.73)
                │
                ├── reverter.revert(candidate, repo_path)
                │   ├── create_worktree() → wt-pr-5001-a3b4c5d6/
                │   ├── try git revert --no-commit <sha>
                │   │   ✅ → RevertResult(level=LEVEL_1, injected_diff="...", golden_patch="...")
                │   └── (或降级 Level 2/3)
                │
                ├── verifier.verify(worktree_path, test_files)
                │   ├── 检测测试运行器: pytest.ini → "python -m pytest -x --tb=short -q"
                │   ├── 运行目标测试 → 失败 ✅
                │   └── 运行全量测试 → 99% 通过 ✅ blast_radius_ok=True
                │
                └── BenchmarkInstance(instance_id="pallets-flask-pr-5001", ...)
                        │
                        ▼
                JSONLWriter.write() → ./benchmark_dataset/benchmark.jsonl
```

---

*文档最后更新: 2026-03-04*
