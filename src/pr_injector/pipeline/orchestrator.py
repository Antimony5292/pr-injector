"""Pipeline orchestrator: drives the 4-stage funnel pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime

from pr_injector.core.diff_parser import extract_test_diff
from pr_injector.core.exceptions import (
    ArchitectureDeprecated,
    RevertFailed,
    SemanticInjectionFailed,
)
from pr_injector.core.git_ops import GitWorkspace
from pr_injector.core.logging import get_logger
from pr_injector.core.models import (
    BenchmarkInstance,
    CandidatePR,
    InjectionLevel,
    InjectionStrategy,
)
from pr_injector.output.writer import JSONLWriter
from pr_injector.pipeline.miner import PRMiner
from pr_injector.pipeline.resolver import PRResolver
from pr_injector.pipeline.reverter import PRReverter
from pr_injector.pipeline.verifier import TestVerifier

logger = get_logger(__name__)


class PipelineOrchestrator:
    """Drives the 4-stage funnel pipeline.

    Coordinates: Miner → Reverter → Resolver → Verifier
    Produces BenchmarkInstance records written to JSONL output.
    """

    def __init__(
        self,
        miner: PRMiner,
        reverter: PRReverter,
        resolver: PRResolver,
        verifier: TestVerifier,
        writer: JSONLWriter,
        workspace: GitWorkspace,
    ) -> None:
        self.miner = miner
        self.reverter = reverter
        self.resolver = resolver
        self.verifier = verifier
        self.writer = writer
        self.workspace = workspace

        # Statistics
        self.stats = {
            "total_candidates": 0,
            "level_1_success": 0,
            "level_2_success": 0,
            "level_3_success": 0,
            "level_4_deprecated": 0,
            "verification_passed": 0,
            "verification_failed": 0,
            "errors": 0,
        }

    async def run_single(
        self,
        repo: str,
        pr_number: int,
        strategy: InjectionStrategy = InjectionStrategy.AUTO,
    ) -> BenchmarkInstance | None:
        """Inject a single PR and produce a benchmark instance.

        Args:
            repo: Repository in "owner/name" format.
            pr_number: PR number to inject.
            strategy: Injection strategy to use.

        Returns:
            BenchmarkInstance if injection succeeds, None otherwise.
        """
        logger.info("pipeline_single_start", repo=repo, pr=pr_number, strategy=strategy.value)

        # Clone or update the repository
        repo_url = f"https://github.com/{repo}.git"
        repo_path = await self.workspace.clone_or_update(repo_url, repo)
        base_commit = await self.workspace.get_current_head(repo_path)

        # Fetch the PR
        candidate = await self.miner.fetch_single_pr(repo, pr_number)
        self.stats["total_candidates"] += 1

        # Run through the pipeline
        instance = await self._process_candidate(
            candidate, repo_path, base_commit, strategy
        )

        if instance:
            self.writer.write(instance)
            logger.info(
                "pipeline_single_complete",
                instance_id=instance.instance_id,
                level=instance.injection_level.value,
            )
        else:
            logger.info("pipeline_single_failed", repo=repo, pr=pr_number)

        return instance

    async def run_batch(
        self,
        repo: str,
        since: datetime | None = None,
        require_tests: bool = True,
        verify_blast_radius: bool = True,
        max_candidates: int = 100,
        max_workers: int = 4,
    ) -> AsyncIterator[BenchmarkInstance]:
        """Mine and inject PRs in batch mode.

        Args:
            repo: Repository in "owner/name" format.
            since: Only consider PRs merged after this date.
            require_tests: Skip PRs without test changes.
            verify_blast_radius: Run verification stage.
            max_candidates: Maximum candidates to process.
            max_workers: Number of parallel workers.

        Yields:
            BenchmarkInstance for each successful injection.
        """
        logger.info(
            "pipeline_batch_start",
            repo=repo,
            since=str(since),
            max_candidates=max_candidates,
            max_workers=max_workers,
        )

        # Clone or update the repository
        repo_url = f"https://github.com/{repo}.git"
        repo_path = await self.workspace.clone_or_update(repo_url, repo)
        base_commit = await self.workspace.get_current_head(repo_path)

        # Create a queue for parallel processing
        queue: asyncio.Queue[CandidatePR | None] = asyncio.Queue(maxsize=max_workers * 2)
        results: asyncio.Queue[BenchmarkInstance | None] = asyncio.Queue()

        # Producer: mine candidates
        async def producer() -> None:
            try:
                async for candidate in self.miner.mine(
                    repo, since=since, require_tests=require_tests, max_candidates=max_candidates
                ):
                    await queue.put(candidate)
            finally:
                # Signal workers to stop
                for _ in range(max_workers):
                    await queue.put(None)

        # Worker: process candidates
        async def worker() -> None:
            while True:
                candidate = await queue.get()
                if candidate is None:
                    break

                self.stats["total_candidates"] += 1
                instance = await self._process_candidate(
                    candidate, repo_path, base_commit, InjectionStrategy.AUTO
                )

                if instance:
                    if verify_blast_radius and not (
                        instance.verification and instance.verification.blast_radius_ok
                    ):
                        logger.info(
                            "instance_blast_radius_failed",
                            instance_id=instance.instance_id,
                        )
                        continue

                    self.writer.write(instance)
                    await results.put(instance)

            await results.put(None)

        # Start producer and workers
        producer_task = asyncio.create_task(producer())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(max_workers)]

        # Yield results as they come
        done_workers = 0
        while done_workers < max_workers:
            instance = await results.get()
            if instance is None:
                done_workers += 1
            else:
                yield instance

        # Wait for all tasks to complete
        await producer_task
        await asyncio.gather(*worker_tasks)

        logger.info("pipeline_batch_complete", stats=self.stats)

    async def _process_candidate(
        self,
        candidate: CandidatePR,
        repo_path: str,
        base_commit: str,
        strategy: InjectionStrategy,
    ) -> BenchmarkInstance | None:
        """Process a single candidate through the pipeline stages."""
        pr_num = candidate.metadata.pr_number

        try:
            # Stage 2 + 3: Injection (Reverter → Resolver)
            injection_result = await self._inject(candidate, repo_path, strategy)

            if injection_result is None:
                return None

            level, injected_diff, golden_patch, worktree_path = injection_result

            # Get the original diff for test_patch extraction
            try:
                original_diff = await self.workspace.get_commit_diff(
                    repo_path, candidate.metadata.merge_commit_sha
                )
                test_patch = extract_test_diff(original_diff)
            except Exception:
                test_patch = ""

            # Stage 4: Verification
            verification = None
            try:
                verification = await self.verifier.verify(
                    worktree_path=worktree_path,
                    target_test_files=candidate.metadata.test_files,
                )

                if verification.blast_radius_ok:
                    self.stats["verification_passed"] += 1
                else:
                    self.stats["verification_failed"] += 1

            except Exception as e:
                logger.warning("verification_error", pr=pr_num, error=str(e))
                self.stats["verification_failed"] += 1

            # Build the benchmark instance
            problem_statement = candidate.metadata.title
            if candidate.metadata.body:
                problem_statement += "\n\n" + candidate.metadata.body

            instance = BenchmarkInstance(
                instance_id=f"{candidate.metadata.repo.replace('/', '-')}-pr-{pr_num}",
                repo=candidate.metadata.repo,
                base_commit=base_commit,
                problem_statement=problem_statement,
                injection_level=level,
                golden_patch=golden_patch,
                test_patch=test_patch,
                verification=verification,
            )

            return instance

        except Exception as e:
            logger.error("candidate_processing_error", pr=pr_num, error=str(e))
            self.stats["errors"] += 1
            return None

    async def _inject(
        self,
        candidate: CandidatePR,
        repo_path: str,
        strategy: InjectionStrategy,
    ) -> tuple[InjectionLevel, str, str, str] | None:
        """Run injection stages based on strategy.

        Returns (level, injected_diff, golden_patch, worktree_path) or None.
        """
        pr_num = candidate.metadata.pr_number

        # Try Level 1 + Level 2 via Reverter
        if strategy in (
            InjectionStrategy.AUTO, InjectionStrategy.GIT_ONLY, InjectionStrategy.AST_ONLY
        ):
            try:
                result = await self.reverter.revert(candidate, repo_path)
                if result.level == InjectionLevel.LEVEL_1_CLEAN_REVERT:
                    self.stats["level_1_success"] += 1
                else:
                    self.stats["level_2_success"] += 1
                return (
                    result.level,
                    result.injected_diff,
                    result.golden_patch,
                    result.worktree_path,
                )
            except RevertFailed as e:
                logger.info("revert_failed", pr=pr_num, error=str(e)[:200])
                if strategy in (InjectionStrategy.GIT_ONLY, InjectionStrategy.AST_ONLY):
                    return None

        # Try Level 3 via Resolver
        if strategy in (InjectionStrategy.AUTO, InjectionStrategy.LLM_ONLY):
            try:
                original_diff = await self.workspace.get_commit_diff(
                    repo_path, candidate.metadata.merge_commit_sha
                )
                result = await self.resolver.resolve(
                    candidate, repo_path, original_diff
                )
                self.stats["level_3_success"] += 1
                return (
                    result.level,
                    result.injected_diff,
                    result.golden_patch,
                    result.worktree_path,
                )
            except ArchitectureDeprecated:
                self.stats["level_4_deprecated"] += 1
                logger.info("architecture_deprecated", pr=pr_num)
            except SemanticInjectionFailed as e:
                logger.info("semantic_injection_failed", pr=pr_num, error=str(e)[:200])

        return None

    def get_stats(self) -> dict:
        """Return pipeline execution statistics."""
        return dict(self.stats)
