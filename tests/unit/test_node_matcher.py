"""Tests for tree-sitter node matching."""

from __future__ import annotations

from pr_injector.ast_engine.engine import ASTEngine
from pr_injector.ast_engine.node_matcher import find_functions, find_node_by_name


def test_find_functions_records_qualified_names():
    source = """
class A:
    def run(self):
        return "a"

class B:
    def run(self):
        return "b"
"""
    tree = ASTEngine().parse_source(source, language="python")

    funcs = find_functions(tree, "python")  # type: ignore[arg-type]

    assert [(func.name, func.qualified_name) for func in funcs] == [
        ("run", "A.run"),
        ("run", "B.run"),
    ]


def test_find_node_by_qualified_name_disambiguates_methods():
    source = """
class A:
    def run(self):
        return "a"

class B:
    def run(self):
        return "b"
"""
    tree = ASTEngine().parse_source(source, language="python")

    match = find_node_by_name(tree, "B.run", "python")  # type: ignore[arg-type]

    assert match is not None
    assert match.qualified_name == "B.run"
    assert 'return "b"' in match.get_text(source.encode("utf-8"))
