"""Generate concise per-PR LLM analyses for the injection report.

Reads injection_results.jsonl + sampled.jsonl, calls Azure OpenAI for each
selected PR, and writes/updates experiments/<repo>/pr_summaries.json.

Usage:
    python scripts/generate_pr_summary.py experiments/ado_anapa --prs 1372881,1398105
    python scripts/generate_pr_summary.py experiments/ado_anapa --all
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _classify(rec: dict) -> str:
    if not rec.get("success"):
        fr = rec.get("failure_reason") or ""
        return {
            "healthy_check_failed": "A_baseline_unstable",
            "build_failed_on_clean_head": "B_build_failed_clean",
            "no_test_projects_detected": "C_no_tests",
        }.get(fr, "X_other")
    v = rec.get("verification") or {}
    if v.get("pass_to_fail"):
        return "P2F_OK"
    if (v.get("buggy_total") or 0) == 0:
        return "D_buggy_execution_invalid"
    if (v.get("fail_count_increase") or 0) <= 0 and not v.get("new_failed_tests"):
        return "E_no_delta_failure"
    return "F_other_p2f_miss"


SYSTEM_PROMPT = """You are an expert code reviewer assisting with a bug-injection \
benchmark for AI coding agents. The pipeline reverse-applies a historical PR fix to \
recreate the original bug, then verifies the bug surfaces via tests.

Given the PR metadata, the original PR diff (the fix), the injected diff (what the \
pipeline actually reverted), and the test outcome, produce a concise English analysis \
in strict JSON with these fields:
- verdict: <= 14 words, one-line conclusion
- pr_intent: <= 25 words, what the original PR fixed
- injection_quality: <= 35 words, did the revert match the PR semantics, any artifacts
- root_cause: <= 35 words, why P2F was/was not confirmed (test gap, build break, baseline noise, etc.)
- next_action: <= 20 words, recommended next step (e.g. add coverage, fix AST surgery, mock dep)

