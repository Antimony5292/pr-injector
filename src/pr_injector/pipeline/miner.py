"""Stage 1: PR candidate discovery and filtering via GitHub API."""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx

from pr_injector.core.diff_parser import is_test_file
from pr_injector.core.exceptions import MinerError
from pr_injector.core.logging import get_logger
from pr_injector.core.models import CandidatePR, PRMetadata

logger = get_logger(__name__)

# Default test file patterns for filtering
DEFAULT_TEST_PATTERNS = ["test_", "_test.", ".test.", ".spec.", "tests/", "test/", "__tests__/"]


class PRMiner:
    """Stage 1: Discover and filter candidate PRs from GitHub.

    Fetches merged PRs from a repository, applies filtering heuristics
    (time decay, change frequency, test file existence), and yields
    candidates for injection.
    """

    def __init__(
        self,
        github_token: str,
        api_base: str = "https://api.github.com",
        max_concurrent: int = 10,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.headers = {
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if github_token:
            self.headers["Authorization"] = f"Bearer {github_token}"
        self.max_concurrent = max_concurrent

    async def mine(
        self,
        repo: str,
        since: datetime | None = None,
        require_tests: bool = True,
        max_candidates: int = 100,
        max_patch_size: int = 5000,
    ) -> AsyncIterator[CandidatePR]:
        """Discover and yield candidate PRs for injection.

        Args:
            repo: Repository in "owner/name" format.
            since: Only consider PRs merged after this date.
            require_tests: If True, skip PRs without test changes.
            max_candidates: Maximum number of candidates to yield.
            max_patch_size: Maximum patch size (lines changed) to accept.

        Yields:
            CandidatePR instances that pass all filters.
        """
        logger.info("mining_start", repo=repo, since=str(since))
        yielded = 0

        async with httpx.AsyncClient(
            headers=self.headers, timeout=30.0
        ) as client:
            page = 1
            while yielded < max_candidates:
                prs = await self._fetch_merged_prs(client, repo, page=page)
                if not prs:
                    break

                for pr_data in prs:
                    if yielded >= max_candidates:
                        break

                    merged_at = pr_data.get("merged_at")
                    if not merged_at:
                        continue

                    merge_time = datetime.fromisoformat(
                        merged_at.replace("Z", "+00:00")
                    )

                    # Apply time filter
                    if since:
                        since_utc = (
                            since.astimezone(timezone.utc)
                            if since.tzinfo
                            else since.replace(tzinfo=timezone.utc)
                        )
                        if merge_time < since_utc:
                            continue

                    # Fetch PR details (files changed)
                    try:
                        metadata = await self._build_metadata(
                            client, repo, pr_data
                        )
                    except Exception as e:
                        logger.warning(
                            "pr_metadata_failed",
                            pr=pr_data.get("number"),
                            error=str(e),
                        )
                        continue

                    # Filter: must have test files
                    if require_tests and not metadata.test_files:
                        logger.debug(
                            "pr_skipped_no_tests", pr=metadata.pr_number
                        )
                        continue

                    # Filter: patch size
                    total_changes = metadata.additions + metadata.deletions
                    if total_changes > max_patch_size:
                        logger.debug(
                            "pr_skipped_too_large",
                            pr=metadata.pr_number,
                            changes=total_changes,
                        )
                        continue

                    # Filter: skip revert PRs
                    if self._is_revert_pr(metadata):
                        logger.debug(
                            "pr_skipped_is_revert", pr=metadata.pr_number
                        )
                        continue

                    # Compute scores
                    time_decay_score = self._compute_time_decay(merge_time)

                    candidate = CandidatePR(
                        metadata=metadata,
                        time_decay_score=time_decay_score,
                        change_frequency_score=0.5,  # Placeholder
                        test_files_exist=True,  # Will be verified by Reverter
                    )

                    logger.info(
                        "candidate_found",
                        pr=metadata.pr_number,
                        title=metadata.title[:60],
                        time_decay=round(time_decay_score, 3),
                    )

                    yield candidate
                    yielded += 1

                page += 1

        logger.info("mining_complete", repo=repo, candidates=yielded)

    async def fetch_single_pr(
        self, repo: str, pr_number: int
    ) -> CandidatePR:
        """Fetch a single PR and return as a candidate.

        Args:
            repo: Repository in "owner/name" format.
            pr_number: PR number.

        Returns:
            CandidatePR for the specified PR.

        Raises:
            MinerError: If PR cannot be fetched or is not merged.
        """
        async with httpx.AsyncClient(
            headers=self.headers, timeout=30.0
        ) as client:
            url = f"{self.api_base}/repos/{repo}/pulls/{pr_number}"
            response = await client.get(url)

            if response.status_code != 200:
                raise MinerError(
                    f"Failed to fetch PR #{pr_number}: HTTP {response.status_code}"
                )

            pr_data = response.json()

            if not pr_data.get("merged_at"):
                raise MinerError(f"PR #{pr_number} is not merged")

            metadata = await self._build_metadata(client, repo, pr_data)

            merge_time = datetime.fromisoformat(
                pr_data["merged_at"].replace("Z", "+00:00")
            )

            return CandidatePR(
                metadata=metadata,
                time_decay_score=self._compute_time_decay(merge_time),
                change_frequency_score=0.5,
                test_files_exist=True,
            )

    async def _fetch_merged_prs(
        self,
        client: httpx.AsyncClient,
        repo: str,
        page: int = 1,
        per_page: int = 30,
    ) -> list[dict]:
        """Fetch a page of merged PRs from the GitHub API."""
        url = f"{self.api_base}/repos/{repo}/pulls"
        params = {
            "state": "closed",
            "sort": "updated",
            "direction": "desc",
            "per_page": per_page,
            "page": page,
        }

        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise MinerError(f"GitHub API error: {e}") from e

        # Filter to only merged PRs
        prs = response.json()
        return [pr for pr in prs if pr.get("merged_at") is not None]

    async def _build_metadata(
        self,
        client: httpx.AsyncClient,
        repo: str,
        pr_data: dict,
    ) -> PRMetadata:
        """Build PRMetadata from GitHub API response."""
        pr_number = pr_data["number"]

        # Fetch files changed
        files_url = f"{self.api_base}/repos/{repo}/pulls/{pr_number}/files"
        files_response = await client.get(files_url, params={"per_page": 100})
        files_data = files_response.json() if files_response.status_code == 200 else []

        changed_files = [f["filename"] for f in files_data if isinstance(f, dict)]
        test_files = [f for f in changed_files if is_test_file(f)]

        additions = sum(f.get("additions", 0) for f in files_data if isinstance(f, dict))
        deletions = sum(f.get("deletions", 0) for f in files_data if isinstance(f, dict))

        return PRMetadata(
            repo=repo,
            pr_number=pr_number,
            title=pr_data.get("title", ""),
            body=pr_data.get("body"),
            merge_commit_sha=pr_data.get("merge_commit_sha", ""),
            base_sha=pr_data.get("base", {}).get("sha", ""),
            head_sha=pr_data.get("head", {}).get("sha", ""),
            merged_at=datetime.fromisoformat(
                pr_data["merged_at"].replace("Z", "+00:00")
            ),
            diff_url=pr_data.get("diff_url", ""),
            changed_files=changed_files,
            test_files=test_files,
            additions=additions,
            deletions=deletions,
        )

    @staticmethod
    def _compute_time_decay(merge_time: datetime) -> float:
        """Compute time decay score using exponential decay.

        More recent PRs get higher scores. Uses a half-life of 180 days.
        """
        now = datetime.now(timezone.utc)
        days_ago = (now - merge_time.replace(tzinfo=timezone.utc)).days
        half_life = 180  # days
        return math.exp(-0.693 * days_ago / half_life)

    @staticmethod
    def _is_revert_pr(metadata: PRMetadata) -> bool:
        """Detect if a PR is itself a revert (should be skipped)."""
        title_lower = metadata.title.lower()
        return title_lower.startswith("revert") or 'revert "' in title_lower
