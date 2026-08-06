"""Skill 导出器（PRD 第 8 章）。

将工作流导出为 Skill 包::

    skill-package/
    ├── SKILL.md               说明文档（入参表 / 最终输出 / 结构）
    ├── inference/prompt-N.md  LLM 提示词模板（原始，未渲染）
    ├── scripts/main.py        离线状态机入口
    │       └── <code节点>.py  各 Code 节点脚本
    └── workflow.yaml          工作流定义

规则：存在校验 Error 禁止导出；不自动覆盖已有目录（显式 overwrite
除外）；导出中途失败清理不完整目录。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from ..engine.command_format import format_step_command
from ..engine.topo import topo_sort
from ..validator.validator import validate_workflow


def export_skill(wf: dict, target_dir: str | Path,
                 overwrite: bool = False,
                 command_context: dict[str, Any] | None = None) -> Path:
    """导出工作流为 Skill 包，返回包目录路径。"""
    report = validate_workflow(wf)
    if report["errors"]:
        codes = ", ".join(i["code"] for i in report["errors"])
        raise ValueError(f"工作流存在校验 Error，禁止导出: {codes}")
    target = Path(target_dir)
    if target.exists():
        if not overwrite:
            raise FileExistsError(
                f"目标目录已存在: {target}（如需覆盖请显式确认）")
        shutil.rmtree(target)
    target.mkdir(parents=True)
    try:
        context = _export_command_context(command_context, target)
        _write_skill_md(wf, target)
        _write_workflow_yaml(wf, target)
        _write_agent_interface(wf, target, context)
        _write_inference(wf, target)
        _write_scripts(wf, target, context)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


# ----------------------------------------------------------------------
def _write_skill_md(wf: dict, target: Path) -> None:
    name = wf.get("name") or wf.get("id") or "未命名工作流"
    first_prompt = _first_prompt_content(wf)
    lines = [
        "---",
        f"name: {_yaml_string(name)}",
        'description: ""',
        "---",
        "",
        "# 任务描述",
        "",
        "# 最终输出",
        "",
        "# SOP",
        "",
        "你需要在状态机的指引下,一步步完成任务, let us step by step!",
        "",
        f"**{first_prompt}**",
        "",
        "---",
        "",
    ]
    (target / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _first_prompt_content(wf: dict) -> str:
    nodes = wf.get("nodes", []) or []
    by_id = {node.get("id"): node for node in nodes}
    try:
        order = topo_sort(wf)
    except ValueError:
        order = [node.get("id") for node in nodes]
    for node_id in order:
        node = by_id.get(node_id)
        if node and node.get("type") == "llm":
            return (node.get("config", {}) or {}).get("prompt", "")
    return ""


def _export_command_context(context: dict[str, Any] | None,
                            target: Path) -> dict[str, Any]:
    merged = dict(context or {})
    merged["main_path"] = str((target / "scripts" / "main.py").resolve())
    return merged


def _write_workflow_yaml(wf: dict, target: Path) -> None:
    (target / "workflow.yaml").write_text(
        yaml.safe_dump(wf, allow_unicode=True, sort_keys=False),
        encoding="utf-8")


def _code_input_schema(node: dict) -> list[dict[str, Any]]:
    """导出 Agent 可见的 argparse 风格参数 schema，不暴露旧 DAG source。"""
    schema: list[dict[str, Any]] = []
    for i, spec in enumerate((node.get("config", {}) or {}).get("inputs", []) or []):
        if spec.get("source"):
            continue
        schema.append({
            "name": spec.get("name") or f"arg-{i + 1}",
            "description": spec.get("description") or "",
            "type": spec.get("type") or "string",
            "required": spec.get("required", True) is not False,
        })
    return schema


def _next_code_entries(wf: dict, prompt_id: str,
                       command_context: dict[str, Any] | None = None,
                       task_id: str = "<task-id>") -> list[dict[str, Any]]:
    """从 Prompt 出口继续向后找下一层 Code 入口（跳过非 Code 节点）。"""
    nodes = wf.get("nodes", []) or []
    by_id = {n["id"]: n for n in nodes}
    out_edges: dict[str, list[dict]] = {}
    for edge in wf.get("edges", []) or []:
        if edge.get("source_handle") in ("error", "retry"):
            continue
        out_edges.setdefault(edge.get("source"), []).append(edge)

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue = [e.get("target") for e in out_edges.get(prompt_id, [])]
    while queue:
        nid = queue.pop(0)
        if not nid or nid in seen:
            continue
        seen.add(nid)
        node = by_id.get(nid)
        if not node:
            continue
        if node.get("type") == "code":
            step_param = {
                spec["name"]: f"<{spec['type']}>"
                for spec in _code_input_schema(node)
            }
            found.append({
                "step_id": nid,
                "node_name": node.get("name") or nid,
                "input_schema": _code_input_schema(node),
                "command_template": format_step_command(
                    command_context, task_id, nid, step_param),
            })
            continue
        if node.get("type") in ("llm", "end"):
            continue
        queue.extend(e.get("target") for e in out_edges.get(nid, []))
    return found


def _write_agent_interface(wf: dict, target: Path,
                           command_context: dict[str, Any]) -> None:
    """写出 Agent 交互路由表。

    main.py 是总状态机入口；Code 节点是 Agent 输入进入 DAG 的独立入口，
    Prompt(LLM) 节点是 DAG 内容返回 Agent 的出口。
    """
    nodes = wf.get("nodes", []) or []
    by_id = {n["id"]: n for n in nodes}
    entries = []
    exits = []
    for n in nodes:
        cfg = n.get("config", {}) or {}
        iface = cfg.get("agent_interface", {}) or {}
        if n.get("type") == "code":
            step_param = {
                spec["name"]: f"<{spec['type']}>"
                for spec in _code_input_schema(n)
            }
            entries.append({
                "entry_id": iface.get("entry_id") or n["id"],
                "step_id": iface.get("entry_id") or n["id"],
                "node_id": n["id"],
                "node_name": n.get("name") or n["id"],
                "direction": "agent_to_code",
                "input_schema": _code_input_schema(n),
                "command_template": format_step_command(
                    command_context, "<task-id>", n["id"], step_param),
            })
        elif n.get("type") == "llm":
            exits.append({
                "exit_id": iface.get("exit_id") or n["id"],
                "step_id": iface.get("exit_id") or n["id"],
                "node_id": n["id"],
                "node_name": n.get("name") or n["id"],
                "direction": "prompt_to_agent",
                "prompt_template": cfg.get("prompt", ""),
                "next_code_entries": _next_code_entries(
                    wf, n["id"], command_context),
            })
    routes = [{
        "source": e.get("source"),
        "source_name": by_id.get(e.get("source"), {}).get("name"),
        "target": e.get("target"),
        "target_name": by_id.get(e.get("target"), {}).get("name"),
        "handle": e.get("source_handle", "out"),
    } for e in wf.get("edges", []) or []]
    payload = {
        "version": "1.0",
        "main_thread": "scripts/main.py",
        "workflow_id": wf.get("id"),
        "entries": entries,
        "exits": exits,
        "code_entries": entries,
        "prompt_exits": exits,
        "routes": routes,
        "agent_envelope": {
            "agent_command": (
                format_step_command(
                    command_context, "<task-id>", "<code_node_id>",
                    {"<arg>": "<value>"})),
                "task-id": "<task-id>",
            "step-id": "<actual_code_node_id>",
            "step-param": {"<arg>": "<value>"},
            "step-param-format": "JSON string keyed by Code input_schema.name",
        },
    }
    (target / "agent_interface.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _write_inference(wf: dict, target: Path) -> None:
    llm_nodes = [n for n in wf.get("nodes", []) if n.get("type") == "llm"]
    if not llm_nodes:
        return
    d = target / "inference"
    d.mkdir(exist_ok=True)
    for idx, n in enumerate(llm_nodes, start=1):
        prompt = n.get("config", {}).get("prompt", "")
        (d / f"prompt-{idx}.md").write_text(prompt, encoding="utf-8")


def _write_scripts(wf: dict, target: Path,
                   command_context: dict[str, Any]) -> None:
    d = target / "scripts"
    d.mkdir(exist_ok=True)
    for n in _iter_nodes(wf):
        if n.get("type") == "code":
            code = n.get("config", {}).get("code", "")
            (d / f"{n['id']}.py").write_text(code, encoding="utf-8")
    (d / "main.py").write_text(_render_main_py(wf, command_context),
                               encoding="utf-8")


def _iter_nodes(graph: dict) -> list[dict]:
    out: list[dict] = []
    for node in graph.get("nodes", []) or []:
        out.append(node)
        body = (node.get("config", {}) or {}).get("body") or {}
        out.extend(_iter_nodes(body))
    return out


# ----------------------------------------------------------------------
def _node_var_base(node: dict) -> str:
    raw = str(node.get("name") or node.get("id") or "node").strip()
    raw = re.sub(r"\s+", "-", raw)
    value = re.sub(r"[^0-9A-Za-z_-]", "-", raw)
    value = re.sub(r"-+", "-", value).strip("-")
    if not re.match(r"^[A-Za-z_]", value):
        value = re.sub(r"[^0-9A-Za-z_-]", "-", str(node.get("id") or "node"))
        value = re.sub(r"-+", "-", value).strip("-")
    if not re.match(r"^[A-Za-z_]", value):
        value = f"node-{value or 'output'}"
    return value


def _auto_output_name(node: dict) -> str:
    return f"{_node_var_base(node)}-output"


def _node_output_names(node: dict) -> list[str]:
    ntype = node.get("type")
    cfg = node.get("config", {}) or {}
    if ntype == "code":
        return [o["name"] for o in cfg.get("outputs", []) or []]
    if ntype == "llm":
        return []
    if ntype in ("for", "aggregate"):
        return [_auto_output_name(node)]
    if ntype == "start":
        return [i["name"] for i in cfg.get("inputs", []) or []]
    return []


def _final_output_names(wf: dict) -> list[str]:
    """到达 End 前最后可用的全局输出变量。"""
    by_id = {n["id"]: n for n in wf.get("nodes", [])}
    ends = [n for n in wf.get("nodes", []) if n.get("type") == "end"]
    for e in wf.get("edges", []):
        if e.get("target") in {x["id"] for x in ends}:
            pred = by_id.get(e.get("source"))
            if pred is not None:
                names = _node_output_names(pred)
                if names:
                    return names
    return []


def _render_main_py(wf: dict, command_context: dict[str, Any] | None = None
                    ) -> str:
    """生成 Agent CLI 入口脚本（内嵌工作流定义）。"""
    template = r'''# -*- coding: utf-8 -*-
"""fsm to skill 导出的 Agent 入口。

