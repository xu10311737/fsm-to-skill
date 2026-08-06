"""Code 节点子进程执行器（PRD 4.2 + 第 7 章）。

每次执行在独立子进程中运行用户脚本的 ``main(**args)``，通过
stdout 标记段回传 JSON 结果，保证：进程隔离、超时强杀、异常分类、
stdout/stderr 捕获、返回值 JSON 序列化校验。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any

from ..engine.naming import python_arg_map

_BEGIN = "<<<DAG2SKILL_RESULT_BEGIN>>>"
_END = "<<<DAG2SKILL_RESULT_END>>>"

_WRAPPER_TEMPLATE = '''# -*- coding: utf-8 -*-
"""自动生成的执行包装器：调用用户 main 并回传 JSON 结果。"""
import inspect
import json
import sys

USER_CODE = {code_literal}
USER_PARAMS = {params_literal}
USER_ARGS = {args_literal}


def _emit(payload):
    sys.stdout.write("\\n{begin}" + json.dumps(payload, ensure_ascii=False)
                     + "{end}\\n")
    sys.stdout.flush()


def _main_wants_params_dict(main):
    try:
        params = list(inspect.signature(main).parameters.values())
    except (TypeError, ValueError):
        return False
    return (
        len(params) == 1
        and params[0].name == "params"
        and params[0].kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )


def _invoke_main(main):
    if _main_wants_params_dict(main):
        return main(USER_PARAMS)
    return main(**USER_ARGS)


def _run():
    ns = {{"__name__": "__user_main__"}}
    exec(compile(USER_CODE, "<user_code>", "exec"), ns)
    main = ns.get("main")
    if not callable(main):
        raise NameError("main 函数未定义")
    return _invoke_main(main)


try:
    _result = _run()
except BaseException as _e:  # noqa: BLE001
    _emit({{"ok": False, "result": None,
           "error_type": type(_e).__name__, "error_message": str(_e)}})
else:
    try:
        _emit({{"ok": True, "result": _result,
               "error_type": None, "error_message": None}})
    except (TypeError, ValueError) as _e:
        _emit({{"ok": False, "result": None, "error_type": "TypeError",
               "error_message": "main 返回值无法 JSON 序列化: " + str(_e)}})
'''


def run_code(code: str, args: dict[str, Any], timeout: int = 30,
             python_path: str | None = None,
             node_id: str = "") -> dict[str, Any]:
    """在独立子进程中执行 code 的 main(**args)。

    返回 {"ok", "result", "stdout", "stderr", "error_type",
          "error_message", "duration_ms"}
    """
    started = time.perf_counter()
    python = python_path or sys.executable
    raw_args = dict(args or {})
    try:
        py_args = python_arg_map(raw_args)
    except ValueError as exc:
        duration = (time.perf_counter() - started) * 1000
        return {
            "ok": False,
            "result": None,
            "stdout": "",
            "stderr": "",
            "error_type": "ValueError",
            "error_message": str(exc),
            "duration_ms": duration,
        }
    wrapper = _WRAPPER_TEMPLATE.format(
        code_literal=json.dumps(code, ensure_ascii=False),
        params_literal=json.dumps(raw_args, ensure_ascii=False),
        args_literal=json.dumps(py_args, ensure_ascii=False),
        begin=_BEGIN,
        end=_END,
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", suffix=".py", prefix="dag2skill_",
                encoding="utf-8", delete=False) as f:
            f.write(wrapper)
            tmp_path = f.name
        try:
            proc = subprocess.run(
                [python, "-X", "utf8", tmp_path],
                capture_output=True, timeout=timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            payload = _extract_payload(stdout)
            if payload is None:
                payload = {
                    "ok": False, "result": None,
                    "error_type": "RuntimeError",
                    "error_message": (
                        "子进程未返回结果（可能异常退出，"
                        f"exit={proc.returncode}）"),
                }
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
            payload = {
                "ok": False, "result": None,
                "error_type": "TimeoutError",
                "error_message": f"脚本执行超过 {timeout} 秒被终止",
            }
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    duration = (time.perf_counter() - started) * 1000
    return {
        "ok": bool(payload.get("ok")),
        "result": payload.get("result"),
        "stdout": _strip_payload(stdout),
        "stderr": stderr,
        "error_type": payload.get("error_type"),
        "error_message": payload.get("error_message"),
        "duration_ms": duration,
    }


def static_check(code: str) -> tuple[bool, str]:
    """语法检查 + main 函数存在性检查。返回 (是否通过, 错误信息)。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e.msg}（第 {e.lineno} 行）"
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return True, ""
    return False, "代码必须定义 main 函数"


def _extract_payload(stdout: str) -> dict[str, Any] | None:
    """从 stdout 中提取最后一个标记段的 JSON 结果。"""
    begin = stdout.rfind(_BEGIN)
    if begin < 0:
        return None
    end = stdout.find(_END, begin)
    if end < 0:
        return None
    raw = stdout[begin + len(_BEGIN):end]
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _strip_payload(stdout: str) -> str:
    """从 stdout 中移除标记段，保留用户真实的打印内容。"""
    while True:
        begin = stdout.find(_BEGIN)
        if begin < 0:
            return stdout
        end = stdout.find(_END, begin)
        if end < 0:
            return stdout[:begin]
        stdout = stdout[:begin] + stdout[end + len(_END):]
