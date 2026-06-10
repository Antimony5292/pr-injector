"""AST node matching - locate functions, classes, and methods by name."""

from __future__ import annotations

import tree_sitter

from pr_injector.ast_engine.languages import (
    CLASS_NODE_TYPES,
    FUNCTION_NODE_TYPES,
)
from pr_injector.core.logging import get_logger

logger = get_logger(__name__)


class NodeMatch:
    """A matched AST node with its location and content."""

    def __init__(
        self,
        node: tree_sitter.Node,
        name: str,
        node_type: str,
        start_line: int,
        end_line: int,
        start_byte: int,
        end_byte: int,
        qualified_name: str | None = None,
    ) -> None:
        self.node = node
        self.name = name
        self.qualified_name = qualified_name or name
        self.node_type = node_type
        self.start_line = start_line
        self.end_line = end_line
        self.start_byte = start_byte
        self.end_byte = end_byte

    def get_text(self, source: bytes) -> str:
        """Extract the text of this node from the source."""
        return source[self.start_byte : self.end_byte].decode("utf-8", errors="replace")

    def __repr__(self) -> str:
        return (
            f"NodeMatch({self.node_type} '{self.qualified_name}' "
            f"L{self.start_line}-{self.end_line})"
        )


def _get_node_name(node: tree_sitter.Node, language: str) -> str | None:
    """Extract the name from a function/class definition node."""
    # For decorated definitions, look at the child definition
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type in ("function_definition", "class_definition"):
                return _get_node_name(child, language)
        return None

    # Find the name/identifier child
    for child in node.children:
        if child.type in ("identifier", "name", "property_identifier"):
            return child.text.decode("utf-8", errors="replace") if child.text else None

    return None


def _get_qualified_name(node: tree_sitter.Node, language: str) -> str | None:
    name = _get_node_name(node, language)
    if not name:
        return None

    parts = [name]
    parent = node.parent
    while parent is not None:
        if parent.type in CLASS_NODE_TYPES.get(language, []):
            parent_name = _get_node_name(parent, language)
            if parent_name:
                parts.append(parent_name)
        parent = parent.parent

    return ".".join(reversed(parts))


def find_functions(
    tree: tree_sitter.Tree, language: str
) -> list[NodeMatch]:
    """Find all function/method definitions in an AST.

    Args:
        tree: Parsed tree-sitter tree.
        language: Language name for node type lookup.

    Returns:
        List of NodeMatch for all functions found.
    """
    func_types = FUNCTION_NODE_TYPES.get(language, [])
    if not func_types:
        return []

    matches: list[NodeMatch] = []
    _walk_tree(tree.root_node, func_types, language, matches)
    return matches


def find_classes(
    tree: tree_sitter.Tree, language: str
) -> list[NodeMatch]:
    """Find all class definitions in an AST."""
    class_types = CLASS_NODE_TYPES.get(language, [])
    if not class_types:
        return []

    matches: list[NodeMatch] = []
    _walk_tree(tree.root_node, class_types, language, matches)
    return matches


def find_node_by_name(
    tree: tree_sitter.Tree,
    name: str,
    language: str,
    node_kind: str = "function",
) -> NodeMatch | None:
    """Find a specific named node in the AST.

    Args:
        tree: Parsed tree-sitter tree.
        name: Name of the function/class to find.
        language: Language name.
        node_kind: "function" or "class".

    Returns:
        NodeMatch if found, None otherwise.
    """
    if node_kind == "function":
        nodes = find_functions(tree, language)
    else:
        nodes = find_classes(tree, language)

    for match in nodes:
        if match.name == name or match.qualified_name == name:
            return match

    return None


def _walk_tree(
    node: tree_sitter.Node,
    target_types: list[str],
    language: str,
    matches: list[NodeMatch],
) -> None:
    """Recursively walk the AST collecting matching nodes."""
    if node.type in target_types:
        name = _get_node_name(node, language)
        if name:
            qualified_name = _get_qualified_name(node, language)
            matches.append(
                NodeMatch(
                    node=node,
                    name=name,
                    qualified_name=qualified_name,
                    node_type=node.type,
                    start_line=node.start_point[0] + 1,  # 1-indexed
                    end_line=node.end_point[0] + 1,
                    start_byte=node.start_byte,
                    end_byte=node.end_byte,
                )
            )

    for child in node.children:
        _walk_tree(child, target_types, language, matches)
