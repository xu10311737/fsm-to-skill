"""Online Agent runtime for debug mode.

The workflow engine stops at Prompt nodes. This module persists that pause as a
task and lets an external Agent continue by calling:

    python main.py --task-id <task-id> --step-id <code-id> --step-param <json>
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from ..engine.command_format import format_step_command
from ..engine.variables import check_type

TASK_ID_VAR = "task-id"
MAX_NODE_EXECUTIONS = 50


def _coerce_bool(value: Any, type_: str | None) -> Any:
    """系统无 bool 类型，bool 值按数值语义归一化为 int（同 executor）。"""
    if isinstance(value, bool):
        if type_ == "float":
            return float(int(value))
        if type_ == "int":
            return int(value)
    return value


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


def prepare_agent_task(data_dir: str | Path, workflow: dict,
                       result: dict[str, Any]) -> Path | None:
    """Persist a waiting Engine result so main.py can continue the task."""
    if result.get("status") != "waiting":
        return None
    task_id = result.get(TASK_ID_VAR) or result.get("task_id")
    if not task_id:
        return None
    now = time.time()
    values = _plain_values(result.get("variables") or {})
    values[TASK_ID_VAR] = task_id
    state = {
        TASK_ID_VAR: task_id,
        "created-at": now,
        "updated-at": now,
        "workflow": workflow,
        "variables": values,
        "waiting-node": result.get("waiting_node"),
        "finished": False,
    }
    path = _task_path(data_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def read_agent_task(data_dir: str | Path, task_id: str) -> dict[str, Any] | None:
    path = _task_path(data_dir, task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def execute_agent_step(data_dir: str | Path, task_id: str, step_id: str,
                       step_param: dict[str, Any], code_service: Any
                       ) -> dict[str, Any]:
    """Execute one Agent-entered Code node until the next Prompt or End."""
    state = _load_task(data_dir, task_id)
    wf = state.get("workflow") or {}
    variables = dict(state.get("variables") or {})
    variables[TASK_ID_VAR] = task_id
    node = _node_by_id(wf, step_id)
    if not node or node.get("type") != "code":
        raise ValueError(f"--step-id 必须是实际 Code 节点 id: {step_id}")

    entry_args = _step_args(node, step_param, variables)
    for name, value in entry_args.items():
        source = _input_source(node, name)
        if not source:
            variables[name] = value
        elif source not in variables and name in step_param:
            variables[source] = value
    try:
        _run_code_node(node, entry_args, variables, code_service)
    except BaseException as exc:  # noqa: BLE001
        prompt = _error_prompt(wf, node, variables, exc)
        if prompt is not None:
            return _save_waiting(data_dir, state, variables, None, prompt)
        raise

    queue: list[dict[str, Any]] = []
    _enqueue(wf, queue, step_id, {"out"})
    executed: dict[str, int] = {}
    reached_end = False
    while queue:
        item = queue.pop(0)
        node_id = item.get("node_id")
        current = _node_by_id(wf, node_id)
        if not current:
            continue
        executed[node_id] = executed.get(node_id, 0) + 1
        if executed[node_id] > MAX_NODE_EXECUTIONS:
            raise RuntimeError(f"节点执行次数超过上限: {node_id}")
        ntype = current.get("type")
        if ntype == "llm":
            prompt = _render_prompt(
                wf, current, variables, from_error=bool(item.get("from_error")))
            return _save_waiting(data_dir, state, variables,
                                 current.get("id"), prompt)
        if ntype == "end":
            reached_end = True
            continue
        try:
            if ntype == "code":
                _run_code_node(current, _args_from_variables(current, variables),
                               variables, code_service)
                handles = {"out"}
            elif ntype == "if":
                handles = _run_if(current, variables, wf)
            elif ntype == "for":
                _run_for(wf, current, variables, code_service)
                handles = {"out"}
            elif ntype == "aggregate":
                _aggregate(wf, current, variables)
                handles = {"out"}
            else:
                handles = {"out"}
        except BaseException as exc:  # noqa: BLE001
            if ntype == "code":
                prompt = _error_prompt(wf, current, variables, exc)
                if prompt is not None:
                    return _save_waiting(data_dir, state, variables, None, prompt)
            raise
        _enqueue(wf, queue, node_id, handles)

    if reached_end:
        state["variables"] = variables
        state["finished"] = True
        _save_task(data_dir, state)
        return {
            "status": "completed",
            "task-id": task_id,
            "variables": variables,
            "message": (
                "任务已完成\n\n"
                f"task-id: {task_id}\n\n"
                "最终变量:\n"
                + json.dumps(variables, ensure_ascii=False, indent=2)
            ),
        }
    raise RuntimeError(f"从 Code 节点 {step_id} 未到达 Prompt 出口或 End 节点")


def _plain_values(snapshot: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, info in snapshot.items():
        if isinstance(info, dict) and "value" in info:
            out[name] = info.get("value")
        else:
            out[name] = info
    return out


def _task_path(data_dir: str | Path, task_id: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z_.-]", "-", str(task_id or "task"))
    return Path(data_dir) / "tasks" / f"{safe}.json"


def _load_task(data_dir: str | Path, task_id: str) -> dict[str, Any]:
    path = _task_path(data_dir, task_id)
    if not path.exists():
        raise FileNotFoundError(f"task 不存在或已过期: {task_id}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_task(data_dir: str | Path, state: dict[str, Any]) -> None:
    state["updated-at"] = time.time()
    path = _task_path(data_dir, state[TASK_ID_VAR])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _save_waiting(data_dir: str | Path, state: dict[str, Any],
                  variables: dict[str, Any], node_id: str | None,
                  prompt: str) -> dict[str, Any]:
    state["variables"] = variables
    state["waiting-node"] = node_id
    state["last-prompt"] = prompt
    state["finished"] = False
    _save_task(data_dir, state)
    return {
        "status": "waiting",
        "task-id": state[TASK_ID_VAR],
        "waiting_node": node_id,
        "prompt": prompt,
        "variables": variables,
    }


def _node_by_id(wf: dict[str, Any], node_id: str | None) -> dict[str, Any] | None:
    if not node_id:
        return None
    for node in wf.get("nodes", []) or []:
        if node.get("id") == node_id:
            return node
    for node in wf.get("nodes", []) or []:
        body = (node.get("config", {}) or {}).get("body") or {}
        found = _node_by_id(body, node_id)
        if found:
            return found
    return None


def _edges(wf: dict[str, Any]) -> list[dict[str, Any]]:
    return wf.get("edges", []) or []


def _out_edges(wf: dict[str, Any], node_id: str,
               handles: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for edge in _edges(wf):
        if edge.get("source") != node_id:
            continue
        handle = edge.get("source_handle", "out")
        if handles is not None and handle not in handles:
            continue
        out.append(edge)
    return out


def _enqueue(wf: dict[str, Any], queue: list[dict[str, Any]],
             node_id: str, handles: set[str], from_error: bool = False) -> None:
    for edge in _out_edges(wf, node_id, handles):
        queue.append({
            "node_id": edge.get("target"),
            "from_error": from_error or edge.get("source_handle") == "error",
        })


def _step_args(node: dict[str, Any], step_param: dict[str, Any],
               variables: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(step_param, dict):
        raise TypeError("--step-param 必须是 JSON object")
    inputs = _code_input_specs(node)
    # Bound inputs normally come from task state. Keep accepting their names
    # for legacy first-step CLI calls that have no persisted source yet.
    allowed = {spec["name"] for spec in inputs}
    extra = sorted(set(step_param) - allowed)
    if extra:
        raise ValueError(f"--step-param 包含未声明参数: {', '.join(extra)}")
    args: dict[str, Any] = {}
    for spec in inputs:
        name = spec["name"]
        source = spec.get("source")
        if source and source in variables:
            value = variables[source]
        elif name in step_param:
            value = step_param[name]
        elif spec.get("required", True):
            raise ValueError(f"--step-param 缺少必填参数: {source or name}")
        else:
            value = None
        type_ = spec.get("type")
        if value is not None and type_ and not check_type(value, type_):
            raise TypeError(
                f"参数 {name} 声明类型 {type_}，实际值 {value!r} 不符")
        args[name] = value
    return args


def _args_from_variables(node: dict[str, Any],
                         variables: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for spec in _code_input_specs(node):
        name = spec["name"]
        source = spec.get("source") or name
        if source in variables:
            value = variables[source]
        elif spec.get("required", True):
            raise ValueError(f"输入参数 {name} 未提供: {source}")
        else:
            value = None
        args[name] = value
    return args


def _run_code_node(node: dict[str, Any], args: dict[str, Any],
                   variables: dict[str, Any], code_service: Any) -> None:
    cfg = node.get("config", {}) or {}
    resp = code_service.run(cfg.get("code", ""), args,
                            timeout=cfg.get("timeout", 30),
                            node_id=node.get("id", ""))
    if not resp.get("ok"):
        err_type = resp.get("error_type") or "RuntimeError"
        err_msg = resp.get("error_message") or "脚本执行失败"
        raise RuntimeError(f"{err_type}: {err_msg}")
    returned = resp.get("result")
    if not isinstance(returned, dict):
        raise TypeError("脚本 main 必须返回 dict")
    declared = [out for out in (cfg.get("outputs", []) or []) if out.get("name")]
    declared_by_name = {out["name"]: out for out in declared}
    if (len(declared) == 1 and set(returned) == {"result"} and
            declared[0]["name"] != "result"):
        outputs = declared
        values = {declared[0]["name"]: returned["result"]}
    else:
        outputs = [
            {"name": key, "type": declared_by_name.get(key, {}).get("type")}
            for key in returned
        ]
        values = dict(returned)
    for out in outputs:
        name = out["name"]
        type_ = out.get("type")
        variables[name] = _coerce_bool(values[name], type_)


def _error_prompt(wf: dict[str, Any], node: dict[str, Any],
                  variables: dict[str, Any], exc: BaseException) -> str | None:
    prefix = _node_var_base(node)
    variables[f"{prefix}-error-type"] = type(exc).__name__
    variables[f"{prefix}-error-message"] = str(exc)
    for edge in _out_edges(wf, node.get("id"), {"error"}):
        target = _node_by_id(wf, edge.get("target"))
        if target and target.get("type") == "llm":
            return _render_prompt(wf, target, variables, from_error=True)
    return None


def _render(template: str, variables: dict[str, Any]) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name not in variables:
            raise KeyError(name)
        value = variables[name]
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return "" if value is None else str(value)
    return re.sub(r"{{\s*([^}]+?)\s*}}", repl, template or "")


def _render_prompt(wf: dict[str, Any], node: dict[str, Any],
                   variables: dict[str, Any],
                   from_error: bool = False) -> str:
    prompt = _render((node.get("config", {}) or {}).get("prompt", ""), variables)
    prompt = _append_loop_context(prompt, variables, from_error=from_error)
    prompt = _append_task_context(prompt, variables)
    if from_error:
        return prompt
    entries = _next_code_entries(wf, node.get("id"))
    if not entries:
        return prompt
    context = _command_context(wf, node)
    return prompt.rstrip() + "\n\n" + _format_next_code_entries(
        entries, variables.get(TASK_ID_VAR, ""), context, variables)


def _command_context(wf: dict[str, Any],
                     node: dict[str, Any]) -> dict[str, Any] | None:
    cfg = node.get("config", {}) or {}
    context = cfg.get("command_context")
    if isinstance(context, dict):
        return context
    runtime = wf.get("_runtime") or {}
    context = runtime.get("command_context")
    return context if isinstance(context, dict) else None


def _append_loop_context(prompt: str, variables: dict[str, Any],
                         from_error: bool = False) -> str:
    if from_error or "index" not in variables:
        return prompt
    total = variables.get("len", variables.get("total"))
    if total is None:
        return prompt
    try:
        current = int(variables["index"]) + 1
        total_i = int(total)
    except (TypeError, ValueError):
        return prompt
    return prompt.rstrip() + f"\n\n---\n循环上下文: 当前轮次 {current}/{total_i}"


def _append_task_context(prompt: str, variables: dict[str, Any]) -> str:
    task_id = variables.get(TASK_ID_VAR)
    if not task_id:
        return prompt
    return prompt.rstrip() + f"\n\n---\ntask-id: {task_id}"


def _next_code_entries(wf: dict[str, Any], prompt_id: str | None
                       ) -> list[dict[str, Any]]:
    if not prompt_id:
        return []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue = [edge.get("target") for edge in _out_edges(wf, prompt_id)]
    while queue:
        node_id = queue.pop(0)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        node = _node_by_id(wf, node_id)
        if not node:
            continue
        if node.get("type") == "code":
            found.append({
                "step_id": node_id,
                "node_name": node.get("name") or node_id,
                "input_schema": _code_input_schema(node),
            })
            continue
        if node.get("type") in ("llm", "end"):
            continue
        queue.extend(edge.get("target") for edge in _out_edges(wf, node_id))
    return found


def _format_next_code_entries(entries: list[dict[str, Any]],
                              task_id: str,
                              command_context: dict[str, Any] | None,
                              variables: dict[str, Any] | None = None) -> str:
    variables = variables or {}
    first = entries[0]
    # 参数值由 agent 基于上下文推理，命令中保留 <type> 占位符，不硬编码写死。
    command = format_step_command(
        command_context,
        task_id or "<task-id>",
        first["step_id"],
        {
            spec["name"]: f"<{spec['type']}>"
            for spec in first["input_schema"]
        },
    )
    lines = [
        "-------------",
        "## 下一个step待执行命令:",
        "",
        command,
        "-------------",
        "**step-param 入参说明**:",
        "",
        _format_schema_md(entries),
        "",
        "**执行说明**:",
        "1. 当前主机是 Windows PowerShell，禁止 bash 语法（&&、||、ls -la、rg、test -f 等）。",
        "2. 命令中的 `--step-param-b64 '<...>'` 是 JSON 的 base64，请先用下面命令解码查看占位符：",
        "   python -c \"import base64,json; print(json.dumps(json.loads(base64.urlsafe_b64decode('<...>')), ensure_ascii=False))\"",
        "3. 用真实推理出的参数替换 JSON 里的 `<占位符>`，再重新 base64 编码提交，例如：",
        "   python -c \"import base64,json; print(base64.urlsafe_b64encode(json.dumps({'arg-1':'真实值'}, ensure_ascii=False, separators=(',',':')).encode('utf-8')).decode('ascii'))\"",
        "4. 严禁原样提交含 `<占位符>` 的命令，那会导致脚本拿到占位符而非真实值。",
    ]
    # 附上上下文里已有的参考值，帮助 agent 推理出真实参数（仅作参考，不写死）。
    refs = []
    for spec in first["input_schema"]:
        name = spec["name"]
        if variables.get(name) is not None:
            v = variables[name]
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            refs.append(f"  {name} = {v}")
    if refs:
        lines.extend([
            "",
            "**当前上下文可参考值（请基于这些推理出真实参数，替换命令中的 <占位符>，不得原样提交占位符）**:",
            "",
            *refs,
        ])
    lines.append("-------------")
    return "\n".join(lines)


def _format_schema_md(entries: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for entry in entries:
        lines.extend([
            f"### 节点{entry['node_name']}",
            "",
            "| 参数 | 类型 | 必填 | 描述 |",
            "| --- | --- | --- | --- |",
        ])
        for spec in entry["input_schema"]:
            lines.append(
                f"| {spec['name']} | {spec['type']} | "
                f"{'是' if spec['required'] else '否'} | "
                f"{spec.get('description', '')} |")
        lines.append("")
    return "\n".join(lines).rstrip()


def _code_input_specs(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [{
        "name": spec.get("name") or f"arg-{i + 1}",
        "description": spec.get("description") or "",
        "type": spec.get("type") or "string",
        "required": spec.get("required", True) is not False,
        **({"source": spec.get("source")} if spec.get("source") else {}),
    } for i, spec in enumerate(
        (node.get("config", {}) or {}).get("inputs", []) or [])]


def _code_input_schema(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only parameters that must be supplied by the external Agent."""
    return [spec for spec in _code_input_specs(node) if not spec.get("source")]


