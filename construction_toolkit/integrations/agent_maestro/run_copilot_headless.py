#!/usr/bin/env python3
"""Headless GitHub Copilot CLI adapter for PR-INJECTOR RQ2 runs."""

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


def extract_copilot_error(stdout: str) -> str:
    last_error = ""
    for line in stdout.splitlines():
        try:
            obj: Any = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for key in ("error", "message"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                last_error = value
            elif isinstance(value, dict) and isinstance(value.get("message"), str):
                last_error = value["message"]
    return last_error


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--tools", default="")
    parser.add_argument("--model", default=os.environ.get("COPILOT_MODEL", "gpt-5.3-codex"))
    parser.add_argument("--max-ai-credits", type=int, default=int(os.environ.get("COPILOT_MAX_AI_CREDITS", "30")))
    parser.add_argument("--effort", default=os.environ.get("COPILOT_REASONING_EFFORT", "high"))
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
            "Runner note: edit this repository directly. Do not wait for human approval. When finished, leave only the source-code repair patch in the working tree.",
        ]
    )
    prompt_path = out_path.parent / "copilot_prompt.txt"
    stdout_path = out_path.parent / "llm.stdout.jsonl"
    stderr_path = out_path.parent / "llm.stderr.txt"
    share_path = out_path.parent / "copilot_session.md"
    prompt_path.write_text(prompt, encoding="utf-8")

    copilot_bin = os.environ.get("COPILOT_BIN") or shutil.which("copilot")
    if not copilot_bin:
        raise SystemExit("copilot binary not found")

    cmd = [
        copilot_bin,
        "-p",
        prompt,
        "-C",
        str(repo),
        "--model",
        args.model,
        "--output-format",
        "json",
        "--allow-all-tools",
        "--allow-all-paths",
        "--no-ask-user",
        "--no-remote",
        "--no-auto-update",
        "--max-ai-credits",
        str(args.max_ai_credits),
        "--share",
        str(share_path),
    ]
    if args.effort:
        cmd.extend(["--effort", args.effort])

    env = os.environ.copy()
    env["COPILOT_ALLOW_ALL"] = "1"
    env["COPILOT_NO_AUTO_UPDATE"] = "1"
    if os.environ.get("COPILOT_USE_AGENT_MAESTRO", "").lower() in {"1", "true", "yes", "on"}:
        base_url = os.environ.get(
            "AGENT_MAESTRO_OPENAI_BASE_URL",
            os.environ.get("AGENT_MAESTRO_BASE_URL", "http://127.0.0.1:23333").rstrip("/")
            + "/api/openai/v1",
        )
        env["COPILOT_PROVIDER_BASE_URL"] = base_url
        env["COPILOT_PROVIDER_TYPE"] = "openai"
        env["COPILOT_PROVIDER_WIRE_API"] = "responses"
        env["COPILOT_PROVIDER_MAX_OUTPUT_TOKENS"] = os.environ.get("COPILOT_PROVIDER_MAX_OUTPUT_TOKENS", "4096")
        env["COPILOT_PROVIDER_MAX_PROMPT_TOKENS"] = os.environ.get("COPILOT_PROVIDER_MAX_PROMPT_TOKENS", "271790")
        env["COPILOT_MODEL"] = args.model
        if env.get("AGENT_MAESTRO_API_KEY"):
            env["COPILOT_PROVIDER_BEARER_TOKEN"] = env["AGENT_MAESTRO_API_KEY"]

    for key in [
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_MODEL",
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
        stderr = f"[copilot_timeout] timeout_s={args.timeout_s}\n"
        returncode = 124

    stdout_path.write_text(stdout or "", encoding="utf-8", errors="ignore")
    stderr_path.write_text(stderr or "", encoding="utf-8", errors="ignore")

    post_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    post_diff = run_git(repo, ["diff"])
    payload = {
        "cmd": [
            copilot_bin,
            "-p",
            "<prompt omitted>",
            "-C",
            str(repo),
            "--model",
            args.model,
            "--output-format",
            "json",
            "--allow-all-tools",
            "--allow-all-paths",
            "--no-ask-user",
            "--no-remote",
            "--no-auto-update",
            "--max-ai-credits",
            str(args.max_ai_credits),
            "--share",
            str(share_path),
        ],
        "cwd": str(repo),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "copilot_error": extract_copilot_error(stdout or ""),
        "timeout_s": args.timeout_s,
        "elapsed_s": round(time.time() - started, 3),
        "pre_git_status": pre_status,
        "pre_git_diff": pre_diff,
        "post_git_status": post_status,
        "post_git_diff": post_diff,
        "env": {
            "COPILOT_BIN": copilot_bin,
            "COPILOT_MODEL": args.model,
            "COPILOT_USE_AGENT_MAESTRO": env.get("COPILOT_USE_AGENT_MAESTRO"),
            "COPILOT_PROVIDER_BASE_URL": env.get("COPILOT_PROVIDER_BASE_URL"),
            "AGENT_MAESTRO_API_KEY_PRESENT": bool(env.get("AGENT_MAESTRO_API_KEY")),
            "max_ai_credits": args.max_ai_credits,
            "effort": args.effort,
        },
        "share_path": str(share_path),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
