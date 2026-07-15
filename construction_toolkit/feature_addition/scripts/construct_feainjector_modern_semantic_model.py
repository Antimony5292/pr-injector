"""Construct FeaBench modern feature-missing tasks with a model-assisted lane.

This is the PR-INJECTOR-style feature-addition path: start from modern HEAD,
remove/disable the feature semantics, and record the reverse patch as the gold
feature-restoration patch. Existing reviewed handler outputs can be seeded in,
while pending rows use Agent Maestro's OpenAI Responses endpoint.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from prinjector_v2_metrics import patch_profile, read_jsonl, resolve_text, write_jsonl
except ImportError:
    from .prinjector_v2_metrics import patch_profile, read_jsonl, resolve_text, write_jsonl

try:
    from construct_feainjector_modern_poc import (
        DEFAULT_REPO_CACHE,
        ROOT,
        component_names,
        ensure_worktree,
        repo_cache_path,
        run,
        write_feature_patches_for_paths,
    )
    from feainjector_fidelity import (
        feature_fidelity,
        feature_fidelity_feedback,
        implementation_diff,
        is_implementation_path,
    )
except ImportError:
    from .construct_feainjector_modern_poc import (
        DEFAULT_REPO_CACHE,
        ROOT,
        component_names,
        ensure_worktree,
        repo_cache_path,
        run,
        write_feature_patches_for_paths,
    )
    from .feainjector_fidelity import (
        feature_fidelity,
        feature_fidelity_feedback,
        implementation_diff,
        is_implementation_path,
    )


REPO_LOCKS: dict[str, Lock] = {}
REPO_FAILURES: dict[str, str] = {}
REPO_LOCKS_GUARD = Lock()


def repo_lock(repo: str) -> Lock:
    with REPO_LOCKS_GUARD:
        return REPO_LOCKS.setdefault(repo, Lock())


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("instance_id") or "")


def missing_python_nodeids(worktree: Path, nodeids: list[str]) -> list[str]:
    """Return Python pytest nodeids whose declared class/function no longer exists."""
    missing: list[str] = []
    parsed_files: dict[Path, ast.AST | None] = {}
    for raw_nodeid in nodeids:
        nodeid = str(raw_nodeid).strip()
        if not nodeid or "::" not in nodeid:
            continue
        parts = nodeid.split("::")
        path = worktree / parts[0]
        if path.suffix != ".py":
            continue
        if not path.is_file():
            missing.append(nodeid)
            continue
        if path not in parsed_files:
            try:
                parsed_files[path] = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                parsed_files[path] = None
        tree = parsed_files[path]
        if tree is None:
            continue
        scope: ast.AST = tree
        exists = True
        for raw_name in parts[1:]:
            name = raw_name.split("[", 1)[0]
            body = getattr(scope, "body", [])
            match = next(
                (
                    child
                    for child in body
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                    and child.name == name
                ),
                None,
            )
            if match is None:
                exists = False
                break
            scope = match
        if not exists:
            missing.append(nodeid)
    return missing


def load_seed_constructed(paths: list[Path]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            continue
        for row in read_jsonl(path):
            if row.get("status") == "constructed_feature_missing" and row_id(row):
                out.setdefault(row_id(row), row)
    return out


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    parts = lowered.split("/")
    name = parts[-1] if parts else lowered
    return (
        "tests" in parts
        or "test" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or "/tests/" in lowered
    )


def source_files_from_patch(patch: str) -> list[str]:
    files: list[str] = []
    for line in patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[6:].strip()
        if path == "/dev/null" or not is_implementation_path(path):
            continue
        if path not in files:
            files.append(path)
    return files[:8]


def modern_path_candidates(historical_path: str, worktree: Path) -> list[str]:
    """Resolve common source-layout drift without guessing semantic edits."""
    direct_candidates = [worktree / historical_path, worktree / "src" / historical_path]
    direct = [
        str(path.relative_to(worktree))
        for path in direct_candidates
        if path.exists() and path.is_file() and is_implementation_path(str(path.relative_to(worktree)))
    ]
    if direct:
        return list(dict.fromkeys(direct))

    candidates: list[Path] = []
    historical = Path(historical_path)
    suffix_parts = historical.parts[-2:]
    basenames = {historical.name, f"_{historical.name}"}
    for path in worktree.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        rel = path.relative_to(worktree)
        if path.name in basenames or (suffix_parts and tuple(rel.parts[-len(suffix_parts):]) == suffix_parts):
            candidates.append(path)
    out: list[str] = []
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        rel = str(path.relative_to(worktree))
        if is_implementation_path(rel) and rel not in out:
            out.append(rel)
    return out


def search_terms(row: dict[str, Any]) -> list[str]:
    terms = component_names(row)
    statement = str(row.get("problem_statement") or "")
    terms.extend(re.findall(r"`([A-Za-z_][A-Za-z0-9_.]{2,})`", statement))
    terms.extend(re.findall(r"\b[A-Z][A-Za-z0-9_]{3,}\b", statement))
    out: list[str] = []
    for term in terms:
        if term not in out:
            out.append(term)
    return out[:30]


def discover_context_files(row: dict[str, Any], worktree: Path) -> list[str]:
    files: list[str] = []
    for historical_path in source_files_from_patch(str(row.get("feature_patch") or "")):
        for path in modern_path_candidates(historical_path, worktree):
            if path not in files:
                files.append(path)
    if files:
        return files[:8]
    seen = set(files)
    for name in search_terms(row)[:12]:
        proc = run(["rg", "-l", "-F", "-i", name, "."], cwd=worktree, check=False)
        for line in proc.stdout.splitlines():
            rel = line.strip().removeprefix("./")
            if not rel or rel in seen or not is_implementation_path(rel):
                continue
            if (worktree / rel).is_file():
                files.append(rel)
                seen.add(rel)
            if len(files) >= 8:
                return files
    return files


def focused_source(text: str, terms: list[str], char_limit: int) -> str:
    if len(text) <= char_limit:
        return text
    lines = text.splitlines()
    lowered_terms = [term.lower() for term in terms if len(term) >= 3][:30]
    matches = [
        idx for idx, line in enumerate(lines)
        if any(term in line.lower() for term in lowered_terms)
    ]
    if not matches:
        return "\n".join(lines[: max(80, char_limit // 100)])[:char_limit]
    ranges: list[tuple[int, int]] = []
    for idx in matches[:12]:
        start, end = max(0, idx - 45), min(len(lines), idx + 46)
        if ranges and start <= ranges[-1][1] + 10:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    chunks: list[str] = []
    for start, end in ranges:
        chunks.append(f"[exact excerpt: lines {start + 1}-{end}]\n" + "\n".join(lines[start:end]))
    return "\n\n".join(chunks)[:char_limit]


def file_context(worktree: Path, files: list[str], char_limit: int, terms: list[str]) -> str:
    chunks: list[str] = []
    total_limit = int(os.environ.get("FEA_SEMANTIC_TOTAL_FILE_CONTEXT_LIMIT", "48000"))
    used = 0
    for rel in files:
        path = worktree / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text) > char_limit:
            text = focused_source(text, terms, char_limit)
        chunk = f"### {rel}\n```text\n{text}\n```"
        remaining = total_limit - used
        if remaining <= 0:
            break
        chunks.append(chunk[:remaining])
        used += min(len(chunk), remaining)
    return "\n\n".join(chunks)


def call_responses(model: str, base_url: str, api_key: str, system_prompt: str, user_prompt: str) -> tuple[str, dict[str, Any]]:
    endpoint = base_url.rstrip("/") + "/api/openai/v1/responses"
    payload = {
        "model": model,
        "instructions": system_prompt,
        "input": user_prompt,
        "max_output_tokens": int(os.environ.get("FEA_SEMANTIC_MAX_OUTPUT_TOKENS", "8192")),
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request_data = json.dumps(payload).encode("utf-8")
    attempts = max(1, int(os.environ.get("FEA_SEMANTIC_HTTP_ATTEMPTS", "3")))
    raw = ""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(endpoint, data=request_data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request,
                timeout=int(os.environ.get("FEA_SEMANTIC_TIMEOUT", "300")),
            ) as response:
                raw = response.read().decode("utf-8", errors="replace")
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            transient = exc.code in {408, 429, 500, 502, 503, 504} or "408" in detail
            if not transient or attempt >= attempts:
                raise RuntimeError(
                    f"HTTP {exc.code} (request_bytes={len(request_data)}): {detail[-1000:]}"
                ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"Agent Maestro unavailable (request_bytes={len(request_data)}): {exc}"
                ) from exc
        time.sleep(min(2 ** (attempt - 1), 8))
    data = json.loads(raw)
    texts: list[str] = []
    if isinstance(data.get("output_text"), str):
        texts.append(data["output_text"])
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                texts.append(str(block.get("text") or ""))
    usage = data.get("usage") or {}
    return "\n".join(text for text in texts if text).strip(), {
        "provider": "agent_maestro_openai_responses",
        "model": data.get("model") or model,
        "response_id": data.get("id"),
        "status": data.get("status"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def extract_diff(text: str) -> str:
    for match in re.finditer(r"```(?:diff)?\s*\n(.*?)```", text, re.DOTALL):
        block = match.group(1).strip()
        if "diff --git " in block:
            return trim_diff(block)
    match = re.search(r"(diff --git\s+.*)", text, re.DOTALL)
    return trim_diff(match.group(1).strip()) if match else ""


def trim_diff(diff: str) -> str:
    lines = diff.splitlines()
    start = next((idx for idx, line in enumerate(lines) if line.startswith("diff --git ")), None)
    if start is None:
        return ""
    kept: list[str] = []
    for line in lines[start:]:
        if line.startswith(("diff --git ", "index ", "--- ", "+++ ", "@@ ", "+", "-", " ")):
            kept.append(line)
            continue
        if line.startswith(("new file mode ", "deleted file mode ", "similarity index ")):
            kept.append(line)
            continue
        if kept and line.strip() == "":
            kept.append(line)
            continue
        break
    return "\n".join(kept).rstrip() + "\n" if kept else ""


def diff_files(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in files:
                files.append(path)
    return files


def reanchor_unified_diff(diff: str, worktree: Path) -> str:
    """Recompute malformed/stale hunk headers from exact current source text."""
    lines = diff.splitlines()
    out: list[str] = []
    current_path = ""
    source_cache: dict[str, list[str]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ b/"):
            current_path = line[6:].strip()
            out.append(line)
            i += 1
            continue
        if not line.startswith("@@ ") or not current_path:
            out.append(line)
            i += 1
            continue

        j = i + 1
        while j < len(lines) and not lines[j].startswith(("@@ ", "diff --git ")):
            j += 1
        hunk = lines[i + 1:j]
        old_sequence = [item[1:] for item in hunk if item.startswith((" ", "-"))]
        old_count = len(old_sequence)
        new_count = sum(1 for item in hunk if item.startswith((" ", "+")))
        match = re.match(r"@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)", line)
        if not match or not old_sequence:
            out.append(line)
            out.extend(hunk)
            i = j
            continue

        source_lines = source_cache.get(current_path)
        if source_lines is None:
            source = worktree / current_path
            source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines() if source.exists() else []
            source_cache[current_path] = source_lines
        positions = [
            pos
            for pos in range(0, len(source_lines) - old_count + 1)
            if source_lines[pos:pos + old_count] == old_sequence
        ]
        if positions:
            expected = int(match.group(1))
            actual = min(positions, key=lambda pos: abs((pos + 1) - expected)) + 1
            out.append(f"@@ -{actual},{old_count} +{actual},{new_count} @@{match.group(3)}")
        else:
            out.append(line)
        out.extend(hunk)
        i = j
    return "\n".join(out).rstrip() + "\n"


def apply_model_diff(worktree: Path, diff: str) -> tuple[bool, str, str]:
    strategies = [
        (["git", "apply", "--check", "--recount"], ["git", "apply", "--recount"], "git_apply"),
        (
            ["git", "apply", "--check", "--recount", "--ignore-space-change"],
            ["git", "apply", "--recount", "--ignore-space-change"],
            "git_apply_ignore_space",
        ),
    ]
    errors: list[str] = []
    for check_args, apply_args, name in strategies:
        check = subprocess.run(
            check_args,
            cwd=worktree,
            input=diff,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check.returncode != 0:
            errors.append(f"{name}: {check.stderr[-800:]}")
            continue
        applied = subprocess.run(
            apply_args,
            cwd=worktree,
            input=diff,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if applied.returncode == 0:
            return True, name, ""
        errors.append(f"{name}: {applied.stderr[-800:]}")

    reanchored = reanchor_unified_diff(diff, worktree)
    if reanchored != diff:
        check = subprocess.run(
            ["git", "apply", "--check", "--recount"],
            cwd=worktree,
            input=reanchored,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check.returncode == 0:
            applied = subprocess.run(
                ["git", "apply", "--recount"],
                cwd=worktree,
                input=reanchored,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if applied.returncode == 0:
                return True, "git_apply_reanchored", ""
            errors.append(f"git_apply_reanchored: {applied.stderr[-800:]}")
        else:
            errors.append(f"git_apply_reanchored: {check.stderr[-800:]}")

    # Model-generated hunks can carry stale line offsets even when their
    # surrounding code is correct. BSD patch's bounded fuzz is a safe fallback
    # here because the resulting source diff still has to pass fidelity and the
    # full feature/P2P/gold-restore verifier.
    dry_run = subprocess.run(
        ["patch", "--dry-run", "--batch", "-p1", "-F", "3"],
        cwd=worktree,
        input=diff,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if dry_run.returncode == 0:
        applied = subprocess.run(
            ["patch", "--batch", "-p1", "-F", "3"],
            cwd=worktree,
            input=diff,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if applied.returncode == 0:
            return True, "patch_fuzz_3", ""
        errors.append(f"patch_fuzz_3: {(applied.stderr or applied.stdout)[-800:]}")
    else:
        errors.append(f"patch_fuzz_3: {(dry_run.stderr or dry_run.stdout)[-800:]}")
    return False, "", "\n".join(errors)[-2400:]


def ensure_worktree_with_optional_clone(
    row: dict[str, Any],
    output_dir: Path,
    repo_cache_roots: list[Path],
    *,
    checkout_modern_head: bool,
    clone_missing_repos: bool,
) -> Path:
    repo = str(row["repo"])
    with repo_lock(repo):
        if repo in REPO_FAILURES:
            raise RuntimeError(f"cached repo preflight failure for {repo}: {REPO_FAILURES[repo]}")
        try:
            if repo_cache_path(repo, repo_cache_roots) is None and clone_missing_repos:
                cache_root = repo_cache_roots[0] if repo_cache_roots else DEFAULT_REPO_CACHE.resolve()
                cache_root.mkdir(parents=True, exist_ok=True)
                target = cache_root / repo.replace("/", "__")
                if not target.exists():
                    repo_url = f"https://github.com/{repo}.git"
                    run(
                        ["git", "clone", "--filter=blob:none", "--no-tags", repo_url, str(target)],
                        timeout=int(os.environ.get("FEA_GIT_CLONE_TIMEOUT", "600")),
                    )
            return ensure_worktree(
                row,
                output_dir,
                repo_cache_roots,
                checkout_modern_head=checkout_modern_head,
            )
        except Exception as exc:
            REPO_FAILURES[repo] = str(exc)[:1000]
            raise


def construct_with_model(
    row: dict[str, Any],
    worktree: Path,
    output_dir: Path,
    model: str,
    base_url: str,
    api_key: str,
    require_fidelity_gate: bool,
) -> dict[str, Any]:
    context_files = discover_context_files(row, worktree)
    if not context_files:
        return {
            "instance_id": row_id(row),
            "repo": row.get("repo"),
            "status": "semantic_model_context_missing",
            "reason": "no modern source files found from feature patch or component-name search",
        }
    system_prompt = (
        "You construct feature-missing benchmark states. Given a feature-addition PR and modern "
        "HEAD code, output ONLY a valid unified diff that removes or disables the same feature "
        "semantics from the modern code. Do not edit tests, docs, lockfiles, generated files, or "
        "unrelated behavior. The diff must apply cleanly and start with 'diff --git'."
    )
    profile = patch_profile(implementation_diff(str(row.get("feature_patch") or "")))
    previous_feedback = ""
    if row.get("feature_retry_previous_status") or row.get("feature_retry_previous_reason"):
        previous_feedback = f"""
