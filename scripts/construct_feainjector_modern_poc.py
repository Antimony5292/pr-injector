"""Construct modern feature-missing POC tasks for FEA-INJECTOR.

This script is intentionally conservative. It creates per-case modern worktrees,
then applies semantic feature-removal handlers only for cases where we have a
reviewed modern-code mapping. Unsupported cases are recorded as pending instead
of guessed.
"""

from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from prinjector_v2_metrics import read_jsonl, write_jsonl
except ImportError:
    from scripts.prinjector_v2_metrics import read_jsonl, write_jsonl


ROOT = Path(__file__).resolve().parent.parent
REPO_CACHE = ROOT / ".pri-workspace" / "repos"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def repo_cache_path(repo: str) -> Path:
    return REPO_CACHE / repo.replace("/", "__")


def ensure_worktree(row: dict[str, Any], output_dir: Path) -> Path:
    instance_id = str(row["instance_id"])
    worktree = (output_dir / "worktrees" / instance_id).resolve()
    if worktree.exists():
        return worktree
    source = repo_cache_path(str(row["repo"])).resolve()
    if not source.exists():
        raise FileNotFoundError(f"missing cached repo for {row['repo']}: {source}")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--shared", str(source), str(worktree)])
    return worktree


def unified_diff(original: str, modified: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def remove_block(text: str, start: str, end_before: str) -> tuple[str, bool]:
    start_idx = text.find(start)
    if start_idx < 0:
        return text, False
    end_idx = text.find(end_before, start_idx)
    if end_idx < 0:
        return text, False
    return text[:start_idx] + text[end_idx:], True


def write_feature_patches_for_paths(
    row: dict[str, Any],
    worktree: Path,
    output_dir: Path,
    rels: list[Path],
) -> dict[str, str]:
    rel_args = [str(rel) for rel in rels]
    diff_text = run(["git", "diff", "--", *rel_args], cwd=worktree).stdout
    reverse_gold = run(["git", "diff", "-R", "--", *rel_args], cwd=worktree).stdout

    patch_dir = (output_dir / "modern_feature_patches").resolve()
    patch_dir.mkdir(parents=True, exist_ok=True)
    missing_patch_path = patch_dir / f"{row['instance_id']}.feature_missing.diff"
    gold_patch_path = patch_dir / f"{row['instance_id']}.gold_feature_restore.diff"
    missing_patch_path.write_text(diff_text, encoding="utf-8")
    gold_patch_path.write_text(reverse_gold, encoding="utf-8")
    return {
        "feature_missing_patch": str(missing_patch_path.relative_to(ROOT)),
        "gold_feature_restore_patch": str(gold_patch_path.relative_to(ROOT)),
    }


def write_feature_patches(row: dict[str, Any], worktree: Path, output_dir: Path, rel: Path) -> dict[str, str]:
    return write_feature_patches_for_paths(row, worktree, output_dir, [rel])


def constructed_result(
    row: dict[str, Any],
    worktree: Path,
    patch_paths: dict[str, str],
    strategy: str,
) -> dict[str, Any]:
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "status": "constructed_feature_missing",
        "strategy": strategy,
        "worktree": str(worktree.relative_to(ROOT)),
        "modern_head": run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip(),
        "feature_missing_patch": patch_paths["feature_missing_patch"],
        "gold_feature_restore_patch": patch_paths["gold_feature_restore_patch"],
        "feature_tests": row.get("FAIL_TO_PASS") or [],
        "pass_to_pass": row.get("PASS_TO_PASS") or [],
        "verification_status": "not_run",
        "verification_blocker": "repo-specific test environment not yet available",
    }


def construct_pylint_enable_all_extensions(row: dict[str, Any], worktree: Path, output_dir: Path) -> dict[str, Any]:
    rel = Path("pylint/config/utils.py")
    path = worktree / rel
    original = path.read_text(encoding="utf-8")
    if "--enable-all-extensions" in original:
        modified, removed_function = remove_block(
            original,
            "\ndef _enable_all_extensions(run: Run, value: str | None) -> None:\n",
            "\n\nPREPROCESSABLE_OPTIONS:",
        )
        modified = modified.replace(
            '    "--enable-all-extensions": (False, _enable_all_extensions, 9),\n',
            "",
        )
        removed_option = modified != original and "--enable-all-extensions" not in modified
        if not removed_function or not removed_option:
            return {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "handler_failed",
                "reason": "expected modern feature symbols were not found",
            }
        path.write_text(modified, encoding="utf-8")
    patch_paths = write_feature_patches(row, worktree, output_dir, rel)
    return constructed_result(row, worktree, patch_paths, "semantic_remove_modern_feature")


