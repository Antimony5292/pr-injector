#!/usr/bin/env python3
"""Headless Claude Code adapter for PR-INJECTOR A/B eval runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def run_git(repo: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout or ""
    except Exception:
        return ""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def terminate_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except Exception:
        pass
    time.sleep(2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except Exception:
        pass


def parse_claude_json(stdout: str) -> dict[str, Any]:
    try:
        obj = json.loads(stdout)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--tools", default="")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5[1m]"))
    parser.add_argument(
        "--permission-mode",
        default=os.environ.get("CLAUDE_PERMISSION_MODE", "dontAsk"),
        choices=["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan"],
    )
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    system_path = Path(args.system).resolve()
    task_path = Path(args.task).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prompt = "\n\n".join(
        [
            "SYSTEM INSTRUCTIONS:",
            read_text(system_path),
            "TASK INSTRUCTIONS:",
            read_text(task_path),
            "Runner note: edit this repository directly. Do not wait for human approval. "
            "When finished, leave only the requested source-code patch in the working tree.",
        ]
    )
    prompt_path = out_path.parent / "claude_prompt.txt"
    stdout_path = out_path.parent / "llm.stdout.json"
    stderr_path = out_path.parent / "llm.stderr.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    claude_bin = Path(os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude")
    if not claude_bin.exists():
        raise SystemExit(f"claude binary not found: {claude_bin}")

    allowed_tools = [
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "Bash(git *)",
        "Bash(python *)",
        "Bash(python3 *)",
        "Bash(pytest *)",
        "Bash(pip *)",
        "Bash(ls *)",
        "Bash(cat *)",
        "Bash(sed *)",
        "Bash(rg *)",
        "Bash(find *)",
    ]
    cmd = [
        str(claude_bin),
        "-p",
        prompt,
        "--model",
        args.model,
        "--permission-mode",
        args.permission_mode,
        "--no-session-persistence",
        "--output-format",
        "json",
    ]
    for tool in allowed_tools:
        cmd.extend(["--allowedTools", tool])

    env = os.environ.copy()
    env["ANTHROPIC_MODEL"] = args.model
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    for key in [
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "CLAUDE_CODE_USE_BEDROCK",
        "AWS_BEARER_TOKEN_BEDROCK",
        "OPENCODE_MODEL",
        "OPENHANDS_MODEL",
    ]:
        env.pop(key, None)

    pre_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    pre_diff = run_git(repo, ["diff"])
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout_s)
        returncode = int(proc.returncode) if proc.returncode is not None else 0
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(proc.pid)
        stdout = ""
        stderr = f"[claude_timeout] timeout_s={args.timeout_s}\n"
        returncode = 124

    stdout_path.write_text(stdout or "", encoding="utf-8", errors="ignore")
    stderr_path.write_text(stderr or "", encoding="utf-8", errors="ignore")
    post_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    post_diff = run_git(repo, ["diff"])
    parsed = parse_claude_json(stdout or "")
    payload = {
        "cmd": [
            str(claude_bin),
            "-p",
            "<prompt omitted>",
            "--model",
            args.model,
            "--permission-mode",
            args.permission_mode,
            "--no-session-persistence",
            "--output-format",
            "json",
        ],
        "cwd": str(repo),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "claude_json": parsed,
        "timeout_s": args.timeout_s,
        "elapsed_s": round(time.time() - started, 3),
        "pre_git_status": pre_status,
        "pre_git_diff": pre_diff,
        "post_git_status": post_status,
        "post_git_diff": post_diff,
        "usage": parsed.get("usage") or parsed.get("total_cost_usd") or {},
        "result": parsed.get("result") or parsed.get("content") or "",
        "env": {
            "CLAUDE_BIN": str(claude_bin),
            "ANTHROPIC_MODEL": args.model,
            "ANTHROPIC_BASE_URL": env.get("ANTHROPIC_BASE_URL"),
            "ANTHROPIC_API_KEY_PRESENT": bool(env.get("ANTHROPIC_API_KEY")),
            "AGENT_MAESTRO_BASE_URL": env.get("AGENT_MAESTRO_BASE_URL"),
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
