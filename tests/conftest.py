"""Shared test fixtures for PR-Injector."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from pr_injector.core.models import (
    BenchmarkInstance,
    CandidatePR,
    InjectionLevel,
    PRMetadata,
    VerificationResult,
)


@pytest.fixture
def sample_pr_metadata() -> PRMetadata:
    """A sample PR metadata for testing."""
    return PRMetadata(
        repo="pallets/flask",
        pr_number=5001,
        title="Fix request context handling in nested blueprints",
        body="This PR fixes an issue where request context was not properly propagated...",
        merge_commit_sha="abc123def456",
        base_sha="000111222333",
        head_sha="444555666777",
        merged_at=datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
        diff_url="https://github.com/pallets/flask/pull/5001.diff",
        changed_files=["src/flask/blueprints.py", "tests/test_blueprints.py"],
        test_files=["tests/test_blueprints.py"],
        additions=25,
        deletions=10,
    )


@pytest.fixture
def sample_candidate(sample_pr_metadata: PRMetadata) -> CandidatePR:
    """A sample candidate PR for testing."""
    return CandidatePR(
        metadata=sample_pr_metadata,
        time_decay_score=0.75,
        change_frequency_score=0.5,
        test_files_exist=True,
    )


@pytest.fixture
def sample_diff() -> str:
    """A sample unified diff for testing."""
    return (
        "diff --git a/src/flask/blueprints.py b/src/flask/blueprints.py\n"
        "index abc1234..def5678 100644\n"
        "--- a/src/flask/blueprints.py\n"
        "+++ b/src/flask/blueprints.py\n"
        "@@ -100,3 +100,4 @@ class Blueprint:\n"
        "     def register(self, app, options):\n"
        "-        ctx = app.request_context\n"
        "+        ctx = app.ensure_request_context()\n"
        "+        ctx.push()\n"
        "         self._register_views(ctx)\n"
    )


@pytest.fixture
def sample_verification() -> VerificationResult:
    """A sample verification result."""
    return VerificationResult(
        target_tests_failed=True,
        unrelated_tests_passed=True,
        blast_radius_ok=True,
        target_test_names=["tests/test_blueprints.py"],
        failed_test_names=["tests/test_blueprints.py::test_nested_blueprint"],
        total_tests_run=150,
        total_failures=1,
        test_duration_seconds=12.5,
    )


@pytest.fixture
def sample_benchmark(
    sample_candidate: CandidatePR,
    sample_diff: str,
    sample_verification: VerificationResult,
) -> BenchmarkInstance:
    """A sample benchmark instance."""
    return BenchmarkInstance(
        instance_id="flask-pr-5001",
        repo="pallets/flask",
        base_commit="latest_main_commit_hash",
        problem_statement="Fix request context handling in nested blueprints",
        injection_level=InjectionLevel.LEVEL_1_CLEAN_REVERT,
        golden_patch=sample_diff,
        test_patch="",
        verification=sample_verification,
    )