Previous attempt status: {row.get('feature_retry_previous_status')}
Previous attempt failure: {str(row.get('feature_retry_previous_reason') or '')[:2000]}
"""
    user_prompt = f"""## Feature-Addition Source Case

Instance: {row_id(row)}
Repo: {row.get("repo")}

Problem statement:
{str(row.get("problem_statement") or "")[:2500]}

New component names:
{json.dumps(component_names(row), ensure_ascii=False)[:3000]}

Feature tests expected to fail in the feature-missing state:
{json.dumps(row.get("FAIL_TO_PASS") or [], ensure_ascii=False)[:3000]}

Original feature-addition patch profile:
- line_changes: {profile.line_changes}
- hunks: {profile.hunks}
- source_files: {profile.source_files}
{previous_feedback}

Original feature-addition patch:
```diff
{str(row.get("feature_patch") or "")[:12000]}
```

## Modern HEAD Code Context

Allowed modern source paths: {json.dumps(context_files, ensure_ascii=False)}

{file_context(worktree, context_files, int(os.environ.get("FEA_SEMANTIC_FILE_CONTEXT_LIMIT", "10000")), component_names(row))}

## Task

Generate a feature-missing diff against the modern code above:
- remove or disable the feature semantics added by the source PR;
- keep the surrounding modern APIs valid;
- preserve unrelated functionality and adjacent behavior;
- do not edit tests or documentation;
- use only paths listed in Allowed modern source paths; historical paths may no longer exist;
- preserve the historical feature's multi-file and multi-hunk behavior surface when it still exists;
- keep the change scope comparable to the original feature patch when feasible.
- copy every hunk's context lines verbatim from Modern HEAD Code Context;
- prefer three context lines per hunk so the unified diff remains robust.

