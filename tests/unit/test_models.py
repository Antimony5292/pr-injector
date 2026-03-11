"""Unit tests for Pydantic data models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pr_injector.core.models import (
    CandidatePR,
    InjectionLevel,
    InjectionStrategy,
    PRMetadata,
    VerificationResult,
)


class TestInjectionLevel:
    def test_values(self):
        assert InjectionLevel.LEVEL_1_CLEAN_REVERT == "Level_1_Clean_Revert"
        assert InjectionLevel.LEVEL_2_AST_SURGERY == "Level_2_AST_Surgery"
        assert InjectionLevel.LEVEL_3_LLM_SEMANTIC == "Level_3_LLM_Semantic"
        assert InjectionLevel.LEVEL_4_DEPRECATED == "Level_4_Architecture_Deprecated"

    def test_from_string(self):
        assert InjectionLevel("Level_1_Clean_Revert") == InjectionLevel.LEVEL_1_CLEAN_REVERT


class TestInjectionStrategy:
    def test_values(self):
        assert InjectionStrategy.AUTO == "auto"
        assert InjectionStrategy.GIT_ONLY == "git"
        assert InjectionStrategy.AST_ONLY == "ast"
        assert InjectionStrategy.LLM_ONLY == "llm"


class TestPRMetadata:
    def test_create(self, sample_pr_metadata):
        assert sample_pr_metadata.repo == "pallets/flask"
        assert sample_pr_metadata.pr_number == 5001
        assert len(sample_pr_metadata.changed_files) == 2
        assert len(sample_pr_metadata.test_files) == 1

    def test_defaults(self):
        meta = PRMetadata(
            repo="test/repo",
            pr_number=1,
            title="Test",
            merge_commit_sha="abc",
            base_sha="def",
            head_sha="ghi",
            merged_at=datetime.now(timezone.utc),
            diff_url="https://example.com",
        )
        assert meta.body is None
        assert meta.changed_files == []
        assert meta.additions == 0


class TestCandidatePR:
    def test_create(self, sample_candidate):
        assert sample_candidate.time_decay_score == 0.75
        assert sample_candidate.test_files_exist is True
        assert sample_candidate.estimated_level is None

    def test_score_bounds(self, sample_pr_metadata):
        with pytest.raises(ValueError):
            CandidatePR(
                metadata=sample_pr_metadata,
                time_decay_score=1.5,  # Out of bounds
            )


class TestVerificationResult:
    def test_defaults(self):
        result = VerificationResult()
        assert result.target_tests_failed is False
        assert result.blast_radius_ok is False
        assert result.total_tests_run == 0

    def test_blast_radius(self, sample_verification):
        assert sample_verification.blast_radius_ok is True
        assert sample_verification.total_failures == 1


class TestBenchmarkInstance:
    def test_create(self, sample_benchmark):
        assert sample_benchmark.instance_id == "flask-pr-5001"
        assert sample_benchmark.injection_level == InjectionLevel.LEVEL_1_CLEAN_REVERT

    def test_serialization(self, sample_benchmark):
        data = sample_benchmark.model_dump()
        assert data["instance_id"] == "flask-pr-5001"
        assert data["injection_level"] == "Level_1_Clean_Revert"
