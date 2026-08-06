"""Shell-aware command rendering for Agent step prompts."""
from __future__ import annotations

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
    ctx = context or {}
    main_path = str(ctx.get("main_path") or (Path.cwd() / "main.py"))
    task_text = task_id or "<task-id>"
    return "\n".join([
        f"python {main_path} \\",
        f"--task_id {task_text} \\",
        f"--step-id {step_id} \\",
        "--step-param <下文中实际节点入参>",
    ])


def _quote_posix(value: str) -> str:
    if value == "":
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
