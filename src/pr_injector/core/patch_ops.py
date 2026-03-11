"""Patch application and conflict detection utilities."""

from __future__ import annotations

import asyncio

from pr_injector.core.logging import get_logger

logger = get_logger(__name__)


async def apply_reverse_patch(
    worktree_path: str, patch_content: str
) -> tuple[bool, str]:
    """Apply a patch in reverse mode using git apply.

    Args:
        worktree_path: Path to the git worktree.
        patch_content: Unified diff content to apply in reverse.

    Returns:
        Tuple of (success, diff_or_error).
    """
    # First check if patch can be applied
    proc = await asyncio.create_subprocess_exec(
        "git", "apply", "--reverse", "--check", "-",
        cwd=worktree_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(patch_content.encode())

    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")

    # Apply the patch
    proc = await asyncio.create_subprocess_exec(
        "git", "apply", "--reverse", "-",
        cwd=worktree_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(patch_content.encode())

    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")

    # Get the resulting diff
    proc = await asyncio.create_subprocess_exec(
        "git", "diff",
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    diff = stdout.decode(errors="replace")

    return True, diff


async def check_patch_applicability(
    worktree_path: str, patch_content: str, reverse: bool = False
) -> bool:
    """Check if a patch can be cleanly applied.

    Args:
        worktree_path: Path to the git worktree.
        patch_content: Unified diff content.
        reverse: If True, check reverse application.

    Returns:
        True if patch can be applied cleanly.
    """
    cmd = ["git", "apply", "--check", "-"]
    if reverse:
        cmd.insert(2, "--reverse")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worktree_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(patch_content.encode())
    return proc.returncode == 0


async def get_worktree_diff(worktree_path: str) -> str:
    """Get the current unstaged diff in a worktree."""
    proc = await asyncio.create_subprocess_exec(
        "git", "diff",
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode(errors="replace")


async def reset_worktree(worktree_path: str) -> None:
    """Reset a worktree to a clean state."""
    proc = await asyncio.create_subprocess_exec(
        "git", "checkout", "--", ".",
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "git", "clean", "-fd",
        cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
