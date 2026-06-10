"""Conservative hunk-level reverse surgery.

This module implements a narrow first-pass strategy for Level 2 injection:
when the historical fixed lines still exist in the modern file, replace only
those fixed lines with the historical buggy lines. Keeping the edit at hunk
granularity avoids downgrading an entire modern function body to an old API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from unidiff import PatchSet


@dataclass(frozen=True)
class HunkReplacement:
    """One hunk-level replacement applied to a source file."""

    file_path: str
    strategy: str
    removed_lines: int
    added_lines: int
    context: str = ""


@dataclass
class HunkSurgeryResult:
    """Result of applying hunk-level reverse surgery."""

    content: str
    replacements: list[HunkReplacement] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.replacements)

    def to_metadata(self) -> list[dict]:
        return [
            {
                "file_path": replacement.file_path,
                "strategy": replacement.strategy,
                "removed_lines": replacement.removed_lines,
                "added_lines": replacement.added_lines,
                "context": replacement.context,
            }
            for replacement in self.replacements
        ]


def reverse_patch_hunks_for_file(
    file_path: str,
    current_content: str,
    fix_patch: str,
) -> HunkSurgeryResult:
    """Apply exact hunk-level reverse replacements for one file.

    The input patch is the historical fix. For each hunk, this function looks
    for the fixed added lines in the current modern file and replaces them with
    the historical removed lines, preserving the current function wrapper and
    unrelated modern code.
    """

    result = HunkSurgeryResult(content=current_content)
    try:
        patch_set = PatchSet(fix_patch)
    except Exception:
        return result

    modified = current_content
    for patched_file in patch_set:
        if patched_file.path != file_path:
            continue
        for hunk in patched_file:
            for removed, added in _edit_groups(hunk):
                if not removed or not added:
                    continue
                if _is_import_only_edit(removed, added):
                    continue

                before = _join_patch_lines(added)
                after = _join_patch_lines(removed)
                if not before.strip() or before == after:
                    continue

                updated = _replace_exact_once(modified, before, after)
                strategy = "exact_added_block"
                if updated is None:
                    updated = _replace_trimmed_once(modified, before, after)
                    strategy = "trimmed_added_block"
                if updated is None:
                    continue

                modified = updated
                result.replacements.append(
                    HunkReplacement(
                        file_path=file_path,
                        strategy=strategy,
                        removed_lines=len(removed),
                        added_lines=len(added),
                        context=(hunk.section_header or "")[:120],
                    )
                )

    result.content = modified
    return result


def old_changed_line_ranges_for_file(file_path: str, fix_patch: str) -> list[tuple[int, int]]:
    """Return old-side line ranges touched by source removals in a fix patch."""

    try:
        patch_set = PatchSet(fix_patch)
    except Exception:
        return []

    ranges: list[tuple[int, int]] = []
    for patched_file in patch_set:
        if patched_file.path != file_path:
            continue
        for hunk in patched_file:
            old_line = hunk.source_start
            touched: list[int] = []
            for line in hunk:
                if line.is_removed:
                    touched.append(old_line)
                    old_line += 1
                elif line.is_context:
                    old_line += 1
            if touched:
                ranges.append((min(touched), max(touched)))
            elif hunk.source_length:
                ranges.append((hunk.source_start, hunk.source_start + hunk.source_length - 1))
    return ranges


def overlaps_any_range(start_line: int, end_line: int, ranges: list[tuple[int, int]]) -> bool:
    """Return whether a line span overlaps any range."""

    return any(
        start_line <= range_end and end_line >= range_start
        for range_start, range_end in ranges
    )


def _join_patch_lines(lines: list[str]) -> str:
    text = "".join(lines)
    if lines and not text.endswith("\n") and all(line.endswith("\n") for line in lines[:-1]):
        return text
    return text


def _edit_groups(hunk) -> list[tuple[list[str], list[str]]]:
    groups: list[tuple[list[str], list[str]]] = []
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        nonlocal removed, added
        if removed or added:
            groups.append((removed, added))
            removed = []
            added = []

    for line in hunk:
        if line.is_removed:
            removed.append(line.value)
        elif line.is_added:
            added.append(line.value)
        else:
            flush()
    flush()
    return groups


def _is_import_only_edit(removed: list[str], added: list[str]) -> bool:
    changed = [line.strip() for line in [*removed, *added] if line.strip()]
    if not changed:
        return False
    return all(
        line.startswith("import ")
        or line.startswith("from ")
        or line.startswith(")")
        or line.endswith("(")
        or line.endswith(",")
        for line in changed
    )


def _replace_exact_once(source: str, before: str, after: str) -> str | None:
    source_lines = source.splitlines(keepends=True)
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    if not before_lines:
        return None
    matches: list[tuple[int, int]] = []
    for start in range(0, len(source_lines) - len(before_lines) + 1):
        end = start + len(before_lines)
        if source_lines[start:end] == before_lines:
            matches.append((start, end))
    if len(matches) != 1:
        return None
    start, end = matches[0]
    return "".join(source_lines[:start] + after_lines + source_lines[end:])


def _replace_trimmed_once(source: str, before: str, after: str) -> str | None:
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if not before_lines or not after_lines:
        return None

    before_indent = _common_indent(before_lines)
    stripped_before = "\n".join(line[before_indent:] for line in before_lines)
    if not stripped_before.strip():
        return None

    source_lines = source.splitlines(keepends=True)
    needle_lines = stripped_before.splitlines()
    matches: list[tuple[int, int, int]] = []
    for start in range(0, len(source_lines) - len(needle_lines) + 1):
        window = source_lines[start : start + len(needle_lines)]
        window_no_newline = [line.rstrip("\n") for line in window]
        window_indent = _common_indent(window_no_newline)
        stripped_window = [
            line[window_indent:] if len(line) >= window_indent else line
            for line in window_no_newline
        ]
        if stripped_window == needle_lines:
            matches.append((start, start + len(needle_lines), window_indent))

    if len(matches) != 1:
        return None

    start, end, target_indent = matches[0]
    replacement = _reindent_lines(after_lines, before_indent, target_indent)
    if source_lines[end - 1].endswith("\n"):
        replacement = [line + "\n" for line in replacement]
    new_lines = source_lines[:start] + replacement + source_lines[end:]
    return "".join(new_lines)


def _common_indent(lines: list[str]) -> int:
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    return min(indents) if indents else 0


def _reindent_lines(lines: list[str], source_indent: int, target_indent: int) -> list[str]:
    reindented = []
    prefix = " " * target_indent
    for line in lines:
        if line.strip():
            body = line[source_indent:] if len(line) >= source_indent else line.lstrip(" ")
            reindented.append(prefix + body)
        else:
            reindented.append("")
    return reindented
