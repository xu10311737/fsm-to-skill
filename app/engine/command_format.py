"""Shell-aware command rendering for Agent step prompts."""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def command_context_from_config(config: dict[str, Any] | None,
                                main_path: str | Path) -> dict[str, str]:
    cfg = config or {}
    shell_cfg = cfg.get("shell_tool") or {}
    return {
        "python_path": str(cfg.get("python_path") or sys.executable or "python"),
        "shell": str(shell_cfg.get("shell") or "auto"),
        "main_path": str(main_path),
    }


def resolve_shell(shell: str | None) -> str:
    value = (shell or "auto").strip().lower()
    if value == "auto":
        return "powershell" if os.name == "nt" else "bash"
    if value == "cmd":
        return "powershell" if os.name == "nt" else "bash"
    if value in ("powershell", "pwsh", "bash", "sh"):
        return value
    return "powershell" if os.name == "nt" else "bash"


def format_step_command(context: dict[str, Any] | None,
                        task_id: str,
                        step_id: str,
                        step_param: dict[str, Any]) -> str:
    """Render a single-line, directly executable Agent step command.

    The command is platform-aware:
      - the interpreter defaults to ``python`` on Windows and ``python3`` on
        POSIX (bash/sh) when ``python_path`` is not supplied,
      - quoting follows the resolved shell (PowerShell vs POSIX),
      - node inputs travel via ``--step-param-b64 '<base64>'`` so the JSON
        never needs quoting/escaping on any shell,
      - everything stays on one line (no POSIX ``\\`` continuations).
    """
    ctx = context or {}
    shell = resolve_shell(ctx.get("shell"))
    posix = shell in ("bash", "sh")
    quote = _quote_posix if posix else _quote_powershell
    python_path = str(ctx.get("python_path") or "")
    if not python_path:
        python_path = "python3" if posix else "python"
    main_path = str(ctx.get("main_path") or (Path.cwd() / "main.py"))
    task_text = task_id or "<task-id>"
    payload = json.dumps(step_param or {}, ensure_ascii=False,
                         separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    # The interpreter must be invoked with PowerShell's call operator ``&``
    # when it is a path (e.g. ``& 'C:\\Python314\\python.exe' 'main.py' ...``).
    # Two adjacent quoted string literals are *not* a valid PS command.
    if posix:
        interp = python_path if not _is_path_like(python_path) else _quote_posix(python_path)
    else:
        # Only use PowerShell's call operator ``&`` when the interpreter is a
        # path (e.g. ``& 'C:\\Python314\\python.exe' 'main.py' ...``). Bare
        # command names like ``python`` must be invoked directly.
        interp = f"& {_quote_powershell(python_path)}" if _is_path_like(python_path) else python_path
    return " ".join([
        interp,
        quote(main_path),
        f"--task-id {task_text}",
        f"--step-id {step_id}",
        f"--step-param-b64 '{b64}'",
    ])


def _quote_posix(value: str) -> str:
    if value == "":
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _is_path_like(value: str) -> bool:
    """Heuristic: does this value look like a path (or contain spaces)?"""
    return "/" in value or "\\" in value or " " in value
