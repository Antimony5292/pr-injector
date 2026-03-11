"""Unit tests for configuration."""

from __future__ import annotations

from pr_injector.core.config import PRInjectorSettings


class TestPRInjectorSettings:
    def test_defaults(self):
        settings = PRInjectorSettings(
            _env_file=None,  # Don't load .env during tests
        )
        assert settings.github_token == ""
        assert settings.llm_model == "claude-sonnet-4-20250514"
        assert settings.max_workers == 4
        assert settings.blast_radius_threshold == 0.1
        assert settings.log_level == "INFO"
        assert settings.output_dir == "./benchmark_dataset"

    def test_env_prefix(self, monkeypatch):
        monkeypatch.setenv("PRI_GITHUB_TOKEN", "test-token-123")
        monkeypatch.setenv("PRI_MAX_WORKERS", "8")
        settings = PRInjectorSettings(_env_file=None)
        assert settings.github_token == "test-token-123"
        assert settings.max_workers == 8
