"""Shared PR-INJECTOR v2 complexity, fidelity, and quota utilities.

The helpers here are intentionally dependency-free so they can be used from
post-processing scripts, construction loops, and lightweight CI checks.
"""

from __future__ import annotations

import json
import gzip
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[3]

TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|testing)/|(^|/)(test_[^/]+\.py|[^/]+_test\.py)$|"
    r"(^|/)(conftest\.py|tox\.ini|noxfile\.py|pytest\.ini|setup\.cfg|pyproject\.toml)$"
)
SYMBOL_RE = re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")


@dataclass(frozen=True)
class PatchProfile:
    files: int
    hunks: int
    added: int
    removed: int
    line_changes: int
    source_files: int
    test_files: int
    python_files: int
    symbols: int
    paths: list[str]
    test_paths: list[str]
    source_paths: list[str]

    def flat(self, prefix: str) -> dict[str, Any]:
        data = asdict(self)
        return {f"{prefix}_{key}": value for key, value in data.items() if not isinstance(value, list)}


@dataclass(frozen=True)
class FidelityGateConfig:
    min_score: float = 0.65
    min_line_ratio: float = 0.50
    min_hunk_ratio: float = 0.50
    min_file_ratio: float = 0.50
    max_line_ratio: float = 2.50
    min_regression_ratio: float = 0.25
    max_repo_share: float = 0.20


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    opener = gzip.open if path.suffix == ".gz" else Path.open
    open_args = {"mode": "rt", "encoding": "utf-8", "errors": "replace"} if path.suffix == ".gz" else {"encoding": "utf-8", "errors": "replace"}
    with opener(path, **open_args) as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def resolve_text(value: Any, *roots: Path) -> str:
    """Return inline text or read a relative/absolute path if it exists."""

    if not isinstance(value, str) or not value:
        return ""
    if value.lstrip().startswith("diff --git "):
        return value
    candidate = Path(value)
    candidates = [candidate]
    if not candidate.is_absolute():
        candidates.extend(root / candidate for root in (ROOT, *roots))
    for path in candidates:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.search(path))


def _path_from_diff_header(line: str) -> str | None:
    # Feature-removal construction stores restore patches from ``git diff -R``.
    # Those patches legitimately use ``b/path a/path`` headers, so do not
    # assume the conventional forward ``a/path b/path`` order.
    match = re.match(r"^diff --git [ab]/(.*?) [ab]/(.*)$", line)
    if not match:
        return None
    return match.group(2)


def patch_profile(diff_text: str) -> PatchProfile:
    paths: list[str] = []
    test_paths: set[str] = set()
    source_paths: set[str] = set()
    symbols: set[str] = set()
    hunks = 0
    added = 0
    removed = 0
    current_path = ""

    for line in diff_text.splitlines():
        path = _path_from_diff_header(line)
        if path is not None:
            current_path = path
            if current_path not in paths:
                paths.append(current_path)
            if is_test_path(current_path):
                test_paths.add(current_path)
            else:
                source_paths.add(current_path)
            continue
        if line.startswith("@@"):
            hunks += 1
            continue
        if not current_path:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
            match = SYMBOL_RE.match(line[1:])
            if match and not is_test_path(current_path):
                symbols.add(f"{current_path}:{match.group(1)}")
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
            match = SYMBOL_RE.match(line[1:])
            if match and not is_test_path(current_path):
                symbols.add(f"{current_path}:{match.group(1)}")

    return PatchProfile(
        files=len(paths),
        hunks=hunks,
        added=added,
        removed=removed,
        line_changes=added + removed,
        source_files=len(source_paths),
        test_files=len(test_paths),
        python_files=sum(1 for path in paths if path.endswith(".py")),
        symbols=len(symbols),
        paths=paths,
        test_paths=sorted(test_paths),
        source_paths=sorted(source_paths),
    )


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 1.0 if numerator <= 0 else float("inf")
    return numerator / denominator


def symmetric_ratio(a: float, b: float) -> float:
    if a <= 0 and b <= 0:
        return 1.0
    if a <= 0 or b <= 0:
        return 0.0
    return min(a, b) / max(a, b)


def complexity_bin(line_changes: int) -> str:
    if line_changes <= 2:
        return "tiny_0_2"
    if line_changes <= 10:
        return "small_3_10"
    if line_changes <= 30:
        return "medium_11_30"
    if line_changes <= 100:
        return "large_31_100"
    return "xlarge_101_plus"


