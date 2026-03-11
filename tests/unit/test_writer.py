"""Unit tests for JSONL writer."""

from __future__ import annotations

import json

from pr_injector.output.writer import JSONLWriter


class TestJSONLWriter:
    def test_write_single(self, tmp_path, sample_benchmark):
        writer = JSONLWriter(str(tmp_path))
        writer.write(sample_benchmark)

        assert writer.count == 1
        assert writer.path.exists()

        with open(writer.path) as f:
            line = f.readline()
            data = json.loads(line)

        assert data["instance_id"] == "flask-pr-5001"
        assert data["repo"] == "pallets/flask"
        assert data["injection_level"] == "Level_1_Clean_Revert"

    def test_write_multiple(self, tmp_path, sample_benchmark):
        writer = JSONLWriter(str(tmp_path))
        writer.write(sample_benchmark)
        writer.write(sample_benchmark)
        writer.write(sample_benchmark)

        assert writer.count == 3

        with open(writer.path) as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_creates_directory(self, tmp_path):
        output_dir = tmp_path / "nested" / "output"
        JSONLWriter(str(output_dir))
        assert output_dir.exists()

    def test_custom_filename(self, tmp_path, sample_benchmark):
        writer = JSONLWriter(str(tmp_path), filename="custom.jsonl")
        writer.write(sample_benchmark)
        assert (tmp_path / "custom.jsonl").exists()
