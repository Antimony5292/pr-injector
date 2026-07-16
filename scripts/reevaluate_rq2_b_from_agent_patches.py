"""Re-evaluate RQ2 B runs from existing agent patches.

This is used when benchmark metadata changes but the agent prompt/output did
not. It replays the PR-INJECTOR B setup, applies the saved agent patch, and
runs the corrected B FAIL_TO_PASS / PASS_TO_PASS tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import run_rq2_claude_bedrock_eval as rq2


ROOT = Path(__file__).resolve().parent.parent


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def replay_case(case: dict, args: argparse.Namespace, repos_dirs: list[Path]) -> dict:
    case_id = case["case_id"]
    repo = case["repo"]
    source_result = Path(args.source_run_dir) / case_id / "B" / "result.json"
    source_patch = Path(args.source_run_dir) / case_id / "B" / "agent.patch"
    out_dir = Path(args.output_dir) / case_id / "B"
    result_path = out_dir / "result.json"
    if result_path.exists() and not args.force:
        return json.loads(result_path.read_text(encoding="utf-8"))

    out_dir.mkdir(parents=True, exist_ok=True)
    if not source_result.exists():
        result = {
            "case_id": case_id,
            "group": "B",
            "repo": repo,
            "evaluation": {"status": "missing_source_result", "solved": False},
        }
        write_json(result_path, result)
        return result

    source = json.loads(source_result.read_text(encoding="utf-8"))
    agent_patch = source_patch.read_text(encoding="utf-8", errors="replace") if source_patch.exists() else ""
    ref = case["B_healthy_head"]
    branch = f"rq2-replay-{case_id.lower()}-{os.getpid()}"
    worktree = (ROOT / args.worktrees_dir).resolve() / f"{case_id}-B-{repo.replace('/', '__')}"
    repo_path = rq2.repo_dir(repo, repos_dirs, ref)

    injected_apply = {"applied": False, "skipped": True, "reason": "agent.patch already includes injected diff"}
    agent_apply = None
    eval_result = {}
    try:
        rq2.create_worktree(repo_path, worktree, ref, branch)
        agent_apply = rq2.apply_patch_text(worktree, agent_patch, "agent_patch")
        if not agent_apply["applied"]:
            eval_result = {"status": "agent_patch_apply_failed", "solved": False, "agent_apply": agent_apply}
        else:
            changed_files = rq2.git_changed_files(worktree)
            forbidden_files = rq2.forbidden_agent_edits(changed_files)
            if args.forbid_forbidden_edits and forbidden_files:
                eval_result = {
                    "status": "agent_modified_forbidden_files",
                    "solved": False,
                    "strict_solved": False,
                    "target_solved": False,
                    "forbidden_files": forbidden_files,
                    "changed_files": changed_files,
                }
            else:
                vpy = rq2.create_venv(worktree)
                fail_to_pass_tests = rq2.normalize_tests(worktree, repo, case.get("B_FAIL_TO_PASS", []))
                rq2.install_project(worktree, repo, vpy, fail_to_pass_tests)
                fail_to_pass = rq2.run_tests(worktree, repo, fail_to_pass_tests, args.test_timeout_s, vpy)
                p2p_source = case.get("B_PASS_TO_PASS_CLEAN") or case.get("B_PASS_TO_PASS", [])
                p2p_tests = rq2.normalize_tests(worktree, repo, p2p_source)[: args.max_pass_to_pass]
                pass_to_pass = rq2.run_tests(worktree, repo, p2p_tests, args.test_timeout_s, vpy) if p2p_tests else None
                target_solved, strict_solved = rq2.strict_eval_solved(
                    fail_to_pass, pass_to_pass, args.require_pass_to_pass
                )
                eval_result = {
                    "status": "completed",
                    "solved": strict_solved,
                    "strict_solved": strict_solved,
                    "target_solved": target_solved,
                    "fail_to_pass": fail_to_pass,
                    "pass_to_pass": pass_to_pass,
                    "runnable_fail_to_pass": fail_to_pass_tests,
                    "runnable_pass_to_pass_count": len(p2p_tests),
                }
    except Exception as exc:
        eval_result = {"status": "error", "solved": False, "error": str(exc)}
    finally:
        result = {
            "case_id": case_id,
            "group": "B",
            "repo": repo,
            "source_dataset": case.get("source_dataset"),
            "A_instance_id": case.get("A_instance_id"),
            "B_instance_id": case.get("B_instance_id"),
            "source_result_path": str(source_result),
            "source_agent_patch_path": str(source_patch),
            "agent_patch_size": len(agent_patch),
            "setup": {"ref": ref, "injected_apply": injected_apply, "agent_apply": agent_apply},
            "evaluation": eval_result,
        }
        write_json(result_path, result)
        rq2.remove_worktree(repo_path, worktree, branch, args.keep_worktrees)
    return result


def summarize(output_dir: Path) -> dict:
    results = [json.loads(p.read_text(encoding="utf-8")) for p in output_dir.glob("RQ2_*/*/result.json")]
    summary = {
        "results": len(results),
        "B_runs": sum(1 for r in results if r.get("group") == "B"),
        "B_solved": sum(1 for r in results if r.get("group") == "B" and r.get("evaluation", {}).get("solved")),
        "B_unsolved": sum(1 for r in results if r.get("group") == "B" and not r.get("evaluation", {}).get("solved")),
    }
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairing", required=True)
    parser.add_argument("--source-run-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--worktrees-dir", default=".pri-workspace/rq2-b-replay-worktrees")
    parser.add_argument("--repos-dir", action="append", default=[])
    parser.add_argument("--test-timeout-s", type=int, default=300)
    parser.add_argument("--max-pass-to-pass", type=int, default=20)
    parser.add_argument("--require-pass-to-pass", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--keep-worktrees", action="store_true")
    parser.add_argument("--forbid-forbidden-edits", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    repos_dirs = [Path(p) for p in args.repos_dir] or [
        ROOT / ".pri-workspace" / "demo-repos",
        ROOT / ".pri-workspace" / "swebench-screen-repos",
    ]
    cases = rq2.read_jsonl(Path(args.pairing))
    if args.case_id:
        wanted = set(args.case_id)
        cases = [case for case in cases if case["case_id"] in wanted]

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    Path(args.worktrees_dir).mkdir(parents=True, exist_ok=True)
    write_json(Path(args.output_dir) / "run_manifest.json", {
        "source_run_dir": args.source_run_dir,
        "pairing": args.pairing,
        "test_timeout_s": args.test_timeout_s,
        "max_pass_to_pass": args.max_pass_to_pass,
        "require_pass_to_pass": args.require_pass_to_pass,
        "case_count": len(cases),
        "forbid_forbidden_edits": args.forbid_forbidden_edits,
    })

    for case in cases:
        print(f"[RQ2-B-replay] {case['case_id']} repo={case['repo']}", flush=True)
        result = replay_case(case, args, repos_dirs)
        ev = result.get("evaluation", {})
        print(f"      status={ev.get('status')} solved={ev.get('solved')}", flush=True)
        summarize(Path(args.output_dir))

    print(json.dumps(summarize(Path(args.output_dir)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