def evaluate_fidelity(
    a_profile: PatchProfile,
    b_profile: PatchProfile,
    a_fail_to_pass_count: int,
    b_fail_to_pass_count: int,
    a_pass_to_pass_count: int,
    b_pass_to_pass_count: int,
    injection_level: str,
    existing_tags: Iterable[str] = (),
    config: FidelityGateConfig | None = None,
) -> dict[str, Any]:
    config = config or FidelityGateConfig()
    line_ratio = _safe_ratio(b_profile.line_changes, a_profile.line_changes)
    hunk_ratio = _safe_ratio(b_profile.hunks, a_profile.hunks)
    file_ratio = _safe_ratio(b_profile.source_files or b_profile.files, a_profile.source_files or a_profile.files)
    target_ratio = _safe_ratio(b_fail_to_pass_count, a_fail_to_pass_count)
    regression_ratio = _safe_ratio(b_pass_to_pass_count, a_pass_to_pass_count)

    parts = {
        "files": symmetric_ratio(a_profile.source_files or a_profile.files, b_profile.source_files or b_profile.files),
        "hunks": symmetric_ratio(a_profile.hunks, b_profile.hunks),
        "line_changes": symmetric_ratio(a_profile.line_changes, b_profile.line_changes),
        "symbols": symmetric_ratio(max(a_profile.symbols, 1), max(b_profile.symbols, 1)),
        "target_tests": symmetric_ratio(max(a_fail_to_pass_count, 1), max(b_fail_to_pass_count, 1)),
        "regression_tests": symmetric_ratio(max(a_pass_to_pass_count, 1), max(b_pass_to_pass_count, 1)),
    }
    weights = {
        "files": 0.18,
        "hunks": 0.20,
        "line_changes": 0.28,
        "symbols": 0.10,
        "target_tests": 0.12,
        "regression_tests": 0.12,
    }
    score = round(sum(parts[key] * weights[key] for key in weights), 4)

    tags = set(existing_tags)
    reasons: list[str] = []
    if line_ratio < config.min_line_ratio:
        tags.add("localized_simplified")
        reasons.append("B line-change complexity is too small relative to A")
    if line_ratio > config.max_line_ratio:
        tags.add("overexpanded")
        reasons.append("B line-change complexity is too large relative to A")
    if hunk_ratio < config.min_hunk_ratio:
        tags.add("hunk_simplified")
        reasons.append("B hunk count is too small relative to A")
    if file_ratio < config.min_file_ratio:
        tags.add("file_scope_simplified")
        reasons.append("B touched-file scope is too small relative to A")
    if regression_ratio < config.min_regression_ratio and a_pass_to_pass_count >= 5:
        tags.add("low_regression_surface")
        reasons.append("B PASS_TO_PASS surface is too small relative to A")
    if a_profile.line_changes >= 30 and b_profile.line_changes <= 10:
        tags.add("hard_to_easy_collapse")
        reasons.append("A is medium/large while B collapsed to a small patch")
    if injection_level.startswith("Level_2") and "localized_simplified" in tags:
        tags.add("level2_simplification_risk")
    if injection_level.startswith("Level_3") and score < config.min_score:
        tags.add("l3_needs_feedback_loop")

    disqualifying_tags = {
        "localized_simplified",
        "overexpanded",
        "hunk_simplified",
        "hard_to_easy_collapse",
        "file_scope_simplified",
        "low_regression_surface",
    }
    if tags.intersection(disqualifying_tags | {"api_drift", "level2_simplification_risk", "l3_needs_feedback_loop"}):
        tags.discard("faithful")
    pass_gate = score >= config.min_score and not disqualifying_tags.intersection(tags)
    if pass_gate and not tags:
        tags.add("faithful_complexity_match")

    return {
        "score": score,
        "pass_gate": pass_gate,
        "ratios": {
            "line_changes": round(line_ratio, 4) if math.isfinite(line_ratio) else "inf",
            "hunks": round(hunk_ratio, 4) if math.isfinite(hunk_ratio) else "inf",
            "files": round(file_ratio, 4) if math.isfinite(file_ratio) else "inf",
            "target_tests": round(target_ratio, 4) if math.isfinite(target_ratio) else "inf",
            "regression_tests": round(regression_ratio, 4) if math.isfinite(regression_ratio) else "inf",
        },
        "parts": parts,
        "tags": sorted(tags),
        "reasons": reasons,
        "config": asdict(config),
    }