def _input_source(node: dict[str, Any], name: str) -> str | None:
    for spec in _code_input_specs(node):
        if spec["name"] == name:
            return spec.get("source")
    return None


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
    edges = (graph or {}).get("edges", []) or []
    legacy = cfg.get("branch_mode") != "multi" and not any(
        str(edge.get("source_handle") or "").startswith("if-")
        for edge in edges if edge.get("source") == node.get("id"))
    if legacy:
        passed = any(results) if cfg.get("combinator") == "or" else all(results)
        return {"if"} if passed else {"else"}
    return {f"if-{matched_index + 1}"} if matched_index is not None else {"else"}


def _run_for(wf: dict[str, Any], node: dict[str, Any],
             variables: dict[str, Any], code_service: Any) -> None:
    cfg = node.get("config", {}) or {}
    source = cfg.get("list_source")
    if not source or source not in variables:
        raise ValueError(f"For 列表来源变量未定义: {source}")
    items = variables[source]
    if not isinstance(items, list):
        raise TypeError(f"For 列表来源 {source} 必须是 list")
    body = cfg.get("body") or {}
    collect = cfg.get("collect") or "item"
    collected = []
    for index, item in enumerate(items):
        local = dict(variables)
        local["index"] = index
        local["item"] = item
        local["len"] = len(items)
        local["total"] = len(items)
        _run_body(body, local, code_service)
        collected.append(local.get(collect, item))
    variables[_auto_output_name(node)] = collected


