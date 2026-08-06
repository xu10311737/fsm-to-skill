"""Online fsm to skill Agent entrypoint.

This file is used by the debug Agent shell tool:

    python main.py --task-id <task-id> --step-id <code-id> --step-param <json>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from app import deps
from app.services.agent_runtime import execute_agent_step
from app.services.config_store import load_config


BACKEND_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DAG2SKILL_DATA_DIR",
                               BACKEND_ROOT / "data"))


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
    parser = argparse.ArgumentParser(
        description="fsm to skill 在线调试 Agent 入口")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--step-id", required=True)
    parser.add_argument("--step-param")
    parser.add_argument("--step-param-b64")
    ns = parser.parse_args()
    try:
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
