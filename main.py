"""Online fsm to skill Agent entrypoint.

This file is used by the debug Agent shell tool:

    python main.py --task-id <task-id> --step-id <code-id> --step-param <json>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from app import deps
from app.services.agent_runtime import execute_agent_step
from app.services.config_store import load_config


BACKEND_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DAG2SKILL_DATA_DIR",
                               BACKEND_ROOT / "data"))


def _task_path(data_dir: Path, task_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "-", str(task_id or "task")) or "task"
    return Path(data_dir) / "tasks" / f"{safe}.json"


def finalize_terminal(client: str = "cli",
                      data_dir: Path | None = None,
                      task_id: str | None = None) -> str:
    """pi-agent 收尾：若任务停在 terminal prompt，标记 task finished。

    注意：pi-agent driver（driver.mjs）读写的是 data/tasks/<task_id>.json
    （execute_agent_step 也写该文件），而 /api/agent/chat 流程使用
    data/runtime/<safe_id>/... 下的 runtime 文件。因此这里必须直接更新
    data/tasks/<task_id>.json，否则 driver 永远读不到 finished=true，
    会陷入死循环。
    """
    from app.main import _terminal_prompt_statuses
    base = Path(data_dir or DATA_DIR)
    if not task_id:
        return "missing task-id"
    task_file = _task_path(base, task_id)
    if not task_file.exists():
        return "no-task-file"
    try:
        state = json.loads(task_file.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return "bad-task-file"
    if state.get("finished"):
        return "already-finished"
    waiting = state.get("waiting-node")
    if not waiting:
        return "not-waiting"
    workflow = state.get("workflow") or {}
    statuses = _terminal_prompt_statuses(workflow, str(waiting))
    if not statuses:
        return "not-terminal"
    node_statuses = state.setdefault("node-statuses", {})
    node_statuses[str(waiting)] = "success"
    node_statuses.update(statuses)
    state["finished"] = True
    state["waiting-node"] = None
    state["last-prompt"] = None
    state.pop("resume", None)
    state["updated-at"] = time.time()
    task_file.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return "finalized"


def main(task_id: str, step_id: str, step_param: dict) -> str:
    cfg = load_config(DATA_DIR / "config.yaml")
    code_service = deps.build_code_service(cfg)
    result = execute_agent_step(DATA_DIR, task_id, step_id, step_param,
                                code_service)
    if result.get("status") == "waiting":
        return result.get("prompt", "")
    if result.get("status") == "completed":
        return result.get("message", "任务已完成")
    return json.dumps(result, ensure_ascii=False, indent=2)


def _cli() -> int:
    # 强制 stdout/stderr 使用 UTF-8，避免 Windows 下 GBK 编码导致 driver 侧乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, LookupError):
        pass
    parser = argparse.ArgumentParser(
        description="fsm to skill 在线调试 Agent 入口")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--step-id", default="")
    parser.add_argument("--step-param")
    parser.add_argument("--step-param-b64")
    parser.add_argument("--finalize", action="store_true")
    ns = parser.parse_args()
    try:
        if ns.finalize:
            output = finalize_terminal(data_dir=DATA_DIR, task_id=ns.task_id)
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
            return 0
        if ns.step_param_b64:
            padded = ns.step_param_b64 + "=" * (-len(ns.step_param_b64) % 4)
            step_param_text = base64.urlsafe_b64decode(
                padded.encode("ascii")).decode("utf-8")
        elif ns.step_param:
            step_param_text = ns.step_param
        else:
            parser.error("--step-param 或 --step-param-b64 必须提供一个")
        step_param = json.loads(step_param_text)
    except json.JSONDecodeError as exc:
        parser.error(f"step 参数不是合法 JSON: {exc}")
    except (ValueError, UnicodeDecodeError) as exc:
        parser.error(f"--step-param-b64 不是合法 base64 JSON: {exc}")
    try:
        output = main(ns.task_id, ns.step_id, step_param)
    except BaseException as exc:  # noqa: BLE001
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
