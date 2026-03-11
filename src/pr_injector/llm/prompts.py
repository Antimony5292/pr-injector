"""Prompt templates for LLM-based semantic injection."""

from __future__ import annotations

SEMANTIC_INJECTION_SYSTEM = (
    "You are an expert software engineer tasked with recreating a historical bug in a modern"
    " codebase. Your goal is to precisely reintroduce the same logical defect that was originally"
    " fixed, adapted to the current code structure.\n\nRules:\n1. Only modify the specific"
    " functions/methods that correspond to the original bug fix.\n2. The bug must be a LOGICAL"
    " defect, not a syntax error.\n3. Output ONLY a valid unified diff (no explanations, no"
    " markdown).\n4. The diff must apply cleanly to the current source files.\n5. Do NOT introduce"
    " any new imports or dependencies.\n6. Preserve all existing functionality EXCEPT for the"
    " specific bug being reintroduced."
)

SEMANTIC_INJECTION_USER = """## Original Bug Context

### Issue Description
{issue_description}

### Original Fix (PR Diff)
```diff
{original_diff}
```

## Current Codebase (Latest Version)

{current_files_section}

## Task

The original PR fixed a bug described above. The codebase has since evolved.
Your task: Create a unified diff that reintroduces the SAME logical bug into the CURRENT code.

The bug should:
- Cause the same category of failure as the original
- Be in the equivalent code location (which may have moved or been refactored)
- Be subtle enough that it's not immediately obvious

Output ONLY the unified diff, starting with "diff --git"."""


def build_semantic_injection_prompt(
    issue_description: str,
    original_diff: str,
    current_files: dict[str, str],
    target_functions: list[str] | None = None,
) -> tuple[str, str]:
    """Build the system and user prompts for semantic injection.

    Args:
        issue_description: Original issue/PR description.
        original_diff: The original fix diff.
        current_files: Dict of {file_path: file_content} for relevant files.
        target_functions: Optional list of function names to focus on.

    Returns:
        Tuple of (system_prompt, user_prompt).
    """
    files_section_parts: list[str] = []
    for path, content in current_files.items():
        # Truncate very large files
        if len(content) > 10000:
            content = content[:10000] + "\n... (truncated)"
        files_section_parts.append(f"### {path}\n```\n{content}\n```")

    current_files_section = "\n\n".join(files_section_parts)

    if target_functions:
        current_files_section += (
            f"\n\n### Target Functions\nFocus on: {', '.join(target_functions)}"
        )

    user_prompt = SEMANTIC_INJECTION_USER.format(
        issue_description=issue_description or "(No description available)",
        original_diff=original_diff,
        current_files_section=current_files_section,
    )

    return SEMANTIC_INJECTION_SYSTEM, user_prompt