def construct_astropy_time_mean(row: dict[str, Any], worktree: Path, output_dir: Path) -> dict[str, Any]:
    rel = Path("astropy/time/core.py")
    path = worktree / rel
    original = path.read_text(encoding="utf-8")
    modified = original
    if "    def mean(self, axis=None, dtype=None, out=None, keepdims=False, *, where=True):\n" in original:
        modified, removed_base_mean = remove_block(
            modified,
            "    def mean(self, axis=None, dtype=None, out=None, keepdims=False, *, where=True):\n"
            "        \"\"\"Mean along a given axis.\n",
            "\n    @lazyproperty\n",
        )
        modified, removed_time_mean = remove_block(
            modified,
            "    def mean(self, axis=None, dtype=None, out=None, keepdims=False, *, where=True):\n"
            "        scale = self.scale\n",
            "\n    def __array_function__",
        )
        if not removed_base_mean or not removed_time_mean:
            return {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "handler_failed",
                "reason": "expected TimeBase.mean and Time.mean blocks were not both found",
            }
        path.write_text(modified, encoding="utf-8")
    diff_text = run(["git", "diff", "--", str(rel)], cwd=worktree).stdout
    if not diff_text.strip():
        return {
            "instance_id": row["instance_id"],
            "repo": row["repo"],
            "status": "handler_failed",
            "reason": "feature symbols already absent and no feature-missing diff exists",
        }
    patch_paths = write_feature_patches(row, worktree, output_dir, rel)
    return constructed_result(row, worktree, patch_paths, "semantic_remove_time_mean_feature")


def construct_astropy_row_get(row: dict[str, Any], worktree: Path, output_dir: Path) -> dict[str, Any]:
    rel = Path("astropy/table/row.py")
    path = worktree / rel
    original = path.read_text(encoding="utf-8")
    modified, removed_get = remove_block(
        original,
        "    def get(self, key, default=None, /):\n"
        "        \"\"\"Return the value for key if key is in the columns, else default.\n",
        "\n    def keys(self):\n",
    )
    if not removed_get:
        diff_text = run(["git", "diff", "--", str(rel)], cwd=worktree).stdout
        if not diff_text.strip():
            return {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "handler_failed",
                "reason": "expected Row.get block was not found",
            }
    else:
        path.write_text(modified, encoding="utf-8")
    patch_paths = write_feature_patches(row, worktree, output_dir, rel)
    return constructed_result(row, worktree, patch_paths, "semantic_remove_row_get_feature")


def construct_xarray_backend_ordering(row: dict[str, Any], worktree: Path, output_dir: Path) -> dict[str, Any]:
    rel = Path("xarray/backends/plugins.py")
    path = worktree / rel
    original = path.read_text(encoding="utf-8")
    replacement = (
        "def create_engines_dict(\n"
        "    backend_entrypoints: list[EntryPoint],\n"
        ") -> dict[str, type[BackendEntrypoint]]:\n"
        "    engines = {}\n"
        "    for backend_ep in backend_entrypoints:\n"
        "        name = backend_ep.name\n"
        "        backend = backend_ep.load()\n"
        "        engines[name] = backend\n"
        "    return engines\n"
    )
    modified, removed_backend_loader = remove_block(
        original,
        "def backends_dict_from_pkg(\n"
        "    entrypoints: list[EntryPoint],\n"
        ") -> dict[str, type[BackendEntrypoint]]:\n",
        "\n\ndef set_missing_parameters(\n",
    )
    if removed_backend_loader:
        modified = modified.replace("\n\ndef set_missing_parameters(\n", "\n\n" + replacement + "\n\ndef set_missing_parameters(\n")
    modified, removed_sorter = remove_block(
        modified,
        "def sort_backends(\n"
        "    backend_entrypoints: dict[str, type[BackendEntrypoint]],\n"
        ") -> dict[str, type[BackendEntrypoint]]:\n",
        "\n\ndef build_engines(entrypoints: EntryPoints) -> dict[str, BackendEntrypoint]:\n",
    )
    modified = modified.replace(
        "    external_backend_entrypoints = backends_dict_from_pkg(entrypoints_unique)\n"
        "    backend_entrypoints.update(external_backend_entrypoints)\n"
        "    backend_entrypoints = sort_backends(backend_entrypoints)\n",
        "    external_backend_entrypoints = create_engines_dict(entrypoints_unique)\n"
        "    backend_entrypoints.update(external_backend_entrypoints)\n",
    )
    if not removed_backend_loader or not removed_sorter or modified == original:
        diff_text = run(["git", "diff", "--", str(rel)], cwd=worktree).stdout
        if not diff_text.strip():
            return {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "handler_failed",
                "reason": "expected xarray backend plugin feature blocks were not found",
            }
    else:
        path.write_text(modified, encoding="utf-8")
    patch_paths = write_feature_patches(row, worktree, output_dir, rel)
    return constructed_result(row, worktree, patch_paths, "semantic_revert_xarray_backend_ordering_feature")


