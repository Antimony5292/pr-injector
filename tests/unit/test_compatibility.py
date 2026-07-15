"""Tests for source compatibility checks."""

from __future__ import annotations

import ast

from construction_toolkit.bug_transplant.scripts.inject_swebench_pro import _extract_functions

from pr_injector.core.compatibility import check_python_source_compatibility


def test_compatibility_flags_new_unresolved_symbol():
    current = """
def fetch(url):
    return request.urlopen(url)
"""
    modified = """
def fetch(url):
    return urllib_request.urlopen(url)
"""

    report = check_python_source_compatibility("pkg/http.py", current, modified)

    assert report.checked is True
    assert report.passed is False
    assert [issue.symbol for issue in report.issues] == ["urllib_request"]


def test_compatibility_allows_existing_unresolved_symbol():
    current = """
def fetch(url):
    return request.urlopen(url)
"""
    modified = """
def fetch(url):
    return request.open(url)
"""

    report = check_python_source_compatibility("pkg/http.py", current, modified)

    assert report.passed is True
    assert report.issues == []


def test_compatibility_flags_function_signature_drift():
    current = """
class Query:
    def add_annotation(self, annotation, alias, select=True):
        return annotation
"""
    modified = """
class Query:
    def add_annotation(self, annotation, alias, is_summary=False):
        return annotation
"""

    report = check_python_source_compatibility("django/db/models/sql/query.py", current, modified)

    assert report.passed is False
    assert any(issue.code == "function_signature_drift" for issue in report.issues)


def test_extract_functions_uses_qualified_method_names():
    source = """
class A:
    def run(self):
        return "a"

class B:
    def run(self):
        return "b"

def top():
    return "top"
"""

    funcs = _extract_functions(source, ast.parse(source))

    assert "A.run" in funcs
    assert "B.run" in funcs
    assert "run" not in funcs
    assert "top" in funcs