def _run_body(body: dict[str, Any], variables: dict[str, Any],
              code_service: Any) -> None:
    nodes = body.get("nodes", []) or []
    edges = body.get("edges", []) or []
    has_in = {edge.get("target") for edge in edges}
    queue = [node.get("id") for node in nodes if node.get("id") not in has_in]
    done: set[str] = set()
    while queue:
        node_id = queue.pop(0)
        if not node_id or node_id in done:
            continue
        node = next((n for n in nodes if n.get("id") == node_id), None)
        if not node:
            continue
        ntype = node.get("type")
        if ntype == "code":
            _run_code_node(node, _args_from_variables(node, variables),
                           variables, code_service)
            handles = {"out"}
        elif ntype == "if":
            handles = _run_if(node, variables, {"nodes": nodes, "edges": edges})
        elif ntype == "aggregate":
            _aggregate({"nodes": nodes, "edges": edges}, node, variables)
            handles = {"out"}
        elif ntype == "llm":
            raise RuntimeError("循环体 Prompt 需要由外部 Agent 分步调用")
        else:
            handles = {"out"}
        done.add(node_id)
        for edge in edges:
            if (edge.get("source") == node_id and
                    edge.get("source_handle", "out") in handles):
                queue.append(edge.get("target"))


def _aggregate(wf: dict[str, Any], node: dict[str, Any],
               variables: dict[str, Any]) -> None:
    cfg = node.get("config", {}) or {}
    selected = [
        raw.get("source") if isinstance(raw, dict) else raw
        for raw in (cfg.get("inputs") or [])
    ]
    selected = [name for name in selected if name]
    selected_set = set(selected)
    direct_sources = {
        edge.get("source") for edge in _edges(wf)
        if edge.get("target") == node.get("id") and
        edge.get("source_handle") != "retry"
    }
    if selected:
        direct_names = {
            name for source_id in direct_sources
            for name in _node_output_names(_node_by_id(wf, source_id) or {})
        }
        invalid = [name for name in selected if name not in direct_names]
        if invalid:
            raise ValueError(
                "聚合输入必须来自直接连接的上游节点: " + ", ".join(invalid))
    values = []
    for edge in _edges(wf):
        if (edge.get("target") != node.get("id") or
                edge.get("source_handle") == "retry"):
            continue
        src = _node_by_id(wf, edge.get("source"))
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


def _node_output_names(node: dict[str, Any]) -> list[str]:
    cfg = node.get("config", {}) or {}
    ntype = node.get("type")
    if ntype == "code":
        return [out.get("name") for out in cfg.get("outputs", []) or []
                if out.get("name")]
    if ntype in ("for", "aggregate"):
        return [_auto_output_name(node)]
    if ntype == "start":
        return [item.get("name") for item in cfg.get("inputs", []) or []
                if item.get("name")]
    return []