Output ONLY a unified diff starting with diff --git."""
    max_attempts = max(1, int(os.environ.get("FEA_SEMANTIC_ATTEMPTS", "3")))
    feedback = ""
    actual = ""
    touched: list[str] = []
    meta: dict[str, Any] = {}
    model_attempts: list[dict[str, Any]] = []
    last_failure: dict[str, Any] = {
        "instance_id": row_id(row),
        "repo": row.get("repo"),
        "status": "semantic_model_no_attempt",
        "reason": "no attempt made",
    }
    for attempt in range(1, max_attempts + 1):
        attempt_prompt = user_prompt
        if feedback:
            attempt_prompt += f"""

## Feedback From Previous Attempt

The previous diff failed construction validation:

```text
{feedback[:2000]}
```

Regenerate the diff against the exact Modern HEAD code context above. Do not
reuse stale line numbers or historical context from the original feature PR.
Every hunk must match current file contents and apply with git apply.
"""
        response, meta = call_responses(model, base_url, api_key, system_prompt, attempt_prompt)
        meta["attempt"] = attempt
        meta["max_attempts"] = max_attempts
        diff = extract_diff(response)
        if not diff:
            feedback = "model response did not contain a unified diff"
            last_failure = {
                "instance_id": row_id(row),
                "repo": row.get("repo"),
                "status": "semantic_model_no_diff",
                "reason": feedback,
                "model_metadata": meta,
                "response_preview": response[:1000],
            }
            continue
        attempt_dir = output_dir / "model_attempts" / row_id(row)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt_path = attempt_dir / f"attempt_{attempt:02d}.diff"
        attempt_path.write_text(diff, encoding="utf-8")
        touched = diff_files(diff)
        forbidden = [path for path in touched if not is_implementation_path(path)]
        if forbidden:
            feedback = f"model diff touched non-implementation paths: {forbidden}"
            last_failure = {
                "instance_id": row_id(row),
                "repo": row.get("repo"),
                "status": "semantic_model_rejected",
                "reason": "model diff touched tests, docs, examples, generated data, or config files",
                "touched_files": touched,
                "model_metadata": meta,
            }
            continue
        applied, apply_strategy, apply_error = apply_model_diff(worktree, diff)
        model_attempts.append({
            "attempt": attempt,
            "diff_file": str(attempt_path.resolve().relative_to(ROOT)),
            "touched_files": touched,
            "apply_strategy": apply_strategy or None,
            "apply_error": apply_error or None,
            "model_metadata": meta,
        })
        if not applied:
            feedback = apply_error
            last_failure = {
                "instance_id": row_id(row),
                "repo": row.get("repo"),
                "status": "semantic_model_patch_apply_failed",
                "reason": feedback,
                "model_metadata": meta,
                "model_attempts": model_attempts,
                "touched_files": touched,
            }
            continue
        actual = run(["git", "diff", "--", *touched], cwd=worktree).stdout
        if not actual.strip():
            feedback = "diff applied but produced no source change"
            last_failure = {
                "instance_id": row_id(row),
                "repo": row.get("repo"),
                "status": "semantic_model_empty_patch",
                "reason": feedback,
                "model_metadata": meta,
                "touched_files": touched,
            }
            continue
        restore = run(["git", "diff", "-R", "--", *touched], cwd=worktree).stdout
        fidelity = feature_fidelity(
            str(row.get("feature_patch") or ""),
            restore,
            source_target_tests=len(row.get("FAIL_TO_PASS") or []),
            modern_target_tests=len(row.get("FAIL_TO_PASS") or []),
            source_regression_tests=len(row.get("PASS_TO_PASS") or []),
            modern_regression_tests=len(row.get("PASS_TO_PASS") or []),
        )
        if require_fidelity_gate and not fidelity.get("passed"):
            feedback = feature_fidelity_feedback(fidelity)
            last_failure = {
                "instance_id": row_id(row),
                "repo": row.get("repo"),
                "status": "semantic_model_fidelity_gate_failed",
                "reason": feedback,
                "feature_fidelity_gate": fidelity,
                "model_metadata": meta,
                "touched_files": touched,
            }
            run(["git", "reset", "--hard", "HEAD"], cwd=worktree)
            actual = ""
            touched = []
            continue
        break
    else:
        return last_failure
    patch_paths = write_feature_patches_for_paths(row, worktree, output_dir, [Path(path) for path in touched])
    b_profile = patch_profile(actual)
    ratio = round((b_profile.line_changes / profile.line_changes), 4) if profile.line_changes else None
    fidelity = feature_fidelity(
        str(row.get("feature_patch") or ""),
        resolve_text(patch_paths["gold_feature_restore_patch"], ROOT),
        source_target_tests=len(row.get("FAIL_TO_PASS") or []),
        modern_target_tests=len(row.get("FAIL_TO_PASS") or []),
        source_regression_tests=len(row.get("PASS_TO_PASS") or []),
        modern_regression_tests=len(row.get("PASS_TO_PASS") or []),
    )
    return {
        "instance_id": row_id(row),
        "repo": row.get("repo"),
        "status": "constructed_feature_missing",
        "strategy": "semantic_model_remove_modern_feature",
        "worktree": str(worktree.relative_to(ROOT)),
        "modern_head": run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip(),
        "feature_missing_patch": patch_paths["feature_missing_patch"],
        "gold_feature_restore_patch": patch_paths["gold_feature_restore_patch"],
        "feature_tests": row.get("FAIL_TO_PASS") or [],
        "pass_to_pass": row.get("PASS_TO_PASS") or [],
        "verification_status": "not_run",
        "verification_blocker": "feature-addition verification harness not run yet",
        "model_metadata": meta,
        "model_attempts": model_attempts,
        "touched_files": touched,
        "feature_complexity_profile": {
            "A_line_changes": profile.line_changes,
            "A_hunks": profile.hunks,
            "A_source_files": profile.source_files,
            "B_line_changes": b_profile.line_changes,
            "B_hunks": b_profile.hunks,
            "B_source_files": b_profile.source_files,
            "line_change_ratio": ratio,
        },
        "feature_fidelity_gate": fidelity,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=111)
    parser.add_argument("--seed-results", action="append", default=[])
    parser.add_argument("--repo-cache-root", action="append", default=[])
    parser.add_argument("--model", default=os.environ.get("FEA_SEMANTIC_MODEL", "gpt-5.3-codex"))
    parser.add_argument("--base-url", default=os.environ.get("AGENT_MAESTRO_BASE_URL", "http://127.0.0.1:23334"))
    parser.add_argument("--api-key", default=os.environ.get("AGENT_MAESTRO_API_KEY", ""))
    parser.add_argument("--no-checkout-default-branch", action="store_true")
    parser.add_argument("--clone-missing-repos", action="store_true")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("FEA_SEMANTIC_WORKERS", "3")))
    parser.add_argument("--no-require-fidelity-gate", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        parser.error("AGENT_MAESTRO_API_KEY is required; load it from Keychain before construction")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    repo_cache_roots = [Path(path).resolve() for path in args.repo_cache_root] or [DEFAULT_REPO_CACHE.resolve()]
    seeded = load_seed_constructed([Path(path) for path in args.seed_results])
    rows = read_jsonl(Path(args.manifest))[: args.limit]
    results_by_index: dict[int, dict[str, Any]] = {}

    def process(index: int, row: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        iid = row_id(row)
        if iid in seeded:
            copied = dict(seeded[iid])
            copied["status"] = "constructed_feature_missing"
            copied["strategy"] = copied.get("strategy") or "seeded_reviewed_handler"
            copied["seeded_from"] = "existing_reviewed_handler_result"
            return index, copied
        try:
            worktree = ensure_worktree_with_optional_clone(
                row,
                output_dir,
                repo_cache_roots,
                checkout_modern_head=not args.no_checkout_default_branch,
                clone_missing_repos=args.clone_missing_repos,
            )
            missing_targets = missing_python_nodeids(worktree, list(row.get("FAIL_TO_PASS") or []))
            missing_p2p = missing_python_nodeids(worktree, list(row.get("PASS_TO_PASS") or []))
            if missing_targets or missing_p2p:
                return index, {
                    "instance_id": iid,
                    "repo": row.get("repo"),
                    "status": "preflight_nodeids_not_present",
                    "reason": "modern HEAD no longer declares one or more mapped pytest nodeids",
                    "missing_target_nodeids": missing_targets,
                    "missing_pass_to_pass_nodeids": missing_p2p,
                }
            result = construct_with_model(
                row,
                worktree,
                output_dir,
                args.model,
                args.base_url,
                args.api_key,
                require_fidelity_gate=not args.no_require_fidelity_gate,
            )
        except FileNotFoundError as exc:
            result = {"instance_id": iid, "repo": row.get("repo"), "status": "repo_cache_missing", "reason": str(exc)}
        except Exception as exc:
            result = {"instance_id": iid, "repo": row.get("repo"), "status": "semantic_model_exception", "reason": str(exc)[:1000]}
        return index, result

    workers = max(1, args.workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(process, index, row) for index, row in enumerate(rows)]
        for future in as_completed(futures):
            index, result = future.result()
            results_by_index[index] = result
            write_jsonl(
                output_dir / "modern_semantic_construction_results.jsonl",
                [results_by_index[idx] for idx in sorted(results_by_index)],
            )

    results = [results_by_index[idx] for idx in sorted(results_by_index)]
    write_jsonl(output_dir / "modern_semantic_construction_results.jsonl", results)
    summary = {
        "rows": len(results),
        "model": args.model,
        "workers": workers,
        "require_fidelity_gate": not args.no_require_fidelity_gate,
        "repo_cache_roots": [str(path) for path in repo_cache_roots],
        "seeded_constructed": len(seeded),
        "status_counts": dict(Counter(str(row.get("status")) for row in results)),
        "strategy_counts": dict(Counter(str(row.get("strategy")) for row in results if row.get("strategy"))),
        "constructed": [row_id(row) for row in results if row.get("status") == "constructed_feature_missing"],
    }
    (output_dir / "modern_semantic_construction_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
