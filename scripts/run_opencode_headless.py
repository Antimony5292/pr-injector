#!/usr/bin/env python3
"""Headless OpenCode + AWS Bedrock adapter for PR-INJECTOR RQ2 runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "amazon-bedrock/deepseek.v3.2"


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


def collect_strings(obj: Any, keys: set[str], out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str) and value.strip():
                out.append(value)
            else:
                collect_strings(value, keys, out)
    elif isinstance(obj, list):
        for item in obj:
            collect_strings(item, keys, out)


def extract_text_from_jsonl(stdout: str) -> str:
    parts: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            parts.append(line)
            continue
        collect_strings(obj, {"text", "content", "message", "output"}, parts)
    return "\n".join(part for part in parts if part.strip())


def extract_error_from_jsonl(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "error":
            continue
        error = obj.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                return data["message"]
            if isinstance(error.get("message"), str):
                return error["message"]
            if isinstance(error.get("name"), str):
                return error["name"]
        return json.dumps(obj, ensure_ascii=False)
    return ""


def build_env(model: str, aws_region: str, aws_profile: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "OPENROUTER_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ]:
        env.pop(key, None)
    if env.get("AWS_BEARER_TOKEN_BEDROCK") and env.get("OPENCODE_ALLOW_BEDROCK_BEARER") != "1":
        env.pop("AWS_BEARER_TOKEN_BEDROCK", None)

    env["AWS_REGION"] = aws_region
    env["AWS_DEFAULT_REGION"] = aws_region
    env["AWS_PROFILE"] = aws_profile
    env["AWS_SDK_LOAD_CONFIG"] = "1"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "1"
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
        {
            "$schema": "https://opencode.ai/config.json",
            "model": model,
            "provider": {
                "amazon-bedrock": {
                    "options": {
                        "region": aws_region,
                        "profile": aws_profile,
                    }
                }
            },
        },
        ensure_ascii=False,
    )
    return env


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--tools", default="")
    parser.add_argument("--model", default=os.environ.get("OPENCODE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE", "default"))
    args = parser.parse_args()

    if not args.model.startswith("amazon-bedrock/"):
        raise SystemExit(f"Refusing non-Bedrock OpenCode model: {args.model}")

    repo = Path(args.repo).resolve()
    system_path = Path(args.system).resolve()
    task_path = Path(args.task).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prompt = "\n\n".join(
        [
            "SYSTEM INSTRUCTIONS:",
            system_path.read_text(encoding="utf-8", errors="ignore"),
            "TASK INSTRUCTIONS:",
            task_path.read_text(encoding="utf-8", errors="ignore"),
            "Runner note: edit this repository directly. Do not wait for human approval. "
            "When finished, leave only the source-code repair patch in the working tree.",
            "Hard evaluation rule: never create, edit, delete, rename, stage, or commit tests, "
            "test fixtures, benchmark metadata, CI files, or project test configuration. "
            "Use /tmp for scratch files. Any forbidden-file change invalidates the run.",
        ]
    )
    prompt_path = out_path.parent / "opencode_prompt.txt"
    stdout_path = out_path.parent / "llm.stdout.jsonl"
    stderr_path = out_path.parent / "llm.stderr.txt"
    result_path = out_path.parent / "llm.result.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    opencode_bin = os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
    if not opencode_bin:
        raise SystemExit("opencode binary not found")

    cmd = [
        opencode_bin,
        "run",
        "--pure",
        "--dir",
        str(repo),
        "--model",
        args.model,
        "--format",
        "json",
        "--dangerously-skip-permissions",
        "--title",
        "pr-injector-rq2",
        "Follow the attached PR-INJECTOR RQ2 repair instructions exactly.",
        f"--file={prompt_path}",
    ]
    env = build_env(args.model, args.aws_region, args.aws_profile)
    state_root = out_path.parent / ".opencode_state"
    for name in ("data", "state", "cache"):
        (state_root / name).mkdir(parents=True, exist_ok=True)
    env["XDG_DATA_HOME"] = str(state_root / "data")
    env["XDG_STATE_HOME"] = str(state_root / "state")
    env["XDG_CACHE_HOME"] = str(state_root / "cache")
    env["OPENCODE_DISABLE_TELEMETRY"] = "1"

    pre_status = run_git(repo, ["status", "--porcelain=v1"])
    pre_diff = run_git(repo, ["diff"])
    start = time.time()
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
    watchdog_fired = False

    def watchdog_kill() -> None:
        nonlocal watchdog_fired
        watchdog_fired = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass

    watchdog = threading.Timer(args.timeout_s, watchdog_kill)
    watchdog.daemon = True
    watchdog.start()
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout_s)
        returncode = int(proc.returncode) if proc.returncode is not None else 0
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        time.sleep(2)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        stdout = ""
        stderr = f"[opencode_timeout] timeout_s={args.timeout_s}\n"
        returncode = 124
    finally:
        watchdog.cancel()
    if watchdog_fired and returncode == 0:
        timed_out = True
        returncode = 124
        stderr = (stderr or "") + f"\n[opencode_watchdog_timeout] timeout_s={args.timeout_s}\n"

    opencode_error = extract_error_from_jsonl(stdout or "")
    result_text = extract_text_from_jsonl(stdout or "")
    if opencode_error and returncode == 0:
        returncode = 1
    if opencode_error and not result_text:
        result_text = opencode_error
    stdout_path.write_text(stdout or "", encoding="utf-8", errors="ignore")
    stderr_path.write_text(stderr or "", encoding="utf-8", errors="ignore")
    result_path.write_text(result_text, encoding="utf-8", errors="ignore")

    post_status = run_git(repo, ["status", "--porcelain=v1"])
    post_diff = run_git(repo, ["diff", "--binary", "HEAD", "--"])
    payload = {
        "cmd": cmd,
        "cwd": str(repo),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "opencode_error": opencode_error,
        "result": result_text,
        "timeout_s": args.timeout_s,
        "elapsed_s": round(time.time() - start, 3),
        "pre_git_status": pre_status,
        "pre_git_diff": pre_diff,
        "post_git_status": post_status,
        "post_git_diff": post_diff,
        "env": {
            "OPENCODE_MODEL": args.model,
            "OPENCODE_BIN": opencode_bin,
            "AWS_REGION": env.get("AWS_REGION"),
            "AWS_PROFILE": env.get("AWS_PROFILE"),
            "XDG_DATA_HOME": env.get("XDG_DATA_HOME"),
            "XDG_STATE_HOME": env.get("XDG_STATE_HOME"),
            "XDG_CACHE_HOME": env.get("XDG_CACHE_HOME"),
            "OPENAI_API_KEY_PRESENT": bool(env.get("OPENAI_API_KEY")),
            "ANTHROPIC_API_KEY_PRESENT": bool(env.get("ANTHROPIC_API_KEY")),
            "AWS_BEARER_TOKEN_BEDROCK_PRESENT": bool(env.get("AWS_BEARER_TOKEN_BEDROCK")),
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