Return ONLY the JSON object, no markdown fences, no commentary."""


def _truncate(s: str, limit: int) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... (truncated, {len(s) - limit} more chars)"


def _build_user_prompt(rec: dict, original_patch: str) -> str:
    v = rec.get("verification") or {}
    metrics = {
        "category": _classify(rec),
        "injection_level": rec.get("injection_level"),
        "success": rec.get("success"),
        "failure_reason": rec.get("failure_reason"),
        "healthy_passed": v.get("healthy_passed"),
        "healthy_failed": v.get("healthy_failed"),
        "healthy_total": v.get("healthy_total"),
        "buggy_passed": v.get("buggy_passed"),
        "buggy_failed": v.get("buggy_failed"),
        "buggy_total": v.get("buggy_total"),
        "fail_count_increase": v.get("fail_count_increase"),
        "pass_to_fail": v.get("pass_to_fail"),
        "new_failed_tests": (v.get("new_failed_tests") or [])[:10],
    }
    parts = [
        f"PR #{rec.get('pr_number')}: {rec.get('title', '')}",
        f"source_files: {rec.get('source_files', [])}",
        f"test_files: {rec.get('test_files', [])}",
        f"metrics: {json.dumps(metrics, ensure_ascii=False)}",
        "",
        "=== ORIGINAL PR DIFF (the fix) ===",
        _truncate(original_patch or "(missing)", 12000),
        "",
        "=== INJECTED DIFF (the revert) ===",
        _truncate(rec.get("injected_diff") or "(missing)", 12000),
    ]
    return "\n".join(parts)


def _create_azure_client(endpoint: str, api_version: str):
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    )
    return AzureOpenAI(
        azure_endpoint=endpoint,
        azure_ad_token_provider=token_provider,
        api_version=api_version,
    )


def _parse_json_loose(text: str) -> dict:
    """Best-effort parse: strip markdown fences if present."""
    t = text.strip()
    if t.startswith("```"):
        # remove first fence line
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: -3]
        # strip optional language tag
        t = t.strip()
    # Try direct parse
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # Try to locate the first { ... } block
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start : end + 1])
    raise ValueError(f"could not parse JSON from response: {text[:300]}")


def _summarize_one(client, deployment: str, rec: dict, original_patch: str) -> dict:
    user_prompt = _build_user_prompt(rec, original_patch)
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_completion_tokens=4000,
        response_format={"type": "json_object"},
    )
    choice = response.choices[0]
    content = choice.message.content or ""
    if not content:
        finish = getattr(choice, "finish_reason", "unknown")
        usage = getattr(response, "usage", None)
        details = f"finish_reason={finish}"
        if usage:
            details += f" prompt_tokens={usage.prompt_tokens} completion_tokens={usage.completion_tokens}"
        raise ValueError(f"empty LLM response ({details})")
    parsed = _parse_json_loose(content)
    return {
        "verdict": parsed.get("verdict", ""),
        "pr_intent": parsed.get("pr_intent", ""),
        "injection_quality": parsed.get("injection_quality", ""),
        "root_cause": parsed.get("root_cause", ""),
        "next_action": parsed.get("next_action", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("exp_dir", help="experiments/<repo> directory")
    parser.add_argument("--prs", help="comma-separated PR numbers")
    parser.add_argument("--all", action="store_true", help="generate for every PR")
    parser.add_argument("--force", action="store_true", help="re-summarize PRs already in pr_summaries.json")
    args = parser.parse_args()

    _load_env()

    exp_dir = Path(args.exp_dir)
    inj_file = exp_dir / "injection_results.jsonl"
    sampled_file = exp_dir / "sampled.jsonl"
    out_file = exp_dir / "pr_summaries.json"
    if not inj_file.exists():
        print(f"file not found: {inj_file}", file=sys.stderr)
        sys.exit(1)

    records: dict[int, dict] = {}
    with open(inj_file, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                records[r["pr_number"]] = r

    patches: dict[int, str] = {}
    if sampled_file.exists():
        with open(sampled_file, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                s = json.loads(line)
                patches[s["pr_number"]] = s.get("patch", "")

    # Load existing summaries
    summaries: dict[str, dict] = {}
    if out_file.exists():
        try:
            summaries = json.loads(out_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summaries = {}

    if args.all:
        target_prs = list(records.keys())
    elif args.prs:
        target_prs = [int(x) for x in args.prs.split(",") if x.strip()]
    else:
        print("must pass --prs <list> or --all", file=sys.stderr)
        sys.exit(2)

    if not args.force:
        target_prs = [p for p in target_prs if str(p) not in summaries]

    if not target_prs:
        print("nothing to do (use --force to regenerate)")
        return

    endpoint = os.environ.get("PRI_AZURE_ENDPOINT", "")
    deployment = os.environ.get("PRI_AZURE_DEPLOYMENT", "")
    api_version = os.environ.get("PRI_AZURE_API_VERSION", "2024-12-01-preview")
    if not endpoint or not deployment:
        print("missing PRI_AZURE_ENDPOINT or PRI_AZURE_DEPLOYMENT in .env", file=sys.stderr)
        sys.exit(1)

    client = _create_azure_client(endpoint, api_version)
    print(f"summarizing {len(target_prs)} PR(s) using deployment {deployment}")

    for pr in target_prs:
        if pr not in records:
            print(f"  PR #{pr}: not in injection_results, skip")
            continue
        try:
            res = _summarize_one(client, deployment, records[pr], patches.get(pr, ""))
            summaries[str(pr)] = res
            print(f"  PR #{pr}: {res['verdict']}")
        except Exception as e:
            print(f"  PR #{pr}: ERROR {e}")
            continue
        # Persist after each PR so partial progress survives
        out_file.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {out_file}  ({len(summaries)} total summaries)")


if __name__ == "__main__":
    main()
