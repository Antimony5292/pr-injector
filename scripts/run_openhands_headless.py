#!/usr/bin/env python3
"""Headless OpenHands + AWS Bedrock adapter for PR-INJECTOR RQ2 runs.

The runner matches the generic agent interface consumed by
run_rq2_claude_bedrock_eval.py:

  --repo <repo> --system <system.txt> --task <task.txt> --out <agent_raw.json>

It intentionally reuses the local OpenHands checkout and venv already prepared
for the BenchInject experiments on this machine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any


DEFAULT_OPENHANDS_ROOT = Path("/Users/harmin/Desktop/ManBo/OpenHands")
DEFAULT_VENV_PYTHON = Path("/Users/harmin/Desktop/BenchInject/.venv_openhands/bin/python")
DEFAULT_REPO_PYTHON = Path(
    "/Users/harmin/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
)
DEFAULT_MODEL = "bedrock/deepseek.v3.2"


def ensure_openhands_python() -> None:
    """Re-exec into the OpenHands venv when the RQ2 runner launches python3."""

    if DEFAULT_VENV_PYTHON.exists() and Path(sys.executable) != DEFAULT_VENV_PYTHON:
        os.execv(str(DEFAULT_VENV_PYTHON), [str(DEFAULT_VENV_PYTHON), *sys.argv])


ensure_openhands_python()


def normalize_bedrock_model(model: str | None) -> str:
    raw = (model or "").strip() or DEFAULT_MODEL
    if raw.startswith("amazon-bedrock/"):
        return "bedrock/" + raw.split("/", 1)[1]
    return raw


def run_git(repo: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        return completed.stdout or ""
    except Exception:
        return ""


def json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return [json_safe(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        return repr(obj)


def repo_python_shims(out_dir: Path) -> Path:
    """Force agent shell commands to use the same stable Python binary.

    OpenHands' local CLI runtime can otherwise inherit user PATH entries such
    as /Users/harmin/bin/python, which currently points at Python 3.14. That
    has caused noisy dependency/test behavior during RQ2 smoke runs.
    """

    shim_dir = out_dir / "repo_python_shims"
    shim_dir.mkdir(parents=True, exist_ok=True)
    python_bin = DEFAULT_REPO_PYTHON if DEFAULT_REPO_PYTHON.exists() else DEFAULT_VENV_PYTHON
    for name in ("python", "python3"):
        shim = shim_dir / name
        shim.write_text(f"#!/usr/bin/env bash\nexec {python_bin} \"$@\"\n", encoding="utf-8")
        shim.chmod(0o755)
    return shim_dir


def event_text(event: Any) -> str:
    if isinstance(event, dict):
        parts: list[str] = []
        for key in ("content", "thought", "command", "message", "error"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
        for key in ("args", "observation", "extras"):
            value = event.get(key)
            if isinstance(value, dict):
                nested = event_text(value)
                if nested:
                    parts.append(nested)
        return "\n".join(parts)
    if isinstance(event, list):
        return "\n".join(event_text(item) for item in event)
    return ""


def history_to_dicts(history: Any, event_to_dict: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in history or []:
        if isinstance(item, dict):
            rows.append(json_safe(item))
            continue
        try:
            rows.append(json_safe(event_to_dict(item)))
        except Exception:
            rows.append({"repr": repr(item)})
    return rows


async def run_openhands(
    *,
    repo: Path,
    prompt: str,
    model: str,
    aws_region: str,
    runtime: str,
    max_iterations: int,
    timeout_s: int,
    out_dir: Path,
) -> tuple[int, dict[str, Any], str]:
    openhands_root = Path(os.environ.get("OPENHANDS_ROOT", str(DEFAULT_OPENHANDS_ROOT))).resolve()
    if str(openhands_root) not in sys.path:
        sys.path.insert(0, str(openhands_root))

    import openhands.agenthub  # noqa: F401
    from openhands.core.config import AgentConfig, LLMConfig, OpenHandsConfig, SandboxConfig
    from openhands.core.main import run_controller
    from openhands.events.action import MessageAction
    from openhands.events.serialization.event import event_to_dict
    from openhands.resolver.utils import codeact_user_response

    sid = f"prinjector-rq2-openhands-{uuid.uuid4().hex[:12]}"
    file_store_path = out_dir / "openhands_file_store"
    cache_dir = out_dir / "openhands_cache"
    python_shims = repo_python_shims(out_dir)
    runtime_path = f"{python_shims}:{os.environ.get('PATH', '')}"
    file_store_path.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    config = OpenHandsConfig(
        default_agent="CodeActAgent",
        runtime=runtime,
        file_store="local",
        file_store_path=str(file_store_path),
        enable_browser=False,
        workspace_base=str(repo),
        workspace_mount_path_in_sandbox=str(repo),
        cache_dir=str(cache_dir),
        run_as_openhands=False,
        max_iterations=max_iterations,
        debug=False,
    )
    llm_request_timeout = min(300, max(60, timeout_s // 6))
    config.set_llm_config(
        LLMConfig(
            model=model,
            aws_region_name=aws_region,
            num_retries=5,
            retry_min_wait=3,
            retry_max_wait=60,
            timeout=llm_request_timeout,
            temperature=0,
            max_output_tokens=4096,
            disable_vision=True,
            disable_stop_word=True,
            native_tool_calling=False,
            drop_params=True,
            modify_params=True,
        )
    )
    config.set_agent_config(
        AgentConfig(
            enable_browsing=False,
            enable_jupyter=False,
            enable_mcp=False,
            enable_llm_editor=False,
            enable_editor=True,
            enable_cmd=True,
            enable_finish=True,
            enable_plan_mode=False,
            runtime=runtime,
        )
    )
    config.sandbox = SandboxConfig(
        timeout=max(120, timeout_s),
        runtime_startup_env_vars={
            "AWS_REGION": aws_region,
            "AWS_DEFAULT_REGION": aws_region,
            "AWS_PROFILE": os.environ.get("AWS_PROFILE", "default"),
            "PATH": runtime_path,
            "PYTHON_BIN": str(DEFAULT_REPO_PYTHON if DEFAULT_REPO_PYTHON.exists() else DEFAULT_VENV_PYTHON),
            "PYTHONNOUSERSITE": "1",
            "PYTHONFAULTHANDLER": "1",
        },
        trusted_dirs=[str(repo)],
    )

    state = await asyncio.wait_for(
        run_controller(
            config=config,
            initial_user_action=MessageAction(content=prompt),
            sid=sid,
            headless_mode=True,
            fake_user_response_fn=codeact_user_response,
        ),
        timeout=timeout_s,
    )

    history = history_to_dicts(getattr(state, "history", []) if state else [], event_to_dict)
    agent_rows = [
        row for row in history if row.get("source") == "agent" and row.get("action") != "system"
    ]
    result_text = "\n\n".join(event_text(row) for row in agent_rows if event_text(row).strip())
    payload = {
        "sid": sid,
        "agent_state": repr(getattr(state, "agent_state", None)) if state else None,
        "last_error": str(getattr(state, "last_error", "") or "") if state else "",
        "metrics": json_safe(getattr(state, "metrics", None)) if state else None,
        "history": history,
    }
    last_error = payload["last_error"]
    agent_state = payload["agent_state"] or ""
    returncode = 1 if "ERROR" in agent_state or last_error else 0
    return returncode, payload, result_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--system", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout-s", type=int, default=1800)
    parser.add_argument("--tools", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--aws-region", default=os.environ.get("AWS_REGION", "us-west-2"))
    parser.add_argument("--aws-profile", default=os.environ.get("AWS_PROFILE", "default"))
    parser.add_argument("--runtime", default=os.environ.get("OPENHANDS_RUNTIME", "cli"))
    parser.add_argument("--max-iterations", type=int, default=int(os.environ.get("OPENHANDS_MAX_ITERATIONS", "100")))
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    system_path = Path(args.system).resolve()
    task_path = Path(args.task).resolve()
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    model = normalize_bedrock_model(
        args.model
        or os.environ.get("OPENHANDS_MODEL")
        or os.environ.get("BEDROCK_MODEL_ID")
    )
    if not model.startswith("bedrock/"):
        raise SystemExit(f"Refusing non-Bedrock OpenHands model: {model}")

    for key in [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "OPENROUTER_API_KEY",
        "CODEX_API_KEY",
        "GOOGLE_API_KEY",
    ]:
        os.environ.pop(key, None)
    os.environ["AWS_REGION"] = args.aws_region
    os.environ["AWS_DEFAULT_REGION"] = args.aws_region
    os.environ["AWS_REGION_NAME"] = args.aws_region
    if args.aws_profile:
        os.environ["AWS_PROFILE"] = args.aws_profile
    os.environ["DESIRED_NUM_WARM_SERVERS"] = "0"
    os.environ["LOCAL_WORKSPACE_BASE"] = str(repo)
    os.environ["LITELLM_LOG"] = "ERROR"
    shim_dir = repo_python_shims(out_dir)
    os.environ["PATH"] = f"{shim_dir}:{os.environ.get('PATH', '')}"
    os.environ["PYTHON_BIN"] = str(DEFAULT_REPO_PYTHON if DEFAULT_REPO_PYTHON.exists() else DEFAULT_VENV_PYTHON)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PYTHONFAULTHANDLER"] = "1"

    stdout_path = out_dir / "llm.stdout.txt"
    stderr_path = out_dir / "llm.stderr.txt"
    result_path = out_dir / "llm.result.txt"
    history_path = out_dir / "openhands.history.json"
    prompt_path = out_dir / "openhands_prompt.txt"

    prompt = "\n\n".join(
        [
            "SYSTEM INSTRUCTIONS:",
            system_path.read_text(encoding="utf-8", errors="ignore"),
            "TASK INSTRUCTIONS:",
            task_path.read_text(encoding="utf-8", errors="ignore"),
            "Runner note: edit this repository directly. Do not wait for human approval. "
            "When finished, leave only the source-code repair patch in the working tree.",
            f"Runner path note: your current working directory is exactly {repo}. "
            "Use relative paths from this directory and do not prefix paths with /workspace.",
            "Python note: use `python` or `python3` from PATH; do not install dependencies unless a test command clearly fails only because a package is missing.",
            "Forbidden-file note: do not create, edit, delete, or rename tests, test scripts, fixtures, config files, CI files, or temporary test_*.py files inside this repository. If you need a scratch script, put it under /tmp.",
            "Environment note: do not replace the repository environment, upgrade Python, or run broad dependency upgrades. "
            "If a test cannot run because the local interpreter or dependency set is unavailable, continue by static analysis and edit only source files.",
        ]
    )
    prompt_path.write_text(prompt, encoding="utf-8")

    pre_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    pre_diff = run_git(repo, ["diff"])

    start = time.time()
    returncode = 0
    timed_out = False
    stderr = ""
    result_text = ""
    state_payload: dict[str, Any] = {}

    try:
        returncode, state_payload, result_text = asyncio.run(
            run_openhands(
                repo=repo,
                prompt=prompt,
                model=model,
                aws_region=args.aws_region,
                runtime=args.runtime,
                max_iterations=args.max_iterations,
                timeout_s=args.timeout_s,
                out_dir=out_dir,
            )
        )
    except asyncio.TimeoutError:
        timed_out = True
        returncode = 124
        stderr = f"[openhands_timeout] timeout_s={args.timeout_s}\n"
    except Exception as exc:
        returncode = 1
        stderr = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8", errors="ignore")
    result_path.write_text(result_text or "", encoding="utf-8", errors="ignore")
    history_path.write_text(json.dumps(json_safe(state_payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    post_status = run_git(repo, ["status", "--porcelain=v1", "-uno"])
    post_diff = run_git(repo, ["diff"])
    produced_source_diff = bool(post_diff.strip())
    last_error = str(state_payload.get("last_error") or "")
    agent_state = str(state_payload.get("agent_state") or "")
    error_text = f"{stderr}\n{last_error}".lower()
    if timed_out or returncode == 124:
        failure_class = "timeout"
        retryable_infra = True
    elif any(
        marker in error_text
        for marker in (
            "apiconnectionerror",
            "connectionerror",
            "readtimeout",
            "connecttimeout",
            "throttlingexception",
            "serviceunavailable",
            "modeltimeoutexception",
        )
    ):
        failure_class = "bedrock_transient"
        retryable_infra = True
    elif returncode != 0:
        failure_class = "openhands_agent_error"
        retryable_infra = False
    else:
        failure_class = None
        retryable_infra = False
    soft_failure = (
        returncode != 0
        and produced_source_diff
        and not stderr
        and not last_error
        and "ERROR" not in agent_state
        and not result_text.lower().strip().startswith("[openhands_timeout]")
    )
    effective_returncode = 0 if soft_failure else returncode

    payload = {
        "cmd": [str(DEFAULT_VENV_PYTHON), *sys.argv],
        "cwd": str(repo),
        "returncode": effective_returncode,
        "raw_returncode": returncode,
        "timed_out": timed_out,
        "soft_success_from_diff": soft_failure,
        "failure_class": failure_class,
        "retryable_infra": retryable_infra,
        "last_error": last_error,
        "agent_state": agent_state,
        "stdout": "",
        "stderr": stderr,
        "result": result_text or "",
        "timeout_s": args.timeout_s,
        "elapsed_s": round(time.time() - start, 3),
        "pre_git_status": pre_status,
        "pre_git_diff": pre_diff,
        "post_git_status": post_status,
        "post_git_diff": post_diff,
        "env": {
            "OPENHANDS_MODEL": model,
            "OPENHANDS_RUNTIME": args.runtime,
            "OPENHANDS_ROOT": str(os.environ.get("OPENHANDS_ROOT", DEFAULT_OPENHANDS_ROOT)),
            "AWS_REGION": os.environ.get("AWS_REGION"),
            "AWS_PROFILE": os.environ.get("AWS_PROFILE"),
            "PATH_PREFIX": str(shim_dir),
            "PYTHON_BIN": os.environ.get("PYTHON_BIN"),
            "OPENAI_API_KEY_PRESENT": bool(os.environ.get("OPENAI_API_KEY")),
            "ANTHROPIC_API_KEY_PRESENT": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "GOOGLE_API_KEY_PRESENT": bool(os.environ.get("GOOGLE_API_KEY")),
        },
        "openhands_history_path": str(history_path),
    }
    out_path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Return code: {effective_returncode}")
    raise SystemExit(effective_returncode)


if __name__ == "__main__":
    main()