def construct_rich_console_out(row: dict[str, Any], worktree: Path, output_dir: Path) -> dict[str, Any]:
    rel = Path("rich/console.py")
    path = worktree / rel
    original = path.read_text(encoding="utf-8")
    modified, removed_out = remove_block(
        original,
        "    def out(\n"
        "        self,\n",
        "\n    def print(\n",
    )
    if not removed_out:
        diff_text = run(["git", "diff", "--", str(rel)], cwd=worktree).stdout
        if not diff_text.strip():
            return {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "handler_failed",
                "reason": "expected Console.out block was not found",
            }
    else:
        path.write_text(modified, encoding="utf-8")
    patch_paths = write_feature_patches(row, worktree, output_dir, rel)
    return constructed_result(row, worktree, patch_paths, "semantic_remove_console_out_feature")


HANDLERS = {
    "astropy__astropy-13508": construct_astropy_time_mean,
    "astropy__astropy-14878": construct_astropy_row_get,
    "pylint-dev__pylint-5315": construct_pylint_enable_all_extensions,
    "pydata__xarray-4886": construct_xarray_backend_ordering,
    "Textualize__rich-376": construct_rich_console_out,
}


def component_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for components in (row.get("new_components") or {}).values():
        for item in components or []:
            name = item.get("name") if isinstance(item, dict) else None
            if name:
                names.append(str(name))
    return names


def inspect_pending(row: dict[str, Any], worktree: Path) -> dict[str, Any]:
    names = component_names(row)
    hits: dict[str, int] = {}
    for name in names:
        if not name:
            continue
        proc = run(["rg", "-n", name, "."], cwd=worktree, check=False)
        hits[name] = len([line for line in proc.stdout.splitlines() if line.strip()])
    return {
        "instance_id": row["instance_id"],
        "repo": row["repo"],
        "status": "pending_semantic_handler",
        "worktree": str(worktree.relative_to(ROOT)),
        "modern_head": run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip(),
        "new_component_names": names,
        "modern_symbol_hits": hits,
        "reason": "needs reviewed semantic-removal mapping before editing modern code",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = read_jsonl(Path(args.manifest))[: args.limit]
    results: list[dict[str, Any]] = []
    for row in rows:
        try:
            worktree = ensure_worktree(row, output_dir)
        except FileNotFoundError as exc:
            results.append({
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "status": "repo_cache_missing",
                "reason": str(exc),
            })
            continue
        handler = HANDLERS.get(str(row["instance_id"]))
        if handler:
            results.append(handler(row, worktree, output_dir))
        else:
            results.append(inspect_pending(row, worktree))

    write_jsonl(output_dir / "modern_poc_construction_results.jsonl", results)
    summary = {
        "rows": len(results),
        "status_counts": dict(Counter(str(row["status"]) for row in results)),
        "constructed": [
            row["instance_id"]
            for row in results
            if row["status"] == "constructed_feature_missing"
        ],
        "pending": [
            row["instance_id"]
            for row in results
            if row["status"] == "pending_semantic_handler"
        ],
    }
    (output_dir / "modern_poc_construction_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