def count_items(value: Any) -> int:
    """Count list-like benchmark fields without treating a string as characters."""

    if value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return 0
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return 1
        return count_items(parsed)
    return 0


def evaluate_patch_pair_fidelity(
    *,
    a_patch: str,
    b_patch: str,
    a_fail_to_pass: Any = None,
    b_fail_to_pass: Any = None,
    a_pass_to_pass: Any = None,
    b_pass_to_pass: Any = None,
    injection_level: str = "",
    existing_tags: Iterable[str] = (),
    config: FidelityGateConfig | None = None,
) -> dict[str, Any]:
    """Evaluate the v2 fidelity gate directly from A/B patch texts."""

    a_profile = patch_profile(a_patch or "")
    b_profile = patch_profile(b_patch or "")
    gate = evaluate_fidelity(
        a_profile,
        b_profile,
        count_items(a_fail_to_pass),
        count_items(b_fail_to_pass),
        count_items(a_pass_to_pass),
        count_items(b_pass_to_pass),
        injection_level,
        existing_tags=existing_tags,
        config=config,
    )
    gate["A_profile"] = asdict(a_profile)
    gate["B_profile"] = asdict(b_profile)
    gate["A_FAIL_TO_PASS_count"] = count_items(a_fail_to_pass)
    gate["B_FAIL_TO_PASS_count"] = count_items(b_fail_to_pass)
    gate["A_PASS_TO_PASS_count"] = count_items(a_pass_to_pass)
    gate["B_PASS_TO_PASS_count"] = count_items(b_pass_to_pass)
    return gate


def build_fidelity_feedback_prompt(gate: dict[str, Any]) -> str:
    """Turn a failed v2 gate result into concise L3 retry feedback."""

    a_profile = gate.get("A_profile") or {}
    b_profile = gate.get("B_profile") or {}
    tags = set(gate.get("tags") or [])
    ratios = gate.get("ratios") or {}
    parts = [
        "Preserve the historical bug semantics while matching the original A-side patch footprint.",
        (
            f"A patch target: files={a_profile.get('source_files') or a_profile.get('files')}, "
            f"hunks={a_profile.get('hunks')}, line_changes={a_profile.get('line_changes')}, "
            f"symbols={a_profile.get('symbols')}, "
            f"FAIL_TO_PASS={gate.get('A_FAIL_TO_PASS_count')}, "
            f"PASS_TO_PASS={gate.get('A_PASS_TO_PASS_count')}."
        ),
        (
            f"Current B attempt: files={b_profile.get('source_files') or b_profile.get('files')}, "
            f"hunks={b_profile.get('hunks')}, line_changes={b_profile.get('line_changes')}, "
            f"symbols={b_profile.get('symbols')}, "
            f"FAIL_TO_PASS={gate.get('B_FAIL_TO_PASS_count')}, "
            f"PASS_TO_PASS={gate.get('B_PASS_TO_PASS_count')}, "
            f"score={gate.get('score')}, ratios={ratios}."
        ),
    ]
    if "localized_simplified" in tags or "hard_to_easy_collapse" in tags:
        parts.append(
            "The B bug is too localized. Increase the behavioral footprint without adding unrelated noise."
        )
    if "hunk_simplified" in tags:
        parts.append("The B bug uses too few hunks. Preserve the original multi-hunk structure where valid.")
    if "file_scope_simplified" in tags:
        parts.append("The B bug touches too few source files. Preserve cross-file API or call-site effects.")
    if "overexpanded" in tags:
        parts.append("The B bug is too broad. Remove unrelated edits while keeping the same semantic defect.")
    if "low_regression_surface" in tags:
        parts.append("The B task needs a broader adjacent/PASS_TO_PASS surface before it is accepted.")
    if "level2_simplification_risk" in tags:
        parts.append("Avoid whole-function transplantation that collapses the historical fix into a local edit.")
    if "l3_needs_feedback_loop" in tags:
        parts.append("Retry L3 with explicit complexity constraints until the v2 fidelity score passes.")
    reasons = gate.get("reasons") or []
    if reasons:
        parts.append("Gate reasons: " + "; ".join(str(reason) for reason in reasons) + ".")
    return " ".join(parts)


def summarize_counter(counter: Counter[str], top: int | None = None) -> dict[str, int]:
    items = counter.most_common(top)
    return {str(key): int(value) for key, value in items}
