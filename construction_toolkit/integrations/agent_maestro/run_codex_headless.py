#!/usr/bin/env python3
"""Headless Codex CLI adapter for PR-INJECTOR RQ2 runs."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any


CODEX_BIN = Path.home() / ".local" / "bin" / "codex"


def resolve_codex_model(requested: str, env: dict[str, str] | None = None) -> str:
    if requested:
        return requested
    environment = env or os.environ
    codex_home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
    config = codex_home / "config.toml"
    try:
        payload = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "default-codex"
    return str(payload.get("model") or "default-codex")


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


def collect_codex_error(stdout: str) -> str:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "error" and isinstance(obj.get("message"), str):
            return obj["message"]
        error = obj.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return ""


def collect_codex_usage(stdout: str) -> dict[str, int]:
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for line in stdout.splitlines():
        try:
            obj: Any = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "turn.completed":
            continue
        usage = obj.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--tools", default="")
    parser.add_argument("--model", default=os.environ.get("CODEX_MODEL", ""))
    parser.add_argument("--profile", default=os.environ.get("CODEX_PROFILE", "agent-maestro"))
    parser.add_argument(
        "--reasoning-effort",
        default=os.environ.get("CODEX_REASONING_EFFORT", "high"),
        choices=["minimal", "low", "medium", "high", "xhigh"],
    )
    parser.add_argument(
        "--runner-note",
        default=(
            "Runner note: edit this repository directly. Do not wait for human approval. "
            "When finished, leave only the requested source-code patch in the working tree."
        ),
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
            args.runner_note,
        ]
    )
    prompt_path = out_path.parent / "codex_prompt.txt"
    stdout_path = out_path.parent / "llm.stdout.jsonl"
    stderr_path = out_path.parent / "llm.stderr.txt"
    result_path = out_path.parent / "llm.result.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    codex_bin = Path(os.environ.get("CODEX_BIN") or shutil.which("codex") or CODEX_BIN)
    if not codex_bin.exists():
        raise SystemExit(f"codex binary not found: {codex_bin}")

    cmd = [
        str(codex_bin),
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "-C",
        str(repo),
        "-o",
        str(result_path),
    ]
    if args.profile:
        cmd.extend(["--profile", args.profile])
    if args.model:
        cmd.extend(["-m", args.model])
    cmd.extend(["-c", f'model_reasoning_effort="{args.reasoning_effort}"'])
    cmd.append("-")

    env = os.environ.copy()
    env["CODEX_DISABLE_AUTO_UPDATE"] = "1"
    resolved_model = resolve_codex_model(args.model, env)
    for key in [
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "OPENROUTER_API_KEY",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "ANTHROPIC_MODEL",
        "OPENHANDS_MODEL",
        "OPENCODE_MODEL",
        "OPENCODE_CONFIG_CONTENT",
    ]:
        env.pop(key, None)

    pre_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    pre_diff = run_git(repo, ["diff"])
    start = time.time()
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    timed_out = False
    try:
        stdout, stderr = proc.communicate(prompt, timeout=args.timeout_s)
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
        stderr = f"[codex_timeout] timeout_s={args.timeout_s}\n"
        returncode = 124

    stdout_path.write_text(stdout or "", encoding="utf-8", errors="ignore")
    stderr_path.write_text(stderr or "", encoding="utf-8", errors="ignore")

    post_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    post_diff = run_git(repo, ["diff"])
    codex_error = collect_codex_error(stdout or "")
    usage = collect_codex_usage(stdout or "")
    payload = {
        "cmd": cmd,
        "cwd": str(repo),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "codex_error": codex_error,
        "usage": usage,
        "result": result_path.read_text(encoding="utf-8", errors="ignore") if result_path.exists() else "",
        "timeout_s": args.timeout_s,
        "elapsed_s": round(time.time() - start, 3),
        "pre_git_status": pre_status,
        "pre_git_diff": pre_diff,
        "post_git_status": post_status,
        "post_git_diff": post_diff,
        "env": {
            "CODEX_BIN": str(codex_bin),
            "CODEX_MODEL": args.model,
            "CODEX_MODEL_RESOLVED": resolved_model,
            "CODEX_REASONING_EFFORT": args.reasoning_effort,
            "CODEX_PROFILE": args.profile,
            "CODEX_AGENT_MAESTRO": env.get("CODEX_AGENT_MAESTRO"),
            "AGENT_MAESTRO_BASE_URL": env.get("AGENT_MAESTRO_BASE_URL"),
            "AGENT_MAESTRO_OPENAI_BASE_URL": env.get("AGENT_MAESTRO_OPENAI_BASE_URL"),
            "OPENAI_API_KEY_PRESENT": bool(env.get("OPENAI_API_KEY")),
            "AGENT_MAESTRO_API_KEY_PRESENT": bool(env.get("AGENT_MAESTRO_API_KEY")),
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
