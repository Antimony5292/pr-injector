"""Stage 2: Level 1 (git revert) and Level 2 (AST surgery) injection."""

from __future__ import annotations

import os

from pr_injector.ast_engine.engine import ASTEngine
from pr_injector.ast_engine.hunk_surgeon import (
    old_changed_line_ranges_for_file,
    overlaps_any_range,
    reverse_patch_hunks_for_file,
)
from pr_injector.ast_engine.node_matcher import find_functions
from pr_injector.ast_engine.surgeon import ASTSurgeon
from pr_injector.core.compatibility import (
    CompatibilityReport,
    check_source_compatibility,
    reports_to_dicts,
)
from pr_injector.core.diff_parser import parse_diff, reverse_diff
from pr_injector.core.exceptions import ASTMatchFailed, ASTSurgeryFailed, RevertFailed
from pr_injector.core.git_ops import GitWorkspace
from pr_injector.core.logging import get_logger
from pr_injector.core.models import CandidatePR, InjectionLevel, RevertResult
from pr_injector.core.patch_ops import reset_worktree

logger = get_logger(__name__)


class PRReverter:
    """Stage 2: Attempt Level 1 and Level 2 injection.

    Tries git revert first (Level 1), then falls back to AST surgery
    (Level 2) if the revert fails due to context drift.
    """

    def __init__(
        self,
        workspace: GitWorkspace,
        ast_engine: ASTEngine | None = None,
    ) -> None:
        self.workspace = workspace
        self.ast_engine = ast_engine or ASTEngine()
        self.surgeon = ASTSurgeon(self.ast_engine)

    async def revert(
        self,
        candidate: CandidatePR,
        repo_path: str,
    ) -> RevertResult:
        """Try Level 1 (git revert), then Level 2 (AST surgery).

        Args:
            candidate: The PR candidate to revert.
            repo_path: Path to the cloned repository.

        Returns:
            RevertResult with injection details.

        Raises:
            RevertFailed: If both Level 1 and Level 2 fail.
        """
        # Create an isolated worktree for this attempt
        worktree_path = await self.workspace.create_worktree(
            repo_path, suffix=f"pr-{candidate.metadata.pr_number}"
        )

        try:
            # Try Level 1: Clean git revert
            result = await self._try_level_1(candidate, worktree_path)
            if result is not None:
                return result

            # Reset worktree before Level 2 attempt
            await reset_worktree(worktree_path)

            # Try Level 2: AST surgery
            result = await self._try_level_2(candidate, worktree_path, repo_path)
            if result is not None:
                return result

            await self.workspace.remove_worktree(worktree_path)
            raise RevertFailed(
                f"PR #{candidate.metadata.pr_number}: Both Level 1 and Level 2 injection failed"
            )

        except RevertFailed:
            await self.workspace.remove_worktree(worktree_path)
            raise
        except Exception as e:
            # Clean up on unexpected errors
            await self.workspace.remove_worktree(worktree_path)
            raise RevertFailed(
                f"PR #{candidate.metadata.pr_number}: Unexpected error: {e}"
            ) from e

    async def _try_level_1(
        self,
        candidate: CandidatePR,
        worktree_path: str,
    ) -> RevertResult | None:
        """Attempt Level 1: Clean git revert.

        Returns RevertResult on success, None on failure.
        """
        logger.info(
            "level_1_attempt",
            pr=candidate.metadata.pr_number,
            commit=candidate.metadata.merge_commit_sha[:8],
        )

        success, diff_or_error = await self.workspace.try_revert_commit(
            worktree_path, candidate.metadata.merge_commit_sha
        )

        if not success:
            logger.info(
                "level_1_failed",
                pr=candidate.metadata.pr_number,
                error=diff_or_error[:200],
            )
            return None

        # The injected diff is what was applied (the revert)
        # The golden patch is the reverse (the fix)
        golden_patch = reverse_diff(diff_or_error)

        logger.info(
            "level_1_success",
            pr=candidate.metadata.pr_number,
        )

        return RevertResult(
            candidate=candidate,
            level=InjectionLevel.LEVEL_1_CLEAN_REVERT,
            injected_diff=diff_or_error,
            golden_patch=golden_patch,
            worktree_path=worktree_path,
        )

    async def _try_level_2(
        self,
        candidate: CandidatePR,
        worktree_path: str,
        repo_path: str,
    ) -> RevertResult | None:
        """Attempt Level 2: AST surgery.

        Locates target functions in the current code and replaces them
        with the pre-fix (buggy) versions.

        Returns RevertResult on success, None on failure.
        """
        logger.info(
            "level_2_attempt",
            pr=candidate.metadata.pr_number,
        )

        # Get the original diff to understand what was changed
        try:
            original_diff = await self.workspace.get_commit_diff(
                repo_path, candidate.metadata.merge_commit_sha
            )
        except Exception as e:
            logger.warning("level_2_cannot_get_diff", error=str(e))
            return None

        if not original_diff:
            return None

        # Parse the diff to find which source files were modified
        analysis = parse_diff(original_diff)
        if not analysis.source_files:
            logger.info("level_2_no_source_files", pr=candidate.metadata.pr_number)
            return None

        all_diffs: list[str] = []
        conflict_files: list[str] = []
        compatibility_flagged_files: list[str] = []
        compatibility_rejected_files: list[str] = []
        compatibility_reports: list[CompatibilityReport] = []
        hunk_replacements: list[dict] = []
        skipped_whole_function_replacements: list[str] = []
        level2_mode = os.environ.get("PRI_LEVEL2_MODE", "original_ast").lower()
        use_hunk_first = level2_mode in {"hunk_first", "conservative_hunk_first"}
        allow_whole_function = os.environ.get("PRI_ALLOW_WHOLE_FUNCTION_LEVEL2", "1").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        reject_on_compatibility = os.environ.get("PRI_REJECT_COMPATIBILITY", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        for source_file in analysis.source_files:
            # Read the current version of the file
            current_content = await self.workspace.read_file_at_head(
                worktree_path, source_file
            )
            if current_content is None:
                conflict_files.append(source_file)
                continue

            try:
                # Try to identify functions changed in the original fix
                # and replace them with pre-fix versions
                modified_content = None
                if use_hunk_first:
                    hunk_result = reverse_patch_hunks_for_file(
                        source_file, current_content, original_diff
                    )
                    if hunk_result.changed:
                        modified_content = hunk_result.content
                        hunk_replacements.extend(hunk_result.to_metadata())

                if modified_content is None:
                    if not allow_whole_function:
                        skipped_whole_function_replacements.append(source_file)
                        continue
                    modified_content = await self._ast_revert_file(
                        source_file, current_content, original_diff, repo_path, candidate
                    )

                if modified_content and modified_content != current_content:
                    compatibility = check_source_compatibility(
                        source_file, current_content, modified_content
                    )
                    compatibility_reports.append(compatibility)
                    if compatibility.checked and not compatibility.passed:
                        compatibility_flagged_files.append(source_file)
                        logger.info(
                            "level_2_compatibility_flagged",
                            file=source_file,
                            issues=[issue.code for issue in compatibility.issues],
                        )
                        if reject_on_compatibility:
                            conflict_files.append(source_file)
                            compatibility_rejected_files.append(source_file)
                            continue

                    diff = self.surgeon.apply_surgery(
                        worktree_path, source_file, modified_content
                    )
                    if diff:
                        all_diffs.append(diff)

            except (ASTMatchFailed, ASTSurgeryFailed) as e:
                logger.info(
                    "level_2_file_failed",
                    file=source_file,
                    error=str(e),
                )
                conflict_files.append(source_file)

        if compatibility_rejected_files:
            logger.info(
                "level_2_rejected_by_compatibility",
                pr=candidate.metadata.pr_number,
                files=compatibility_rejected_files,
            )
            return None

        if not all_diffs:
            logger.info("level_2_no_successful_reverts", pr=candidate.metadata.pr_number)
            return None

        injected_diff = "\n".join(all_diffs)
        golden_patch = reverse_diff(injected_diff)

        logger.info(
            "level_2_success",
            pr=candidate.metadata.pr_number,
            files_reverted=len(all_diffs),
            conflicts=len(conflict_files),
        )

        return RevertResult(
            candidate=candidate,
            level=InjectionLevel.LEVEL_2_AST_SURGERY,
            injected_diff=injected_diff,
            golden_patch=golden_patch,
            worktree_path=worktree_path,
            conflict_files=conflict_files,
            compatibility_reports=reports_to_dicts(compatibility_reports),
            quality_metadata={
                "hunk_replacements": hunk_replacements,
                "compatibility_flagged_files": compatibility_flagged_files,
                "compatibility_rejected_files": compatibility_rejected_files,
                "skipped_whole_function_replacements": skipped_whole_function_replacements,
                "whole_function_level2_enabled": allow_whole_function,
                "level2_mode": level2_mode,
                "hunk_first_enabled": use_hunk_first,
                "reject_on_compatibility": reject_on_compatibility,
            },
        )

    async def _ast_revert_file(
        self,
        file_path: str,
        current_content: str,
        original_diff: str,
        repo_path: str,
        candidate: CandidatePR,
    ) -> str | None:
        """Use AST surgery to revert changes in a single file.

        Identifies functions that were modified in the original fix
        and attempts to replace the current version with the pre-fix version.
        """
        from pr_injector.ast_engine.languages import detect_language

        language = detect_language(file_path)
        if language is None:
            return None

        # Parse the current file to find all functions
        tree = self.ast_engine.parse_source(current_content, language=language)
        if tree is None:
            return None

        current_functions = find_functions(tree, language)
        if not current_functions:
            return None

        # Try to get the pre-fix version of the file from the commit's parent
        try:
            from git import Repo

            repo = Repo(repo_path)
            commit = repo.commit(candidate.metadata.merge_commit_sha)
            if not commit.parents:
                return None

            parent = commit.parents[0]
            try:
                blob = parent.tree / file_path
                pre_fix_content = blob.data_stream.read().decode("utf-8", errors="replace")
            except KeyError:
                return None

        except Exception:
            return None

        # Parse the pre-fix version
        pre_fix_tree = self.ast_engine.parse_source(pre_fix_content, language=language)
        if pre_fix_tree is None:
            return None

        pre_fix_functions = find_functions(pre_fix_tree, language)
        changed_ranges = old_changed_line_ranges_for_file(file_path, original_diff)
        if changed_ranges:
            pre_fix_functions = [
                func for func in pre_fix_functions
                if overlaps_any_range(func.start_line, func.end_line, changed_ranges)
            ]
        pre_fix_map = {f.qualified_name: f for f in pre_fix_functions}
        bare_name_counts: dict[str, int] = {}
        for func in pre_fix_functions:
            bare_name_counts[func.name] = bare_name_counts.get(func.name, 0) + 1
        for func in pre_fix_functions:
            if bare_name_counts[func.name] == 1:
                pre_fix_map.setdefault(func.name, func)

        # Find functions that exist in both versions (potential revert targets)
        modified_content = current_content
        replaced_any = False

        for current_func in current_functions:
            lookup_name = current_func.qualified_name
            if lookup_name not in pre_fix_map and bare_name_counts.get(current_func.name) == 1:
                lookup_name = current_func.name
            if lookup_name in pre_fix_map:
                pre_fix_func = pre_fix_map[lookup_name]

                # Get the function text from both versions
                current_text = current_func.get_text(current_content.encode("utf-8"))
                pre_fix_text = pre_fix_func.get_text(pre_fix_content.encode("utf-8"))

                # Only replace if the functions differ (meaning this function was fixed)
                if current_text != pre_fix_text:
                    try:
                        modified_content = self.surgeon.compute_replacement(
                            modified_content,
                            pre_fix_text,
                            current_func.qualified_name,
                            file_path,
                        )
                        replaced_any = True
                    except (ASTMatchFailed, ASTSurgeryFailed):
                        continue

        return modified_content if replaced_any else None