main.py 是总线程；每个 Code 节点是 Agent 输入入口，每个 Prompt 节点
是返回 Agent 的出口。

用法:
  python3 main.py --task-id task-001 --step-id code-1 --step-param '{"arg-1": "hello"}'
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

WORKFLOW = __WORKFLOW__
COMMAND_CONTEXT = __COMMAND_CONTEXT__
SCRIPTS_DIR = Path(__file__).resolve().parent
TASK_STATE_DIR = SCRIPTS_DIR / ".dag2skill_tasks"
MAX_NODE_EXECUTIONS = 50
IDLE_TIMEOUT_SECONDS = float(__IDLE_TIMEOUT__)
MAX_TASK_RUNTIME_SECONDS = float(__MAX_TASK_RUNTIME__)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def _resolve_shell(shell: str | None) -> str:
    value = (shell or "auto").strip().lower()
    if value == "auto":
        return "powershell" if sys.platform.startswith("win") else "bash"
    if value == "cmd":
        return "powershell" if sys.platform.startswith("win") else "bash"
    if value in ("powershell", "pwsh", "bash", "sh"):
        return value
    return "powershell" if sys.platform.startswith("win") else "bash"


def _quote_posix(value: str) -> str:
    if value == "":
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _format_step_command(task_id: str, step_id: str,
                         step_param: dict[str, Any]) -> str:
    main_path = str((SCRIPTS_DIR / "main.py").resolve())
    task_text = task_id or "<task-id>"
    return "\n".join([
        f"python {main_path} \\",
        f"--task_id {task_text} \\",
        f"--step-id {step_id} \\",
        "--step-param <下文中实际节点入参>",
    ])


