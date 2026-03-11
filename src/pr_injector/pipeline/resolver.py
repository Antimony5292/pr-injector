"""Stage 3: Level 3 LLM semantic injection and Level 4 auto-discard."""

from __future__ import annotations

from pr_injector.core.diff_parser import extract_source_diff, is_test_file, parse_diff
from pr_injector.core.exceptions import (
    ArchitectureDeprecated,
    SemanticInjectionFailed,
)
from pr_injector.core.git_ops import GitWorkspace
from pr_injector.core.logging import get_logger
from pr_injector.core.models import CandidatePR, LLMInjectionResult
from pr_injector.llm.client import LLMClient

logger = get_logger(__name__)


class PRResolver:
    """Stage 3: LLM semantic injection for heavily refactored code.

    When Level 1 and Level 2 fail, this stage uses an LLM to understand
    the original bug and recreate it in the current architecture.
    Level 4 auto-discards PRs where the architecture has been deprecated.
    """

    def __init__(
        self,
        workspace: GitWorkspace,
        llm_client: LLMClient,
    ) -> None:
        self.workspace = workspace
        self.llm_client = llm_client

    async def resolve(
        self,
        candidate: CandidatePR,
        repo_path: str,
        original_diff: str,
    ) -> LLMInjectionResult:
        """Use LLM to recreate the bug semantically in current code.

        Args:
            candidate: The PR candidate.
            repo_path: Path to the cloned repository.
            original_diff: The original fix diff.

        Returns:
            LLMInjectionResult with the semantic injection.

        Raises:
            ArchitectureDeprecated: Level 4 - feature/dependency removed.
            SemanticInjectionFailed: LLM could not produce valid injection.
        """
        # Check for Level 4: Architecture Deprecated
        await self._check_architecture_deprecated(candidate, repo_path)

        logger.info(
            "level_3_attempt",
            pr=candidate.metadata.pr_number,
        )

        # Create a worktree for this injection
        worktree_path = await self.workspace.create_worktree(
            repo_path, suffix=f"llm-pr-{candidate.metadata.pr_number}"
        )

        try:
            # Gather current versions of the modified source files
            analysis = parse_diff(original_diff)
            current_files: dict[str, str] = {}

            for source_file in analysis.source_files:
                content = await self.workspace.read_file_at_head(
                    worktree_path, source_file
                )
                if content is not None:
                    current_files[source_file] = content

            if not current_files:
                raise SemanticInjectionFailed(
                    f"PR #{candidate.metadata.pr_number}: No source files found in current codebase"
                )

            # Build problem statement from PR metadata
            issue_description = self._build_issue_description(candidate)

            # Call LLM to generate semantic injection
            diff, confidence, prompt_tokens, completion_tokens = (
                await self.llm_client.generate_semantic_injection(
                    issue_description=issue_description,
                    original_diff=extract_source_diff(original_diff),
                    current_files=current_files,
                )
            )

            # Try to apply the generated diff
            success = await self.workspace.apply_patch(worktree_path, diff)
            if not success:
                raise SemanticInjectionFailed(
                    "LLM-generated diff could not be applied to the worktree"
                )

            # The golden patch is the reverse of the injection
            from pr_injector.core.diff_parser import reverse_diff

            golden_patch = reverse_diff(diff)

            logger.info(
                "level_3_success",
                pr=candidate.metadata.pr_number,
                confidence=round(confidence, 3),
                model=self.llm_client.model,
            )

            return LLMInjectionResult(
                candidate=candidate,
                injected_diff=diff,
                golden_patch=golden_patch,
                worktree_path=worktree_path,
                model_used=self.llm_client.model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                confidence_score=confidence,
            )

        except (ArchitectureDeprecated, SemanticInjectionFailed):
            await self.workspace.remove_worktree(worktree_path)
            raise
        except Exception as e:
            await self.workspace.remove_worktree(worktree_path)
            raise SemanticInjectionFailed(
                f"PR #{candidate.metadata.pr_number}: {e}"
            ) from e

    async def _check_architecture_deprecated(
        self,
        candidate: CandidatePR,
        repo_path: str,
    ) -> None:
        """Check if the PR's target code has been completely removed.

        If none of the test files from the original PR exist, and none
        of the source files exist, the architecture is considered deprecated.
        """
        # Check test files
        test_files_exist = False
        for test_file in candidate.metadata.test_files:
            if await self.workspace.file_exists_at_head(repo_path, test_file):
                test_files_exist = True
                break

        if not test_files_exist and candidate.metadata.test_files:
            logger.info(
                "level_4_deprecated_tests",
                pr=candidate.metadata.pr_number,
                test_files=candidate.metadata.test_files,
            )
            raise ArchitectureDeprecated(
                f"PR #{candidate.metadata.pr_number}: All test files have been removed"
            )

        # Check source files (exclude test files)
        source_files = [
            f for f in candidate.metadata.changed_files
            if not is_test_file(f)
        ]
        source_files_exist = False
        for source_file in source_files:
            if await self.workspace.file_exists_at_head(repo_path, source_file):
                source_files_exist = True
                break

        if not source_files_exist and source_files:
            logger.info(
                "level_4_deprecated_source",
                pr=candidate.metadata.pr_number,
            )
            raise ArchitectureDeprecated(
                f"PR #{candidate.metadata.pr_number}: All source files have been removed"
            )

    @staticmethod
    def _build_issue_description(candidate: CandidatePR) -> str:
        """Build a problem statement from PR metadata."""
        parts: list[str] = []

        parts.append(f"PR #{candidate.metadata.pr_number}: {candidate.metadata.title}")

        if candidate.metadata.body:
            # Truncate very long bodies
            body = candidate.metadata.body
            if len(body) > 3000:
                body = body[:3000] + "... (truncated)"
            parts.append(body)

        return "\n\n".join(parts)
