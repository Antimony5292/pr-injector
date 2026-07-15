"""Feature-addition fidelity metrics shared by construction and auditing."""

from __future__ import annotations

import re
from dataclasses import asdict, replace
from pathlib import PurePosixPath
from typing import Any

try:
    from prinjector_v2_metrics import FidelityGateConfig, evaluate_fidelity, patch_profile
except ImportError:
    from .prinjector_v2_metrics import FidelityGateConfig, evaluate_fidelity, patch_profile


IMPLEMENTATION_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hpp",
    ".py",
    ".pyi",
    ".pyx",
    ".pxd",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
}


def diff_header_path(line: str) -> str | None:
    match = re.match(r"^diff --git [ab]/(.*?) [ab]/(.*)$", line)
    return match.group(2) if match else None


def is_implementation_path(path: str) -> bool:
    lowered = path.lower()
    parts = PurePosixPath(lowered).parts
    name = parts[-1] if parts else lowered
    if "test" in parts or "tests" in parts or "testing" in parts:
        return False
    if "docs" in parts or "doc" in parts or "examples" in parts:
        return False
    if name.startswith("test_") or name.endswith(("_test.py", ".spec.ts", ".test.ts")):
        return False
    return PurePosixPath(lowered).suffix in IMPLEMENTATION_SUFFIXES


def implementation_diff(diff: str) -> str:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    kept = [block for block in blocks if is_implementation_path(diff_header_path(block[0]) or "")]
    return "\n".join("\n".join(block) for block in kept) + ("\n" if kept else "")


def _fold_python_stub_units(profile):
    """Treat a split .py/.pyi pair as one implementation unit.

    Modern Python projects commonly inline annotations that were historically
    maintained in a sibling stub. The raw profiles remain in the audit output;
    only touched-file fidelity uses this architecture-normalized unit count.
    """
    units = {
        path[:-1] if path.endswith(".pyi") else path
        for path in profile.source_paths
    }
    effective_files = len(units)
    if effective_files == profile.source_files:
        return profile
    return replace(profile, files=effective_files, source_files=effective_files)


def feature_fidelity(
    source_feature_patch: str,
    modern_restore_patch: str,
    *,
    source_target_tests: int,
    modern_target_tests: int,
    source_regression_tests: int,
    modern_regression_tests: int,
    config: FidelityGateConfig | None = None,
) -> dict[str, Any]:
    source_profile = patch_profile(implementation_diff(source_feature_patch))
    modern_profile = patch_profile(implementation_diff(modern_restore_patch))
    effective_source_profile = _fold_python_stub_units(source_profile)
    effective_modern_profile = _fold_python_stub_units(modern_profile)
    gate = evaluate_fidelity(
        effective_source_profile,
        effective_modern_profile,
        source_target_tests,
        modern_target_tests,
        source_regression_tests,
        modern_regression_tests,
        "Feature_Semantic_Reconstruction",
        config=config,
    )
    gate["passed"] = bool(gate.get("pass_gate"))
    gate["profile_scope"] = "implementation_code_only"
    gate["source_profile"] = asdict(source_profile)
    gate["modern_profile"] = asdict(modern_profile)
    gate["architecture_normalization"] = {
        "python_stub_pairs_folded": True,
        "source_effective_files": effective_source_profile.source_files,
        "modern_effective_files": effective_modern_profile.source_files,
    }
    return gate


def feature_fidelity_feedback(gate: dict[str, Any]) -> str:
    a = gate.get("source_profile") or {}
    b = gate.get("modern_profile") or {}
    reasons = "; ".join(str(reason) for reason in gate.get("reasons") or [])
    return (
        "feature fidelity gate failed. "
        f"Reasons: {reasons or 'score below threshold'}. "
        f"Source implementation profile: files={a.get('source_files')}, hunks={a.get('hunks')}, "
        f"line_changes={a.get('line_changes')}, symbols={a.get('symbols')}. "
        f"Modern attempt profile: files={b.get('source_files')}, hunks={b.get('hunks')}, "
        f"line_changes={b.get('line_changes')}, symbols={b.get('symbols')}. "
        "Preserve the complete behavior contract and call-chain impact; do not reduce a multi-step feature "
        "to a local stub or remove unrelated code to inflate the patch."
    )
