"""Core data models for PR-Injector pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class InjectionLevel(str, Enum):
    """The 4 injection levels from the core theory."""

    LEVEL_1_CLEAN_REVERT = "Level_1_Clean_Revert"
    LEVEL_2_AST_SURGERY = "Level_2_AST_Surgery"
    LEVEL_3_LLM_SEMANTIC = "Level_3_LLM_Semantic"
    LEVEL_4_DEPRECATED = "Level_4_Architecture_Deprecated"


class InjectionStrategy(str, Enum):
    """User-selectable strategy for the run command."""

    AUTO = "auto"
    GIT_ONLY = "git"
    AST_ONLY = "ast"
    LLM_ONLY = "llm"


class PRMetadata(BaseModel):
    """Raw PR data from GitHub API."""

    repo: str
    pr_number: int
    title: str
    body: str | None = None
    merge_commit_sha: str
    base_sha: str
    head_sha: str
    merged_at: datetime
    diff_url: str
    changed_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)
    additions: int = 0
    deletions: int = 0


class CandidatePR(BaseModel):
    """PR that passed Miner stage filtering."""

    metadata: PRMetadata
    time_decay_score: float = Field(ge=0.0, le=1.0, default=0.0)
    change_frequency_score: float = Field(ge=0.0, le=1.0, default=0.0)
    test_files_exist: bool = False
    estimated_level: InjectionLevel | None = None


class RevertResult(BaseModel):
    """Output of a successful Level 1 or Level 2 injection."""

    candidate: CandidatePR
    level: InjectionLevel
    injected_diff: str
    golden_patch: str
    worktree_path: str
    conflict_files: list[str] = Field(default_factory=list)


class LLMInjectionResult(BaseModel):
    """Output of a Level 3 semantic injection."""

    candidate: CandidatePR
    level: InjectionLevel = InjectionLevel.LEVEL_3_LLM_SEMANTIC
    injected_diff: str
    golden_patch: str
    worktree_path: str
    model_used: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    confidence_score: float = Field(ge=0.0, le=1.0, default=0.0)


class VerificationResult(BaseModel):
    """Output of the Verifier stage."""

    target_tests_failed: bool = False
    unrelated_tests_passed: bool = False
    blast_radius_ok: bool = False
    target_test_names: list[str] = Field(default_factory=list)
    failed_test_names: list[str] = Field(default_factory=list)
    total_tests_run: int = 0
    total_failures: int = 0
    test_duration_seconds: float = 0.0


class BenchmarkInstance(BaseModel):
    """Final JSONL output record, SWE-bench compatible."""

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    injection_level: InjectionLevel
    golden_patch: str
    test_patch: str
    hints_text: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=timezone.utc))
    verification: VerificationResult | None = None
