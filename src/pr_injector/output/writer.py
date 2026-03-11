"""JSONL output writer for benchmark instances."""

from __future__ import annotations

from pathlib import Path

import orjson

from pr_injector.core.logging import get_logger
from pr_injector.core.models import BenchmarkInstance
from pr_injector.output.schema import BenchmarkOutput

logger = get_logger(__name__)


class JSONLWriter:
    """Append-only JSONL writer for benchmark output.

    Writes one BenchmarkInstance per line in SWE-bench compatible format.
    Uses orjson for fast serialization.
    """

    def __init__(self, output_dir: str, filename: str = "benchmark.jsonl") -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.filepath = self.output_dir / filename
        self._count = 0

    def write(self, instance: BenchmarkInstance) -> None:
        """Append a single benchmark instance to the JSONL file."""
        output = BenchmarkOutput.from_benchmark_instance(
            instance_id=instance.instance_id,
            repo=instance.repo,
            base_commit=instance.base_commit,
            problem_statement=instance.problem_statement,
            injection_level=instance.injection_level,
            golden_patch=instance.golden_patch,
            test_patch=instance.test_patch,
            hints_text=instance.hints_text,
            created_at=instance.created_at,
            verification=instance.verification,
        )
        line = orjson.dumps(output.model_dump(), option=orjson.OPT_APPEND_NEWLINE)

        with open(self.filepath, "ab") as f:
            f.write(line)

        self._count += 1
        logger.info(
            "benchmark_instance_written", instance_id=instance.instance_id, total=self._count
        )

    def write_many(self, instances: list[BenchmarkInstance]) -> None:
        """Append multiple benchmark instances."""
        for instance in instances:
            self.write(instance)

    @property
    def count(self) -> int:
        """Number of instances written in this session."""
        return self._count

    @property
    def path(self) -> Path:
        """Path to the output file."""
        return self.filepath
