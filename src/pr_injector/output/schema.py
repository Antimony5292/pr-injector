"""Output schema for SWE-bench compatible benchmark instances."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from pr_injector.core.models import InjectionLevel, VerificationResult


class BenchmarkOutput(BaseModel):
    """SWE-bench compatible JSONL output record.

    This is the serialized form of BenchmarkInstance, optimized for
    compatibility with the SWE-bench evaluator format.
    """

    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    injection_level: str
    golden_patch: str
    test_patch: str
    hints_text: str = ""
    created_at: str = ""

    @classmethod
    def from_benchmark_instance(
        cls,
        instance_id: str,
        repo: str,
        base_commit: str,
        problem_statement: str,
        injection_level: InjectionLevel,
        golden_patch: str,
        test_patch: str,
        hints_text: str = "",
        created_at: datetime | None = None,
        verification: VerificationResult | None = None,
    ) -> BenchmarkOutput:
        """Create output from a BenchmarkInstance's fields."""
        return cls(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            problem_statement=problem_statement,
            injection_level=injection_level.value,
            golden_patch=golden_patch,
            test_patch=test_patch,
            hints_text=hints_text,
            created_at=(created_at or datetime.now()).isoformat(),
        )
