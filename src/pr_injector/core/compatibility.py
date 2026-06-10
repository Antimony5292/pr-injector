"""Lightweight compatibility checks for injected source edits.

These checks are intentionally conservative. They are not a substitute for
running target tests, but they catch common Level 2 failure modes before a
candidate is accepted: syntax/compile errors and newly introduced unresolved
symbols from an older codebase.
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path

_PYTHON_EXTENSIONS = {".py"}
_BUILTIN_NAMES = set(dir(builtins))


@dataclass(frozen=True)
class CompatibilityIssue:
    """A single source compatibility issue."""

    code: str
    message: str
    severity: str = "error"
    symbol: str | None = None


@dataclass
class CompatibilityReport:
    """Compatibility result for one edited file."""

    file_path: str
    checked: bool
    passed: bool
    issues: list[CompatibilityIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "checked": self.checked,
            "passed": self.passed,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "severity": issue.severity,
                    "symbol": issue.symbol,
                }
                for issue in self.issues
            ],
        }


class _NameCollector(ast.NodeVisitor):
    """Collect a pragmatic module-wide approximation of defined and loaded names."""

    def __init__(self) -> None:
        self.defined: set[str] = set()
        self.loaded: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self.loaded.add(node.id)
        elif isinstance(node.ctx, (ast.Store, ast.Del)):
            self.defined.add(node.id)

    def visit_arg(self, node: ast.arg) -> None:  # noqa: N802
        self.defined.add(node.arg)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.defined.add(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.defined.add(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self.defined.add(node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.defined.add(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "*":
                continue
            self.defined.add(alias.asname or alias.name)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name:
            self.defined.add(node.name)
        self.generic_visit(node)


def check_source_compatibility(
    file_path: str,
    current_source: str,
    modified_source: str,
) -> CompatibilityReport:
    """Check whether an edited source file has obvious compatibility hazards."""

    if Path(file_path).suffix not in _PYTHON_EXTENSIONS:
        return CompatibilityReport(file_path=file_path, checked=False, passed=True)
    return check_python_source_compatibility(file_path, current_source, modified_source)


def check_python_source_compatibility(
    file_path: str,
    current_source: str,
    modified_source: str,
) -> CompatibilityReport:
    """Check Python syntax and newly introduced unresolved names."""

    issues: list[CompatibilityIssue] = []
    try:
        current_tree = ast.parse(current_source, filename=file_path)
    except SyntaxError:
        current_tree = None

    try:
        modified_tree = ast.parse(modified_source, filename=file_path)
    except SyntaxError as exc:
        return CompatibilityReport(
            file_path=file_path,
            checked=True,
            passed=False,
            issues=[
                CompatibilityIssue(
                    code="python_syntax_error",
                    message=f"modified source does not parse: {exc.msg}",
                )
            ],
        )

    try:
        compile(modified_tree, file_path, "exec")
    except SyntaxError as exc:
        issues.append(
            CompatibilityIssue(
                code="python_compile_error",
                message=f"modified source does not compile: {exc.msg}",
            )
        )

    current_unbound = _unbound_names(current_tree) if current_tree is not None else set()
    modified_unbound = _unbound_names(modified_tree)
    new_unbound = sorted(modified_unbound - current_unbound)
    for name in new_unbound:
        issues.append(
            CompatibilityIssue(
                code="new_unresolved_symbol",
                message=f"modified source introduces unresolved symbol '{name}'",
                symbol=name,
            )
        )

    if current_tree is not None:
        current_signatures = _function_signatures(current_tree)
        modified_signatures = _function_signatures(modified_tree)
        for qualname, current_signature in sorted(current_signatures.items()):
            modified_signature = modified_signatures.get(qualname)
            if modified_signature and modified_signature != current_signature:
                issues.append(
                    CompatibilityIssue(
                        code="function_signature_drift",
                        message=(
                            f"modified source changes signature of '{qualname}' "
                            f"from {current_signature} to {modified_signature}"
                        ),
                        symbol=qualname,
                    )
                )

    return CompatibilityReport(
        file_path=file_path,
        checked=True,
        passed=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def _unbound_names(tree: ast.AST) -> set[str]:
    collector = _NameCollector()
    collector.visit(tree)
    return collector.loaded - collector.defined - _BUILTIN_NAMES


def _function_signatures(tree: ast.AST) -> dict[str, str]:
    signatures: dict[str, str] = {}

    class SignatureCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            self._record(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            self._record(node)
            self.generic_visit(node)

        def _record(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualname = ".".join([*self.class_stack, node.name])
            signatures[qualname] = _args_signature(node.args)

    SignatureCollector().visit(tree)
    return signatures


def _args_signature(args: ast.arguments) -> str:
    parts: list[str] = []
    posonly = [_arg_name(arg) for arg in args.posonlyargs]
    regular = [_arg_name(arg) for arg in args.args]
    if posonly:
        parts.extend(posonly)
        parts.append("/")
    parts.extend(regular)
    if args.vararg:
        parts.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        parts.append("*")
    parts.extend(_arg_name(arg) for arg in args.kwonlyargs)
    if args.kwarg:
        parts.append("**" + args.kwarg.arg)
    return "(" + ", ".join(parts) + ")"


def _arg_name(arg: ast.arg) -> str:
    return arg.arg


def reports_to_dicts(reports: list[CompatibilityReport]) -> list[dict]:
    return [report.to_dict() for report in reports]
