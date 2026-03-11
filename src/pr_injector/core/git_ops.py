"""Git operations for repository management and revert attempts."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

from git import Repo
from git.exc import GitCommandError

from pr_injector.core.exceptions import GitOperationError
from pr_injector.core.logging import get_logger

logger = get_logger(__name__)


class GitWorkspace:
    """Manages repository clones and worktrees for injection."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._repos_dir = self.base_dir / "repos"
        self._repos_dir.mkdir(exist_ok=True)
        self._worktrees_dir = self.base_dir / "worktrees"
        self._worktrees_dir.mkdir(exist_ok=True)

    def _repo_cache_path(self, repo: str) -> Path:
        """Get the local cache path for a repository."""
        safe_name = repo.replace("/", "__")
        return self._repos_dir / safe_name

    async def clone_or_update(self, repo_url: str, repo_name: str) -> str:
        """Clone repo if not cached, otherwise fetch latest.

        Args:
            repo_url: Full git URL (e.g., https://github.com/pallets/flask.git)
            repo_name: Repository identifier (e.g., pallets/flask)

        Returns:
            Path to the local repository clone.
        """
        cache_path = self._repo_cache_path(repo_name)

        if cache_path.exists():
            logger.info("repo_update", repo=repo_name, path=str(cache_path))
            try:
                repo = Repo(str(cache_path))
                await asyncio.to_thread(repo.remotes.origin.fetch)
                # Reset to latest remote default branch
                default_branch = self._get_default_branch(repo)
                await asyncio.to_thread(
                    repo.git.reset, "--hard", f"origin/{default_branch}"
                )
                return str(cache_path)
            except GitCommandError as e:
                logger.warning("repo_update_failed", repo=repo_name, error=str(e))
                shutil.rmtree(cache_path, ignore_errors=True)

        logger.info("repo_clone", repo=repo_name, url=repo_url)
        try:
            await asyncio.to_thread(
                Repo.clone_from, repo_url, str(cache_path), depth=None
            )
            return str(cache_path)
        except GitCommandError as e:
            raise GitOperationError(f"Failed to clone {repo_url}: {e}") from e

    def _get_default_branch(self, repo: Repo) -> str:
        """Detect the default branch name (main/master/etc.)."""
        try:
            # Try to get from remote HEAD
            remote_refs = repo.remotes.origin.refs
            for ref in remote_refs:
                if ref.remote_head == "HEAD":
                    # Resolve symbolic ref
                    return ref.ref.remote_head
            # Fallback: check common names
            for name in ("main", "master", "develop"):
                if name in [r.remote_head for r in remote_refs]:
                    return name
        except Exception:
            pass
        return "main"

    async def create_worktree(self, repo_path: str, suffix: str = "") -> str:
        """Create isolated worktree for injection.

        Args:
            repo_path: Path to the base repository clone.
            suffix: Optional suffix for the worktree name.

        Returns:
            Path to the new worktree directory.
        """
        worktree_id = (
            f"wt-{suffix}-{uuid.uuid4().hex[:8]}" if suffix else f"wt-{uuid.uuid4().hex[:8]}"
        )
        worktree_path = self._worktrees_dir / worktree_id

        try:
            repo = Repo(repo_path)
            branch_name = f"injection-{worktree_id}"
            await asyncio.to_thread(
                repo.git.worktree, "add", "-b", branch_name, str(worktree_path)
            )
            logger.info("worktree_created", path=str(worktree_path))
            return str(worktree_path)
        except GitCommandError as e:
            raise GitOperationError(f"Failed to create worktree: {e}") from e

    async def remove_worktree(self, worktree_path: str) -> None:
        """Clean up worktree after injection attempt."""
        try:
            # Find the parent repo to properly remove the worktree
            wt_path = Path(worktree_path)
            if wt_path.exists():
                # Get repo associated with the worktree
                repo = Repo(worktree_path)
                main_repo_path = repo.common_dir
                main_repo = Repo(main_repo_path)

                # Remove worktree via git
                await asyncio.to_thread(
                    main_repo.git.worktree, "remove", "--force", str(worktree_path)
                )
                logger.info("worktree_removed", path=worktree_path)
        except Exception as e:
            logger.warning("worktree_remove_failed", path=worktree_path, error=str(e))
            # Fallback: just delete the directory
            shutil.rmtree(worktree_path, ignore_errors=True)

    async def try_revert_commit(
        self, worktree_path: str, commit_sha: str
    ) -> tuple[bool, str]:
        """Attempt git revert --no-commit on a merge commit.

        Args:
            worktree_path: Path to the worktree.
            commit_sha: SHA of the merge commit to revert.

        Returns:
            Tuple of (success, diff_or_error_message).
        """
        try:
            repo = Repo(worktree_path)
            # Try revert with -m 1 for merge commits
            try:
                await asyncio.to_thread(
                    repo.git.revert, "--no-commit", "-m", "1", commit_sha
                )
            except GitCommandError:
                # Might not be a merge commit, try without -m
                await asyncio.to_thread(repo.git.reset, "--hard", "HEAD")
                await asyncio.to_thread(
                    repo.git.revert, "--no-commit", commit_sha
                )

            # Get the diff of what was changed
            diff = await asyncio.to_thread(repo.git.diff, "--cached")
            if not diff.strip():
                return False, "Revert produced empty diff"

            return True, diff

        except GitCommandError as e:
            # Reset the worktree on failure
            try:
                repo = Repo(worktree_path)
                await asyncio.to_thread(repo.git.reset, "--hard", "HEAD")
                await asyncio.to_thread(repo.git.clean, "-fd")
            except Exception:
                pass
            return False, str(e)

    async def get_current_head(self, repo_path: str) -> str:
        """Return SHA of HEAD on default branch."""
        repo = Repo(repo_path)
        return str(repo.head.commit.hexsha)

    async def get_commit_diff(self, repo_path: str, commit_sha: str) -> str:
        """Extract the unified diff introduced by a commit.

        For merge commits, shows the diff against the first parent.
        """
        try:
            repo = Repo(repo_path)
            commit = repo.commit(commit_sha)
            if commit.parents:
                diff = await asyncio.to_thread(
                    repo.git.diff, f"{commit.parents[0].hexsha}..{commit_sha}"
                )
            else:
                diff = await asyncio.to_thread(repo.git.show, "--format=", commit_sha)
            return diff
        except GitCommandError as e:
            raise GitOperationError(f"Failed to get diff for {commit_sha}: {e}") from e

    async def get_changed_files(self, repo_path: str, commit_sha: str) -> list[str]:
        """Get list of files changed by a commit.

        For merge commits, diffs against the first parent.
        """
        try:
            repo = Repo(repo_path)
            commit = repo.commit(commit_sha)
            if commit.parents:
                # For merge commits, diff against first parent
                output = await asyncio.to_thread(
                    repo.git.diff, "--name-only", f"{commit.parents[0].hexsha}..{commit_sha}"
                )
            else:
                output = await asyncio.to_thread(
                    repo.git.diff_tree, "--no-commit-id", "--name-only", "-r", commit_sha
                )
            return [f.strip() for f in output.strip().split("\n") if f.strip()]
        except GitCommandError as e:
            raise GitOperationError(f"Failed to get changed files: {e}") from e

    async def file_exists_at_head(self, repo_path: str, file_path: str) -> bool:
        """Check if a file exists in the current HEAD of the repo."""
        full_path = Path(repo_path) / file_path
        return full_path.exists()

    async def read_file_at_head(self, repo_path: str, file_path: str) -> str | None:
        """Read file content from the current HEAD."""
        full_path = Path(repo_path) / file_path
        if not full_path.exists():
            return None
        return full_path.read_text(encoding="utf-8", errors="replace")

    async def apply_patch(self, worktree_path: str, patch_content: str) -> bool:
        """Apply a unified diff patch to the worktree.

        Returns True if successful.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "apply", "--check", "-",
                cwd=worktree_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate(patch_content.encode())

            if proc.returncode != 0:
                return False

            # Actually apply
            proc = await asyncio.create_subprocess_exec(
                "git", "apply", "-",
                cwd=worktree_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate(patch_content.encode())
            return proc.returncode == 0

        except Exception as e:
            logger.warning("patch_apply_failed", error=str(e))
            return False

    async def cleanup(self) -> None:
        """Remove all worktrees and clean up workspace."""
        if self._worktrees_dir.exists():
            shutil.rmtree(self._worktrees_dir, ignore_errors=True)
            self._worktrees_dir.mkdir(exist_ok=True)
        logger.info("workspace_cleaned", base_dir=str(self.base_dir))
