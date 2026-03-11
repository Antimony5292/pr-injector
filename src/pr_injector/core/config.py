"""PR-Injector configuration via environment variables and .env file."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class PRInjectorSettings(BaseSettings):
    """All configuration loaded from environment / .env file."""

    model_config = {"env_prefix": "PRI_", "env_file": ".env", "env_file_encoding": "utf-8"}

    # GitHub
    github_token: str = ""
    github_api_base: str = "https://api.github.com"
    github_max_concurrent: int = 10

    # LLM
    llm_provider: str = "azure"
    llm_model: str = "claude-sonnet-4-20250514"
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 16384
    llm_max_retries: int = 3

    # Azure OpenAI (when llm_provider=azure)
    azure_endpoint: str = ""
    azure_deployment: str = ""
    azure_api_version: str = "2024-12-01-preview"

    # Pipeline
    workspace_dir: str = ".pri-workspace"
    max_workers: int = 4
    default_strategy: str = "auto"

    # Verifier
    test_timeout_seconds: int = 300
    blast_radius_threshold: float = 0.1

    # AST
    tree_sitter_grammar_dir: str | None = None

    # Output
    output_dir: str = "./benchmark_dataset"

    # Logging
    log_level: str = "INFO"
    log_format: str = "console"


def get_settings() -> PRInjectorSettings:
    """Create and return settings instance."""
    return PRInjectorSettings()
