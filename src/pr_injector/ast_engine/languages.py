"""Language grammar registry and file extension mapping for tree-sitter."""

from __future__ import annotations

# Map file extensions to tree-sitter language names
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
}

# Map language names to their tree-sitter package import names
LANGUAGE_TO_PACKAGE: dict[str, str] = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "java": "tree_sitter_java",
    "go": "tree_sitter_go",
    "rust": "tree_sitter_rust",
    "ruby": "tree_sitter_ruby",
}

# Node types for function/method definitions per language
FUNCTION_NODE_TYPES: dict[str, list[str]] = {
    "python": ["function_definition", "decorated_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "java": ["method_declaration", "constructor_declaration"],
    "go": ["function_declaration", "method_declaration"],
    "rust": ["function_item"],
    "ruby": ["method", "singleton_method"],
}

# Node types for class definitions per language
CLASS_NODE_TYPES: dict[str, list[str]] = {
    "python": ["class_definition", "decorated_definition"],
    "javascript": ["class_declaration"],
    "typescript": ["class_declaration"],
    "java": ["class_declaration", "interface_declaration"],
    "go": ["type_declaration"],
    "rust": ["struct_item", "impl_item", "trait_item"],
    "ruby": ["class", "module"],
}


def detect_language(file_path: str) -> str | None:
    """Detect programming language from file extension.

    Args:
        file_path: Path to a source file.

    Returns:
        Language name string or None if unknown.
    """
    from pathlib import Path

    ext = Path(file_path).suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(ext)
