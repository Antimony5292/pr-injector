"""Build external feature-addition source candidates for PR-INJECTOR.

This script is intentionally conservative. It does not claim a row is already a
valid feature-addition benchmark task; it only normalizes promising
feature/enhancement implementation rows into a source manifest for the later
modern feature-missing construction and strict verifier.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from datasets import load_dataset
from huggingface_hub import HfApi, hf_hub_download

try:
    from prinjector_v2_metrics import patch_profile, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import patch_profile, write_jsonl


FEATURE_RE = re.compile(
    r"\b("
    r"feature|enhancement|implement|implementation|add support|support for|"
    r"new option|new parameter|new api|allow|enable|introduce|expose|"
    r"extend|capability|functionality"
    r")\b",
    re.IGNORECASE,
)
BUG_RE = re.compile(
    r"\b("
    r"bug|fix|regression|crash|incorrect|wrong|error|failure|exception|"
    r"broken|flaky|segfault|leak"
    r")\b",
    re.IGNORECASE,
)

BUGFIX_SECTION_RE = re.compile(
    r"^\s{0,3}\+?\s*#{1,4}\s*(?:bug\s*fix(?:es)?|fix(?:es)?|bugfix(?:es)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def coerce_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return [value]
    return [str(value)]


def first(row: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", [], {}):
            return value
    return ""


def repo_from_instance_id(instance_id: str) -> str:
    """Infer owner/repo from SWE-style instance ids such as owner__repo-123."""
    if not instance_id or "__" not in instance_id:
        return ""
    prefix = instance_id.rsplit("-", 1)[0]
    if "__" not in prefix:
        return ""
    owner, repo = prefix.split("__", 1)
    if owner and repo:
        return f"{owner}/{repo}"
    return ""


def row_text(row: dict[str, Any]) -> str:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    llm_meta = meta.get("llm_metadata") if isinstance(meta.get("llm_metadata"), dict) else {}
    fields = [
        first(row, ["problem_statement", "issue", "issue_body", "description", "body"]),
        first(row, ["title", "issue_title", "summary"]),
        first(row, ["hints_text", "hints", "labels"]),
        row.get("pr_description", ""),
        row.get("task_type", ""),
        meta.get("pr_labels", []),
        llm_meta.get("pr_categories", []),
    ]
    return "\n".join(str(field) for field in fields if field)


def feature_signal(row: dict[str, Any]) -> dict[str, Any]:
    text = row_text(row)
    feature_hits = FEATURE_RE.findall(text)
    bug_hits = BUG_RE.findall(text)
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    llm_meta = meta.get("llm_metadata") if isinstance(meta.get("llm_metadata"), dict) else {}
    labels = " ".join(
        coerce_list(first(row, ["labels", "label_names", "issue_labels"]))
        + coerce_list(meta.get("pr_labels"))
        + coerce_list(llm_meta.get("pr_categories"))
    )
    label_feature = bool(FEATURE_RE.search(labels))
    label_bug = bool(BUG_RE.search(labels))
    task_type = str(row.get("task_type") or "").strip().lower().replace("_", "-")
    explicit_feature = task_type in {"feature", "feature-request", "enhancement"}
    explicit_bug = task_type in {"bug", "bugfix", "bug-fix", "bug-report"}
    score = (
        len(feature_hits)
        + (2 if label_feature else 0)
        + (4 if explicit_feature else 0)
        - len(bug_hits)
        - (2 if label_bug else 0)
        - (3 if explicit_bug else 0)
    )
    return {
        "score": score,
        "feature_hits": len(feature_hits),
        "bug_hits": len(bug_hits),
        "label_feature": label_feature,
        "label_bug": label_bug,
        "explicit_feature_task": explicit_feature,
        "explicit_bug_task": explicit_bug,
    }


def infer_language(raw_language: str, patch: str) -> str:
    language = raw_language.strip().lower()
    if language:
        return language
    extension_language = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".c": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".h": "cpp",
        ".hpp": "cpp",
    }
    counts: Counter[str] = Counter()
    for match in re.finditer(r"^\+\+\+\s+b/(\S+)$", patch, re.MULTILINE):
        suffix = Path(match.group(1)).suffix.lower()
        inferred = extension_language.get(suffix)
        if inferred:
            counts[inferred] += 1
    return counts.most_common(1)[0][0] if counts else "unknown"


def normalize_row(dataset_id: str, split: str, index: int, row: dict[str, Any]) -> dict[str, Any]:
    patch = str(first(row, ["patch", "gold_patch", "solution_patch", "code_patch", "diff"]))
    test_patch = str(first(row, ["test_patch", "tests_patch", "unit_test_patch"]))
    source_profile = patch_profile(patch)
    test_profile = patch_profile(test_patch)
    signal = feature_signal(row)
    instance_id = str(first(row, ["instance_id", "id", "task_id", "problem_id"]) or f"{dataset_id.replace('/', '__')}__{split}__{index}")
    raw_repo = str(first(row, ["repo", "repo_name", "repository", "github_repo"]))
    inferred_repo = repo_from_instance_id(instance_id)
    repo = inferred_repo if inferred_repo and "/" not in raw_repo else raw_repo
    language = infer_language(
        str(first(row, ["language", "lang", "programming_language"])),
        patch,
    )
    fail_to_pass = coerce_list(first(row, ["FAIL_TO_PASS", "fail_to_pass", "f2p", "tests_to_pass"]))
    pass_to_pass = coerce_list(first(row, ["PASS_TO_PASS", "pass_to_pass", "p2p", "regression_tests"]))

    reasons: list[str] = []
    if not repo:
        reasons.append("missing_repo")
    if source_profile.source_files <= 0:
        reasons.append("missing_source_patch")
    if source_profile.line_changes < 6:
        reasons.append("feature_patch_too_small")
    if source_profile.line_changes > 320 or source_profile.source_files > 8 or source_profile.hunks > 36:
        reasons.append("feature_patch_too_large")
    if test_profile.test_files <= 0 and not fail_to_pass:
        reasons.append("missing_feature_tests")
    if signal["score"] <= 0:
        reasons.append("weak_feature_signal")
    if signal["explicit_bug_task"] or BUGFIX_SECTION_RE.search(patch):
        reasons.append("bugfix_task_misclassified_as_feature")
    if language and language not in {"python", "py", "javascript", "typescript", "java", "go", "rust", "c", "cpp", "c++"}:
        reasons.append("unknown_language")

    runner_status = "python_pytest_ready" if language in {"python", "py", ""} else "adapter_backlog"
    pass_gate = not set(reasons).intersection(
        {
            "missing_repo",
            "missing_source_patch",
            "feature_patch_too_small",
            "feature_patch_too_large",
            "missing_feature_tests",
            "weak_feature_signal",
            "bugfix_task_misclassified_as_feature",
        }
    )

    return {
        "instance_id": instance_id,
        "repo": repo,
        "source_benchmark": dataset_id,
        "source_split": split,
        "source_file": row.get("_hf_source_file", ""),
        "source_row_index": index,
        "task_family": "feature_addition",
        "language": language,
        "runner_adapter_status": runner_status,
        "source_base_commit": str(first(row, ["base_commit", "base_sha", "commit", "environment_setup_commit"])),
        "pull_number": first(row, ["pull_number", "pr_number", "pull_request", "pr_id"]),
        "source_created_at": str(first(row, ["created_at", "merged_at", "closed_at"])),
        "source_task_type": str(first(row, ["task_type", "pr_type", "category"])),
        "problem_statement": str(first(row, ["problem_statement", "issue", "issue_body", "description", "body", "title"])),
        "feature_patch": patch,
        "feature_test_patch": test_patch,
        "FAIL_TO_PASS": fail_to_pass,
        "PASS_TO_PASS": pass_to_pass,
        "feature_source_files": source_profile.source_files,
        "feature_patch_hunks": source_profile.hunks,
        "feature_patch_line_changes": source_profile.line_changes,
        "feature_test_files": test_profile.test_files,
        "feature_test_line_changes": test_profile.line_changes,
        "feature_signal": signal,
        "feature_gate_pass": pass_gate,
        "feature_gate_reasons": reasons,
    }


def parse_dataset_arg(value: str) -> tuple[str, str | None]:
    if "::" in value:
        dataset_id, split = value.split("::", 1)
        return dataset_id, split or None
    return value, None


def load_dataset_split(dataset_id: str, split: str | None) -> tuple[Any, str]:
    if split:
        return load_dataset(dataset_id, split=split, streaming=True), split
    dataset = load_dataset(dataset_id, streaming=True)
    if hasattr(dataset, "keys"):
        keys = list(dataset.keys())
        for candidate in ("train", "test", "validation", "dev"):
            if candidate in keys:
                return dataset[candidate], candidate
        if keys:
            return dataset[keys[0]], keys[0]
    return dataset, "train"


def load_hf_jsonl_rows(dataset_id: str, scan_limit: int) -> tuple[list[dict[str, Any]], str]:
    api = HfApi()
    files = [
        name for name in api.list_repo_files(dataset_id, repo_type="dataset")
        if name.endswith(".jsonl")
    ]
    rows: list[dict[str, Any]] = []
    for filename in sorted(files):
        path = hf_hub_download(repo_id=dataset_id, repo_type="dataset", filename=filename)
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    row["_hf_source_file"] = filename
                    rows.append(row)
                    if len(rows) >= scan_limit:
                        return rows, "jsonl_files"
    return rows, "jsonl_files"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", action="append", required=True, help="HF dataset id, optionally id::split")
    parser.add_argument("--output-dir", default="artifacts/feature-external/source-manifest")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--scan-limit-per-dataset", type=int, default=3000)
    parser.add_argument("--python-first", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for dataset_arg in args.dataset:
        dataset_id, split = parse_dataset_arg(dataset_arg)
        try:
            ds, actual_split = load_dataset_split(dataset_id, split)
            iterator = enumerate(ds)
        except Exception as exc:  # noqa: BLE001
            try:
                jsonl_rows, actual_split = load_hf_jsonl_rows(dataset_id, args.scan_limit_per_dataset)
                iterator = enumerate(jsonl_rows)
            except Exception as jsonl_exc:  # noqa: BLE001
                errors.append(
                    {
                        "dataset": dataset_id,
                        "split": split or "auto",
                        "error": str(exc),
                        "jsonl_fallback_error": str(jsonl_exc),
                    }
                )
                continue
        for index, row in iterator:
            if index >= args.scan_limit_per_dataset:
                break
            rows.append(normalize_row(dataset_id, actual_split, index, dict(row)))

    def sort_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
        python_ready = 1 if row.get("runner_adapter_status") == "python_pytest_ready" else 0
        return (
            1 if row.get("feature_gate_pass") else 0,
            python_ready if args.python_first else 0,
            int(row.get("feature_signal", {}).get("score") or 0),
            int(row.get("feature_patch_line_changes") or 0),
        )

    ranked = sorted(rows, key=sort_key, reverse=True)
    selected = ranked[: args.limit]
    gate_pass = [row for row in ranked if row.get("feature_gate_pass")]
    python_ready = [row for row in gate_pass if row.get("runner_adapter_status") == "python_pytest_ready"]

    write_jsonl(output_dir / "external_feature_source_candidates_all.jsonl", ranked)
    write_jsonl(output_dir / "external_feature_source_candidates_selected.jsonl", selected)
    write_jsonl(output_dir / "external_feature_source_gate_pass.jsonl", gate_pass)
    write_jsonl(output_dir / "external_feature_source_python_ready.jsonl", python_ready)
    write_jsonl(output_dir / "external_feature_source_errors.jsonl", errors)
    summary = {
        "datasets": args.dataset,
        "rows_scanned": len(rows),
        "selected_rows": len(selected),
        "gate_pass": len(gate_pass),
        "python_ready_gate_pass": len(python_ready),
        "by_benchmark": dict(Counter(str(row.get("source_benchmark")) for row in rows).most_common()),
        "by_runner_status": dict(Counter(str(row.get("runner_adapter_status")) for row in rows).most_common()),
        "reject_reasons": dict(Counter(reason for row in rows for reason in row.get("feature_gate_reasons", [])).most_common()),
        "errors": errors,
        "outputs": {
            "selected": str(output_dir / "external_feature_source_candidates_selected.jsonl"),
            "gate_pass": str(output_dir / "external_feature_source_gate_pass.jsonl"),
            "python_ready": str(output_dir / "external_feature_source_python_ready.jsonl"),
        },
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
