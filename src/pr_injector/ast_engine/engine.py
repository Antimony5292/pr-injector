"""Tree-sitter parser initialization and multi-language AST parsing."""

from __future__ import annotations

import importlib
from pathlib import Path

import tree_sitter

from pr_injector.ast_engine.languages import LANGUAGE_TO_PACKAGE, detect_language
from pr_injector.core.logging import get_logger

logger = get_logger(__name__)


class ASTEngine:
    """Multi-language AST parsing engine via tree-sitter."""

    def __init__(self) -> None:
        self._parsers: dict[str, tree_sitter.Parser] = {}

    def _get_parser(self, language: str) -> tree_sitter.Parser | None:
        """Get or create a tree-sitter parser for the given language."""
        if language in self._parsers:
            return self._parsers[language]

        package_name = LANGUAGE_TO_PACKAGE.get(language)
        if not package_name:
            logger.warning("unsupported_language", language=language)
            return None

        try:
            module = importlib.import_module(package_name)
            lang = tree_sitter.Language(module.language())
            parser = tree_sitter.Parser(lang)
            self._parsers[language] = parser
            logger.info("parser_initialized", language=language)
            return parser
        except ImportError:
            logger.warning(
                "language_package_not_installed",
                language=language,
                package=package_name,
                hint=f"Install with: pip install {package_name.replace('_', '-')}",
            )
            return None
        except Exception as e:
            logger.error("parser_init_failed", language=language, error=str(e))
            return None

    def parse_source(
        self, source_code: str, language: str | None = None, file_path: str | None = None
    ) -> tree_sitter.Tree | None:
        """Parse source code into a tree-sitter AST.

        Args:
            source_code: Source code string.
            language: Language name. If None, inferred from file_path.
            file_path: Path to source file for language detection.

        Returns:
            Parsed tree or None if parsing fails.
        """
        if language is None and file_path is not None:
            language = detect_language(file_path)

        if language is None:
            logger.warning("cannot_detect_language", file_path=file_path)
            return None

        parser = self._get_parser(language)
        if parser is None:
            return None

        try:
            tree = parser.parse(source_code.encode("utf-8"))
            return tree
        except Exception as e:
            logger.error("parse_failed", language=language, error=str(e))
            return None

    def parse_file(self, file_path: str) -> tree_sitter.Tree | None:
        """Parse a source file into a tree-sitter AST."""
        path = Path(file_path)
        if not path.exists():
            return None

        source = path.read_text(encoding="utf-8", errors="replace")
        return self.parse_source(source, file_path=file_path)

    @property
    def supported_languages(self) -> list[str]:
        """List of languages with available parsers."""
        available = []
        for language, package in LANGUAGE_TO_PACKAGE.items():
            try:
                importlib.import_module(package)
                available.append(language)
            except ImportError:
                pass
        return available
