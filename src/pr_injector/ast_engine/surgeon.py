"""AST-guided block replacement for Level 2 injection."""

from __future__ import annotations

from pathlib import Path

from pr_injector.ast_engine.engine import ASTEngine
from pr_injector.ast_engine.languages import detect_language
from pr_injector.ast_engine.node_matcher import NodeMatch, find_node_by_name
from pr_injector.core.exceptions import ASTMatchFailed, ASTSurgeryFailed
from pr_injector.core.logging import get_logger

logger = get_logger(__name__)


class ASTSurgeon:
    """Performs Level 2 AST-guided block replacement.

    Given the current source code and the original pre-fix/post-fix versions
    of a function, computes and applies the replacement that reintroduces
    the bug into the current code.
    """

    def __init__(self, engine: ASTEngine | None = None) -> None:
        self.engine = engine or ASTEngine()

    def locate_target(
        self,
        source_code: str,
        target_name: str,
        file_path: str,
        node_kind: str = "function",
    ) -> NodeMatch:
        """Locate a target function or class in the current source.

        Args:
            source_code: Current source code content.
            target_name: Name of the function/class to find.
            file_path: Path for language detection.
            node_kind: "function" or "class".

        Returns:
            NodeMatch for the target.

        Raises:
            ASTMatchFailed: If the target cannot be found.
        """
        language = detect_language(file_path)
        if language is None:
            raise ASTMatchFailed(f"Cannot detect language for {file_path}")

        tree = self.engine.parse_source(source_code, language=language)
        if tree is None:
            raise ASTMatchFailed(f"Failed to parse {file_path}")

        match = find_node_by_name(tree, target_name, language, node_kind)
        if match is None:
            raise ASTMatchFailed(
                f"Cannot find {node_kind} '{target_name}' in {file_path}"
            )

        logger.info(
            "target_located",
            name=target_name,
            file=file_path,
            lines=f"{match.start_line}-{match.end_line}",
        )
        return match

    def compute_replacement(
        self,
        current_source: str,
        original_buggy_function: str,
        target_name: str,
        file_path: str,
        node_kind: str = "function",
    ) -> str:
        """Compute the source code with the target function replaced.

        This replaces the current version of the function with the original
        buggy version, effectively reintroducing the bug.

        Args:
            current_source: Current file content.
            original_buggy_function: The buggy version of the function body.
            target_name: Name of the function to replace.
            file_path: For language detection.
            node_kind: "function" or "class".

        Returns:
            Modified source code with the bug reintroduced.

        Raises:
            ASTMatchFailed: Target not found.
            ASTSurgeryFailed: Replacement produces invalid code.
        """
        match = self.locate_target(current_source, target_name, file_path, node_kind)

        source_bytes = current_source.encode("utf-8")
        before = source_bytes[: match.start_byte]
        after = source_bytes[match.end_byte :]

        modified = before + original_buggy_function.encode("utf-8") + after
        modified_source = modified.decode("utf-8", errors="replace")

        # Validate the modified source still parses
        language = detect_language(file_path)
        if language:
            tree = self.engine.parse_source(modified_source, language=language)
            if tree is None:
                raise ASTSurgeryFailed(
                    f"Modified source for {file_path} does not parse correctly"
                )
            # Check for syntax errors
            if tree.root_node.has_error:
                raise ASTSurgeryFailed(
                    f"Modified source for {file_path} contains syntax errors after "
                    f"replacing {target_name}"
                )

        return modified_source

    def apply_surgery(
        self,
        worktree_path: str,
        file_path: str,
        modified_source: str,
    ) -> str:
        """Write the modified source to the worktree and return the diff.

        Args:
            worktree_path: Path to the git worktree.
            file_path: Relative path within the worktree.
            modified_source: The modified source code.

        Returns:
            Unified diff of the change.

        Raises:
            ASTSurgeryFailed: If the file cannot be written.
        """
        full_path = Path(worktree_path) / file_path

        if not full_path.exists():
            raise ASTSurgeryFailed(f"Target file does not exist: {file_path}")

        try:
            original = full_path.read_text(encoding="utf-8", errors="replace")
            full_path.write_text(modified_source, encoding="utf-8")
            logger.info("surgery_applied", file=file_path)

            # Generate a simple unified diff
            return _generate_diff(file_path, original, modified_source)

        except OSError as e:
            raise ASTSurgeryFailed(f"Failed to write {file_path}: {e}") from e


def _generate_diff(file_path: str, original: str, modified: str) -> str:
    """Generate a unified diff between original and modified content."""
    import difflib

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        modified_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    )

    diff_text = "".join(diff)
    # Prepend "diff --git" header if difflib didn't produce one
    if diff_text and not diff_text.startswith("diff --git"):
        diff_text = f"diff --git a/{file_path} b/{file_path}\n{diff_text}"
    return diff_text