def _safe_id(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]", "_", str(value))


def _py_param(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_]", "_", str(value or ""))
    if name and name[0].isdigit():
        name = "_" + name
    return name


def _python_arg_map(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    owners: dict[str, str] = {}
    for raw_name, value in (args or {}).items():
        py_name = _py_param(raw_name)
        if py_name in owners and owners[py_name] != raw_name:
            raise ValueError(
                f"参数名 {owners[py_name]!r} 与 {raw_name!r} 映射到同一个 "
                f"Python 形参 {py_name!r}")
        owners[py_name] = raw_name
        out[py_name] = value
    return out


def _main_wants_params_dict(main) -> bool:
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


def _invoke_main(main, args: dict[str, Any]) -> Any:
    if _main_wants_params_dict(main):
        return main(dict(args or {}))
    return main(**_python_arg_map(args))


class PromptExit(Exception):
    def __init__(self, prompt: str, resume: dict[str, Any] | None = None,
                 node_id: str | None = None):
        super().__init__("Prompt node reached")
        self.prompt = prompt
        self.resume = resume or {}
        self.node_id = node_id


def _task_path(task_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "-", str(task_id or "task"))
    return TASK_STATE_DIR / f"{safe}.json"


def _load_task(task_id: str) -> dict[str, Any]:
    _cleanup_tasks()
    now = time.time()
    path = _task_path(task_id)
    if not path.exists():
        return {
            "task-id": task_id,
            "created-at": now,
            "updated-at": now,
            "variables": {"task-id": task_id},
            "node-statuses": {},
            "finished": False,
        }
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    created = float(data.get("created-at") or now)
    updated = float(data.get("updated-at") or created)
    if now - updated > IDLE_TIMEOUT_SECONDS:
        path.unlink(missing_ok=True)
        raise TimeoutError(
            f"task {task_id} idle timeout after {IDLE_TIMEOUT_SECONDS:g}s")
    if now - created > MAX_TASK_RUNTIME_SECONDS:
        path.unlink(missing_ok=True)
        raise TimeoutError(
            f"task {task_id} exceeded max runtime {MAX_TASK_RUNTIME_SECONDS:g}s")
    data.setdefault("variables", {})["task-id"] = task_id
    data["updated-at"] = now
    return data


def _save_task(state: dict[str, Any]) -> None:
    TASK_STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated-at"] = time.time()
    _task_path(state["task-id"]).write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _set_status(state: dict[str, Any] | None, node_id: str | None,
                status: str) -> None:
    if not state or not node_id:
        return
    state.setdefault("node-statuses", {})[node_id] = status


def _mark_previous_waiting_done(state: dict[str, Any]) -> None:
    waiting = state.get("waiting-node")
    if waiting:
        _set_status(state, waiting, "success")


def _save_waiting(state: dict[str, Any], variables: dict[str, Any],
                  node_id: str | None, prompt: str,
                  resume: dict[str, Any] | None = None) -> None:
    state["variables"] = variables
    state["waiting-node"] = node_id
    state["last-prompt"] = prompt
    _set_status(state, node_id, "waiting")
    if resume:
        state["resume"] = resume
    else:
        state.pop("resume", None)
    state["finished"] = False
    _save_task(state)


def _finish_task(state: dict[str, Any]) -> None:
    state["finished"] = True
    state["waiting-node"] = None
    state["last-prompt"] = None
    _save_task(state)


def _cleanup_tasks() -> None:
    if not TASK_STATE_DIR.exists():
        return
    now = time.time()
    for path in TASK_STATE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            created = float(data.get("created-at") or now)
            updated = float(data.get("updated-at") or created)
        except BaseException:  # noqa: BLE001
            path.unlink(missing_ok=True)
            continue
        if (now - updated > IDLE_TIMEOUT_SECONDS or
                now - created > MAX_TASK_RUNTIME_SECONDS):
            path.unlink(missing_ok=True)


def _node_by_id(node_id: str) -> dict[str, Any] | None:
    return _node_by_id_in({"nodes": WORKFLOW.get("nodes", []) or []}, node_id)


def _node_by_id_in(graph: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for node in graph.get("nodes", []) or []:
        if node.get("id") == node_id:
            return node
        body = (node.get("config", {}) or {}).get("body") or {}
        found = _node_by_id_in(body, node_id)
        if found:
            return found
    return None


def _out_edges(node_id: str, handles: set[str] | None = None) -> list[dict]:
    result = []
    for edge in WORKFLOW.get("edges", []) or []:
        if edge.get("source") != node_id:
            continue
        handle = edge.get("source_handle", "out")
        if handles is not None and handle not in handles:
            continue
        if handle == "retry":
            continue
        result.append(edge)
    return result


def _code_input_specs(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all Code inputs, including workflow-bound variables."""
    schema = []
    cfg = node.get("config", {}) or {}
    for i, spec in enumerate(cfg.get("inputs", []) or []):
        item = {
            "name": spec.get("name") or f"arg-{i + 1}",
            "description": spec.get("description") or "",
            "type": spec.get("type") or "string",
            "required": spec.get("required", True) is not False,
        }
        if spec.get("source"):
            item["source"] = spec["source"]
        schema.append(item)
    return schema


def _input_schema(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [spec for spec in _code_input_specs(node) if not spec.get("source")]


def _next_code_entries(prompt_id: str,
                       graph: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    graph = graph or WORKFLOW
    by_id = {n.get("id"): n for n in graph.get("nodes", []) or []}
    out_map: dict[str, list[dict]] = {}
    for edge in graph.get("edges", []) or []:
        if edge.get("source_handle", "out") in ("error", "retry"):
            continue
        out_map.setdefault(edge.get("source"), []).append(edge)
    found = []
    seen: set[str] = set()
    queue = [e.get("target") for e in out_map.get(prompt_id, [])]
    while queue:
        node_id = queue.pop(0)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        node = by_id.get(node_id)
        if not node:
            continue
        if node.get("type") == "code":
            step_param = {
                spec["name"]: f"<{spec['type']}>"
                for spec in _input_schema(node)
            }
            found.append({
                "step_id": node_id,
                "node_name": node.get("name") or node_id,
                "input_schema": _input_schema(node),
                "command_template": _format_step_command(
                    "<task-id>", node_id, step_param),
            })
            continue
        if node.get("type") in ("llm", "end"):
            continue
        queue.extend(e.get("target") for e in out_map.get(node_id, []))
    return found


def _render(template: str, variables: dict[str, Any]) -> str:
    def _sub(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        value = variables.get(expr, match.group(0))
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return ""
        return str(value)
    return re.sub(r"\{\{\s*(.*?)\s*\}\}", _sub, template or "")


def _check_type(value: Any, type_: str) -> bool:
    if value is None:
        return True
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "list":
        return isinstance(value, list)
    if type_ == "dict":
        return isinstance(value, dict)
    return False


def _coerce_value(value: Any, type_: str) -> Any:
    if value is None:
        return None
    if type_ == "string":
        if isinstance(value, (list, dict)):
            raise TypeError("string 参数不能是 list/dict")
        return value if isinstance(value, str) else str(value)
    if type_ == "int":
        if isinstance(value, bool):
            raise TypeError("int 参数不能是 bool")
        return value if isinstance(value, int) else int(value)
    if type_ == "float":
        if isinstance(value, bool):
            raise TypeError("float 参数不能是 bool")
        return value if isinstance(value, (int, float)) else float(value)
    if type_ in ("list", "dict") and isinstance(value, str):
        value = json.loads(value)
    if not _check_type(value, type_):
        raise TypeError(f"期望 {type_}，实际为 {type(value).__name__}")
    return value


def _step_args(node: dict[str, Any], raw_params: dict[str, Any],
               variables: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_params, dict):
        raise TypeError("--step-param 必须是 JSON object")
    schema = _input_schema(node)
    names = {item["name"] for item in _code_input_specs(node)}
    extra = sorted(set(raw_params) - names)
    if extra:
        raise ValueError(f"--step-param 包含未声明参数: {', '.join(extra)}")
    args = {}
    for spec in _code_input_specs(node):
        name = spec["name"]
        source = spec.get("source")
        if source and source in variables:
            value = variables[source]
        elif name in raw_params:
            value = raw_params[name]
        elif spec.get("required", True):
            raise ValueError(f"缺少必填参数: {source or name}")
        else:
            value = None
        args[name] = _coerce_value(value, spec["type"])
    return args


def _args_from_variables(node: dict[str, Any], variables: dict[str, Any]
                         ) -> dict[str, Any]:
    args = {}
    for raw in (node.get("config", {}) or {}).get("inputs", []) or []:
        name = raw.get("name")
        if not name:
            raise ValueError(f"Code {node.get('id')} 存在空参数名")
        source = raw.get("source") or name
        if source in variables:
            value = variables[source]
        elif raw.get("required", True) is False:
            value = None
        else:
            raise ValueError(
                f"Code {node.get('id')} 缺少参数 {name}（变量 {source} 不存在）")
        value = _coerce_value(value, raw.get("type") or "string")
        args[name] = value
    return args


def _load_module(node_id: str):
    path = SCRIPTS_DIR / f"{node_id}.py"
    spec = importlib.util.spec_from_file_location(
        f"dag_node_{_safe_id(node_id)}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Code 脚本: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_code_node(node: dict[str, Any], args: dict[str, Any],
                   variables: dict[str, Any]) -> None:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        returned = _invoke_main(_load_module(node["id"]).main, args)
    captured = stdout.getvalue()
    if captured:
        sys.stderr.write(captured)
    if not isinstance(returned, dict):
        raise TypeError(
            f"Code {node['id']} 的 main 必须返回 dict，例如 {{'result': value}}")
    cfg = node.get("config", {}) or {}
    declared = [out for out in (cfg.get("outputs") or []) if out.get("name")]
    declared_by_name = {out["name"]: out for out in declared}
    if (len(declared) == 1 and set(returned) == {"result"} and
            declared[0]["name"] != "result"):
        outputs = declared
        values = {declared[0]["name"]: returned["result"]}
    else:
        outputs = [
            {
                "name": key,
                "type": declared_by_name.get(key, {}).get("type") or "string",
            }
            for key in returned.keys()
        ]
        values = dict(returned)
    for out in outputs:
        name = out["name"]
        value = values[name]
        type_ = out.get("type") or "string"
        if not _check_type(value, type_):
            raise TypeError(
                f"Code {node['id']} 输出 {name} 期望 {type_}，"
                f"实际为 {type(value).__name__}")
        variables[name] = value


def _node_var_base(node: dict[str, Any]) -> str:
    raw = str(node.get("name") or node.get("id") or "node").strip()
    raw = re.sub(r"\s+", "-", raw)
    value = re.sub(r"[^0-9A-Za-z_-]", "-", raw)
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        value = str(node.get("id") or "node")
    if value[0].isdigit():
        value = f"node-{value}"
    return value


def _auto_output_name(node: dict[str, Any]) -> str:
    return f"{_node_var_base(node)}-output"


def _node_output_names(node: dict[str, Any]) -> list[str]:
    ntype = node.get("type")
    cfg = node.get("config", {}) or {}
    if ntype == "code":
        return [o.get("name") for o in cfg.get("outputs", []) or []
                if o.get("name")]
    if ntype == "llm":
        return []
    if ntype in ("for", "aggregate"):
        return [_auto_output_name(node)]
    if ntype == "start":
        return [i.get("name") for i in cfg.get("inputs", []) or []
                if i.get("name")]
    return []


def _aggregate(node: dict[str, Any], variables: dict[str, Any],
               nodes: list[dict], edges: list[dict]) -> None:
    by_id = {n.get("id"): n for n in nodes}
    cfg = node.get("config", {}) or {}
    selected = [
        raw.get("source") if isinstance(raw, dict) else raw
        for raw in (cfg.get("inputs") or [])
    ]
    selected = [name for name in selected if name]
    selected_set = set(selected)
    direct_sources = {
        edge.get("source") for edge in edges
        if edge.get("target") == node.get("id") and
        edge.get("source_handle") != "retry"
    }
    if selected:
        direct_names = {
            name for source_id in direct_sources
            for name in _node_output_names(by_id.get(source_id) or {})
        }
        invalid = [name for name in selected if name not in direct_names]
        if invalid:
            raise ValueError(
                f"聚合输入必须来自直接连接的上游节点: {', '.join(invalid)}")
    values = []
    for edge in edges:
        if edge.get("target") != node.get("id"):
            continue
        src = by_id.get(edge.get("source"))
        if not src:
            continue
        for name in _node_output_names(src):
            if selected and name not in selected_set:
                continue
            if name in variables:
                values.append(variables[name])
    output_type = cfg.get("output_type") or "string"
    if output_type == "string":
        value = "\n".join(str(v) for v in values)
    elif output_type in ("int", "float"):
        value = sum(values)
    elif output_type == "list":
        value = []
        for item in values:
            value.extend(item)
    elif output_type == "dict":
        value = {}
        for item in values:
            value.update(item)
    else:
        raise ValueError(f"未知聚合输出类型: {output_type}")
    variables[_auto_output_name(node)] = value


def _eval_condition(cond: dict[str, Any], variables: dict[str, Any]) -> bool:
    var = cond.get("variable")
    if not var or var not in variables:
        raise ValueError(f"IF 条件变量未定义: {var}")
    actual = variables[var]
    op = cond.get("operator")
    if op in ("为空", "不为空"):
        empty = actual is None or (
            isinstance(actual, (str, list, dict)) and len(actual) == 0)
        return empty if op == "为空" else not empty
    if cond.get("value_type", "constant") == "variable":
        ref = cond.get("value")
        if not ref or ref not in variables:
            raise ValueError(f"IF 比较变量未定义: {ref}")
        cmp_val = variables[ref]
    else:
        cmp_val = cond.get("value")
    if op == "包含":
        return cmp_val in actual
    if op == "不包含":
        return cmp_val not in actual
    if op == "开始是":
        return str(actual).startswith(str(cmp_val))
    if op == "结束是":
        return str(actual).endswith(str(cmp_val))
    if op == "是":
        return actual == cmp_val
    if op == "不是":
        return actual != cmp_val
    raise ValueError(f"未知 IF 运算符: {op}")


def _run_if(node: dict[str, Any], variables: dict[str, Any],
            graph: dict[str, Any] | None = None) -> set[str]:
    cfg = node.get("config", {}) or {}
    conditions = cfg.get("conditions") or [{
        "variable": cfg.get("variable"),
        "operator": cfg.get("operator"),
        "value": cfg.get("value"),
        "value_type": cfg.get("value_type", "constant"),
    }]
    results = [_eval_condition(cond, variables) for cond in conditions]
    matched_index = next((index for index, passed in enumerate(results)
                          if passed), None)
    graph = graph or WORKFLOW
    legacy = cfg.get("branch_mode") != "multi" and not any(
        str(edge.get("source_handle") or "").startswith("if-")
        for edge in graph.get("edges", []) or []
        if edge.get("source") == node.get("id"))
    if legacy:
        passed = any(results) if cfg.get("combinator") == "or" else all(results)
        return {"if"} if passed else {"else"}
    return {f"if-{matched_index + 1}"} if matched_index is not None else {"else"}


def _body_out_edges(body: dict[str, Any], node_id: str,
                    handles: set[str]) -> list[str]:
    out = []
    for edge in body.get("edges", []) or []:
        if edge.get("source") != node_id:
            continue
        if edge.get("source_handle", "out") == "retry":
            continue
        if edge.get("source_handle", "out") in handles:
            out.append(edge.get("target"))
    return out


def _run_body(body: dict[str, Any], variables: dict[str, Any],
              state: dict[str, Any] | None = None,
              owner_node_id: str | None = None,
              start_nodes: list[str] | None = None,
              stop_after: str | None = None) -> tuple[bool, list[str]]:
    nodes = body.get("nodes", []) or []
    edges = body.get("edges", []) or []
    if not nodes:
        return False, []
    by_id = {n.get("id"): n for n in nodes}
    has_in = {e.get("target") for e in edges}
    out_map: dict[str, list[dict]] = {}
    for edge in edges:
        if edge.get("source_handle", "out") == "retry":
            continue
        out_map.setdefault(edge.get("source"), []).append(edge)
    queue = list(start_nodes) if start_nodes is not None else [
        n.get("id") for n in nodes if n.get("id") not in has_in]
    done: set[str] = set()
    while queue:
        node_id = queue.pop(0)
        if not node_id or node_id in done:
            continue
        node = by_id[node_id]
        ntype = node.get("type")
        _set_status(state, node_id, "running")
        if ntype == "code":
            _run_code_node(node, _args_from_variables(node, variables), variables)
            handles = {"out"}
        elif ntype == "llm":
            _set_status(state, node_id, "waiting")
            _set_status(state, owner_node_id, "waiting")
            raise PromptExit(
                _render_prompt(node, variables, graph=body),
                node_id=node_id)
        elif ntype == "if":
            handles = _run_if(node, variables, graph=body)
        elif ntype == "aggregate":
            _aggregate(node, variables, nodes, edges)
            handles = {"out"}
        else:
            handles = {"out"}
        _set_status(state, node_id, "success")
        done.add(node_id)
        next_nodes = _body_out_edges(body, node_id, handles)
        if stop_after and node_id == stop_after:
            return False, next_nodes
        queue.extend(next_nodes)
    return True, []


def _infer_body_collect(body: dict[str, Any]) -> str:
    nodes = body.get("nodes", []) or []
    edges = body.get("edges", []) or []
    if not nodes:
        return "item"
    sources = {e.get("source") for e in edges}
    terminal = next((n for n in reversed(nodes)
                     if n.get("id") not in sources), nodes[-1])
    names = _node_output_names(terminal)
    return names[0] if names else ""


def _run_for(node: dict[str, Any], variables: dict[str, Any],
             state: dict[str, Any] | None = None) -> None:
    cfg = node.get("config", {}) or {}
    source = cfg.get("list_source")
    if not source or source not in variables:
        raise ValueError(f"For 列表来源变量未定义: {source}")
    items = variables[source]
    if not isinstance(items, list):
        raise TypeError(f"For 列表来源 {source} 必须是 list")
    body = cfg.get("body") or {}
    collect = cfg.get("collect") or _infer_body_collect(body)
    collected = []
    _set_status(state, node.get("id"), "running")
    for index, item in enumerate(items):
        local = dict(variables)
        local["index"] = index
        local["item"] = item
        local["len"] = len(items)
        local["total"] = len(items)
        try:
            _run_body(body, local, state=state,
                      owner_node_id=node.get("id"))
        except PromptExit as exit_:
            variables.update(local)
            _set_status(state, node.get("id"), "waiting")
            exit_.resume = {
                "type": "for",
                "for_node": node.get("id"),
                "body": body,
                "index": index,
                "collect": collect,
                "collected": collected,
            }
            raise
        collected.append(local.get(collect, item))
    variables[_auto_output_name(node)] = collected
    _set_status(state, node.get("id"), "success")


def _continue_for_resume(resume: dict[str, Any], step_id: str,
                         variables: dict[str, Any],
                         state: dict[str, Any] | None = None) -> bool:
    if resume.get("type") != "for":
        return False
    for_node = _node_by_id(resume.get("for_node"))
    if not for_node:
        return False
    cfg = for_node.get("config", {}) or {}
    source = cfg.get("list_source")
    items = variables.get(source)
    if not isinstance(items, list):
        raise TypeError(f"For 列表来源 {source} 必须是 list")
    body = resume.get("body") or cfg.get("body") or {}
    collect = resume.get("collect") or cfg.get("collect") or _infer_body_collect(body)
    index = int(resume.get("index") or 0)
    collected = list(resume.get("collected") or [])
    local = dict(variables)
    _set_status(state, for_node.get("id"), "running")
    next_nodes = _body_out_edges(body, step_id, {"out"})
    try:
        while next_nodes:
            _, next_nodes = _run_body(
                body, local, state=state,
                owner_node_id=for_node.get("id"), start_nodes=next_nodes)
    except PromptExit as exit_:
        variables.update(local)
        _set_status(state, for_node.get("id"), "waiting")
        exit_.resume = {
            "type": "for",
            "for_node": for_node.get("id"),
            "body": body,
            "index": index,
            "collect": collect,
            "collected": collected,
        }
        raise
    variables.update(local)
    collected.append(local.get(collect, local.get("item")))
    for next_index in range(index + 1, len(items)):
        local = dict(variables)
        local["index"] = next_index
        local["item"] = items[next_index]
        local["len"] = len(items)
        local["total"] = len(items)
        try:
            _run_body(body, local, state=state,
                      owner_node_id=for_node.get("id"))
        except PromptExit as exit_:
            variables.update(local)
            _set_status(state, for_node.get("id"), "waiting")
            exit_.resume = {
                "type": "for",
                "for_node": for_node.get("id"),
                "body": body,
                "index": next_index,
                "collect": collect,
                "collected": collected,
            }
            raise
        collected.append(local.get(collect, local.get("item")))
        variables.update(local)
    variables[_auto_output_name(for_node)] = collected
    _set_status(state, for_node.get("id"), "success")
    return True


def _append_loop_context(prompt: str, variables: dict[str, Any],
                         from_error: bool = False) -> str:
    if from_error:
        return prompt
    if "index" not in variables or (
            "len" not in variables and "total" not in variables):
        return prompt
    try:
        current = int(variables["index"]) + 1
        total = int(variables.get("len", variables.get("total")))
    except (TypeError, ValueError):
        return prompt
    item_line = ""
    if "item" in variables:
        item = variables["item"]
        if isinstance(item, (list, dict)):
            item_text = json.dumps(item, ensure_ascii=False)
        else:
            item_text = str(item)
        if len(item_text) > 300:
            item_text = item_text[:300] + "..."
        item_line = f"\n当前 item: {item_text}"
    return (
        prompt.rstrip()
        + f"\n\n---\n循环上下文: 当前轮次 {current}/{total}"
        + item_line
    )


def _append_task_context(prompt: str, variables: dict[str, Any]) -> str:
    task_id = variables.get("task-id") or variables.get("task_id")
    if not task_id:
        return prompt
    return prompt.rstrip() + f"\n\n---\ntask-id: {task_id}"


def _format_next_code_entries(entries: list[dict[str, Any]],
                              task_id: str) -> str:
    if not entries:
        return ""
    first = entries[0]
    step_param = {
        spec["name"]: f"<{spec['type']}>"
        for spec in first.get("input_schema", [])
    }
    lines = [
        "-------------",
        "## 下一个step待执行命令:",
        "",
        _format_step_command(task_id or "<task-id>",
                             first["step_id"], step_param),
        "-------------",
        "**step-param 入参说明**:",
        "",
    ]
    for entry in entries:
        lines.extend([
            f"### 节点{entry['node_name']}",
            "",
            "| 参数 | 类型 | 必填 | 描述 |",
            "| --- | --- | --- | --- |",
        ])
        for spec in entry.get("input_schema", []):
            required = "是" if spec.get("required", True) else "否"
            lines.append(
                f"| {spec.get('name')} | {spec.get('type')} | {required} | {spec.get('description') or ''} |")
        lines.append("")
    lines.append("-------------")
    return "\n".join(lines).rstrip()


def _render_prompt(node: dict[str, Any], variables: dict[str, Any],
                   from_error: bool = False,
                   graph: dict[str, Any] | None = None) -> str:
    prompt = _render((node.get("config", {}) or {}).get("prompt", ""), variables)
    prompt = _append_loop_context(prompt, variables, from_error=from_error)
    prompt = _append_task_context(prompt, variables)
    if from_error:
        return prompt
    next_entries = _next_code_entries(node["id"], graph=graph)
    if not next_entries:
        return prompt
    schema = _format_next_code_entries(next_entries, variables.get("task-id", ""))
    return (
        prompt.rstrip()
        + "\n\n"
        + schema
    )


def _error_prompt(node: dict[str, Any], variables: dict[str, Any],
                  exc: BaseException) -> str | None:
    prefix = _node_var_base(node)
    variables[f"{prefix}-error-type"] = type(exc).__name__
    variables[f"{prefix}-error-message"] = str(exc)
    for edge in _out_edges(node["id"], {"error"}):
        target = _node_by_id(edge.get("target"))
        if target and target.get("type") == "llm":
            return _render_prompt(target, variables, from_error=True)
    return None


def _enqueue(queue: list[dict[str, Any]], node_id: str,
             handles: set[str], from_error: bool = False) -> None:
    for edge in _out_edges(node_id, handles):
        queue.append({
            "node_id": edge.get("target"),
            "from_error": from_error or edge.get("source_handle") == "error",
        })


def main(task_id: str, step_id: str, step_param: dict[str, Any]) -> str:
    entry = _node_by_id(step_id)
    if not entry or entry.get("type") != "code":
        raise ValueError(f"--step-id 必须是实际 Code 节点 id: {step_id}")

    task_state = _load_task(task_id)
    variables = dict(task_state.get("variables") or {})
    variables["task-id"] = task_id
    entry_args = _step_args(entry, step_param, variables)
    for name, value in entry_args.items():
        spec = next(item for item in _code_input_specs(entry)
                    if item["name"] == name)
        source = spec.get("source")
        if not source:
            variables[name] = value
        elif source not in variables and name in step_param:
            variables[source] = value
    _mark_previous_waiting_done(task_state)
    _set_status(task_state, step_id, "running")
    try:
        _run_code_node(entry, entry_args, variables)
        _set_status(task_state, step_id, "success")
    except BaseException as exc:  # noqa: BLE001
        _set_status(task_state, step_id, "failed")
        prompt = _error_prompt(entry, variables, exc)
        if prompt is not None:
            _save_waiting(task_state, variables, None, prompt)
            return prompt
        raise

    try:
        resume_state = task_state.get("resume") or {}
        resume_for = resume_state.get("for_node")
        resumed = _continue_for_resume(
            resume_state, step_id, variables, state=task_state)
    except PromptExit as exit_:
        _save_waiting(task_state, variables, exit_.node_id,
                      exit_.prompt, resume=exit_.resume)
        return exit_.prompt
    if resumed:
        task_state.pop("resume", None)
        queue: list[dict[str, Any]] = []
        _enqueue(queue, resume_for or step_id, {"out"})
    else:
        queue = []
        _enqueue(queue, step_id, {"out"})

    executed: dict[str, int] = {}
    reached_end = False
    while queue:
        item = queue.pop(0)
        node_id = item.get("node_id")
        node = _node_by_id(node_id)
        if not node:
            continue
        executed[node_id] = executed.get(node_id, 0) + 1
        if executed[node_id] > MAX_NODE_EXECUTIONS:
            raise RuntimeError(f"节点执行次数超过上限: {node_id}")
        ntype = node.get("type")
        _set_status(task_state, node_id, "running")
        if ntype == "llm":
            prompt = _render_prompt(
                node, variables, from_error=bool(item.get("from_error")))
            _save_waiting(task_state, variables, node_id, prompt)
            return prompt
        if ntype == "end":
            _set_status(task_state, node_id, "success")
            reached_end = True
            continue
        try:
            if ntype == "code":
                _run_code_node(node, _args_from_variables(node, variables), variables)
                handles = {"out"}
            elif ntype == "if":
                handles = _run_if(node, variables)
            elif ntype == "for":
                _run_for(node, variables, state=task_state)
                handles = {"out"}
            elif ntype == "aggregate":
                _aggregate(node, variables,
                           WORKFLOW.get("nodes", []) or [],
                           WORKFLOW.get("edges", []) or [])
                handles = {"out"}
            else:
                handles = {"out"}
            _set_status(task_state, node_id, "success")
        except PromptExit as exit_:
            _save_waiting(task_state, variables, exit_.node_id,
                          exit_.prompt, resume=exit_.resume)
            return exit_.prompt
        except BaseException as exc:  # noqa: BLE001
            _set_status(task_state, node_id, "failed")
            if ntype == "code":
                prompt = _error_prompt(node, variables, exc)
                if prompt is not None:
                    _save_waiting(task_state, variables, None, prompt)
                    return prompt
            raise
        _enqueue(queue, node_id, handles)
    if reached_end:
        task_state["variables"] = variables
        _finish_task(task_state)
        return (
            "任务已完成\n\n"
            f"task-id: {task_id}\n\n"
            "最终变量:\n"
            + json.dumps(variables, ensure_ascii=False, indent=2)
        )
    raise RuntimeError(f"从 Code 节点 {step_id} 未到达 Prompt 出口或 End 节点")


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="fsm to skill Agent 入口：执行指定 Code 节点直到 Prompt 出口")
    parser.add_argument("--task-id", "--task_id", dest="task_id",
                        required=True,
                        help="本次任务 id，后续所有 Code 调用保持一致")
    parser.add_argument("--step-id", required=True,
                        help="实际 Code 节点 id，例如 code-1")
    parser.add_argument("--step-param",
                        help="该 Code 节点的入参 JSON 字符串")
    ns = parser.parse_args()
    try:
        if ns.step_param:
            step_param_text = ns.step_param
        else:
            parser.error("--step-param 必须提供")
        step_param = json.loads(step_param_text)
    except json.JSONDecodeError as exc:
        parser.error(f"step 参数不是合法 JSON: {exc}")
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
'''
    return (
        template
        .replace("__WORKFLOW__", repr(wf))
        .replace("__COMMAND_CONTEXT__", repr(command_context or {}))
        .replace("__IDLE_TIMEOUT__", "600")
        .replace("__MAX_TASK_RUNTIME__", "3600")
    )
