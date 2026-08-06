"""工作流静态校验器（PRD 第 5 章）。

对用户工作流做保存前/运行前静态检查，返回结构化问题报告::

    {"errors":   [{"code", "node_id", "message"}, ...],
     "warnings": [{"code", "node_id", "message"}, ...]}

检查项（错误码）：结构完整性（NO_START / MULTIPLE_STARTS / NO_END /
START_HAS_INPUT / END_HAS_OUTPUT / DUPLICATE_NODE_ID / DUPLICATE_NODE_NAME / CYCLE /
UNREACHABLE_NODE / IF_BRANCH_UNCONNECTED）、节点配置（EMPTY_PROMPT /
EMPTY_CODE / CODE_SYNTAX / NO_MAIN_FUNC / MAIN_PARAMS_MISMATCH）、
变量系统（UNDEFINED_VARIABLE / DUPLICATE_VARIABLE / INVALID_VAR_NAME /
TYPE_MISMATCH）、循环（FOR_INPUT_NOT_LIST / FOR_NO_OUTPUT /
NESTED_FOR）。
"""
from __future__ import annotations

import ast
import re
from typing import Any, Optional

from ..engine.naming import is_valid_python_param_name, python_param_name
from ..engine.topo import find_cycle, unreachable_from_start
from ..engine.variables import TYPES, is_valid_var_name

RETRY_HANDLE = "retry"

# 允许作为用户声明输出名的保留字：Code 节点契约 main() -> {"result": ...}
_ALLOWED_RESERVED_OUTPUT = {"result"}

# 模板中常见的 Jinja 关键字/内置，不作为变量引用处理
_JINJA_KEYWORDS = {
    "if", "else", "elif", "endif", "for", "endfor", "in", "is", "not",
    "and", "or", "true", "false", "none", "loop", "defined", "undefined",
    "length", "default", "join", "tojson", "e", "string", "int", "float",
    "list", "dict", "namespace", "set", "with", "endwith", "filter",
    "endfilter", "block", "endblock", "macro", "endmacro", "raw",
    "endraw", "include", "import", "from", "as", "recursive",
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TEMPLATE_EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SAFE_PREFIX_RE = re.compile(r"[^0-9A-Za-z_-]")
_SYSTEM_PRODUCERS = {
    "task-id": ("string", "system"),
}


def _issue(code: str, node_id: Optional[str], message: str) -> dict:
    return {"code": code, "node_id": node_id, "message": message}


def _node_var_base(node: dict) -> str:
    raw = str(node.get("name") or node.get("id") or "node").strip()
    raw = re.sub(r"\s+", "-", raw)
    value = _SAFE_PREFIX_RE.sub("-", raw)
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        value = str(node.get("id") or "node")
    if value[0].isdigit():
        value = f"node-{value}"
    return value


def _safe_prefix(node: dict) -> str:
    """与执行引擎一致的错误变量前缀规则。"""
    return _node_var_base(node)


def _auto_output_name(node: dict) -> str:
    return f"{_node_var_base(node)}-output"


def _is_valid_param_name(name: str) -> bool:
    """Code argparse 参数名：短横线名称，映射后必须是合法 Python 标识符。"""
    return is_valid_python_param_name(name)


def _build_producers(nodes: list[dict]) -> dict[str, tuple[str, Optional[str]]]:
    """构建 变量名 -> (类型, 产出节点id) 映射；重复产出时保留首个并另行报错。"""
    producers: dict[str, tuple[str, Optional[str]]] = dict(_SYSTEM_PRODUCERS)
    for n in nodes:
        ntype = n.get("type")
        cfg = n.get("config", {}) or {}
        if ntype == "start":
            for spec in cfg.get("inputs", []) or []:
                producers.setdefault(spec["name"], (spec.get("type"), n["id"]))
        elif ntype == "code":
            declared = [out for out in (cfg.get("outputs", []) or [])
                        if out.get("name")]
            declared_by_name = {out["name"]: out for out in declared}
            static_keys = _static_return_keys(cfg.get("code", ""))
            if static_keys:
                if (len(declared) == 1 and static_keys == ["result"] and
                        declared[0]["name"] != "result"):
                    outputs = declared
                else:
                    outputs = [
                        {
                            "name": name,
                            "type": declared_by_name.get(name, {})
                            .get("type", "string"),
                        }
                        for name in static_keys
                    ]
            else:
                outputs = declared
            for spec in outputs:
                producers.setdefault(spec["name"], (spec.get("type"), n["id"]))
            if cfg.get("error_branch"):
                prefix = _safe_prefix(n)
                producers.setdefault(f"{prefix}-error-type", ("string", n["id"]))
                producers.setdefault(f"{prefix}-error-message",
                                     ("string", n["id"]))
        elif ntype == "for":
            producers.setdefault(_auto_output_name(n), ("list", n["id"]))
        elif ntype == "aggregate":
            producers.setdefault(_auto_output_name(n),
                                 (cfg.get("output_type"), n["id"]))
    return producers


def _upstream_node_ids(node_id: str, edges: list[dict]) -> set[str]:
    reverse: dict[str, list[str]] = {}
    for edge in edges:
        if edge.get("source_handle") == RETRY_HANDLE:
            continue
        reverse.setdefault(edge.get("target"), []).append(edge.get("source"))
    seen: set[str] = set()
    queue = list(reverse.get(node_id, []))
    while queue:
        nid = queue.pop(0)
        if not nid or nid in seen:
            continue
        seen.add(nid)
        queue.extend(reverse.get(nid, []))
    return seen


def _out_map(edges: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for edge in edges:
        if edge.get("source_handle") == RETRY_HANDLE:
            continue
        out.setdefault(edge.get("source"), []).append(edge)
    return out


def _can_reach(from_id: str, target_id: str,
               out: dict[str, list[dict]],
               seen: Optional[set[str]] = None) -> bool:
    if not from_id:
        return False
    if from_id == target_id:
        return True
    seen = seen or set()
    if from_id in seen:
        return False
    seen.add(from_id)
    return any(_can_reach(edge.get("target"), target_id, out, seen)
               for edge in out.get(from_id, []))


def _branch_guaranteed_owner(owner: Optional[str], target_id: str,
                             nodes: list[dict],
                             edges: list[dict]) -> bool:
    """IF 分支汇合后，只把所有可达分支都保证产出的变量视为可用。"""
    if owner in (None, "system") or not target_id:
        return True
    out = _out_map(edges)
    for node in nodes:
        if node.get("type") != "if":
            continue
        branches = [
            edge for edge in out.get(node["id"], [])
            if edge.get("source_handle") == "else" or
            str(edge.get("source_handle") or "").startswith("if")
        ]
        branches_to_target = [
            edge for edge in branches
            if _can_reach(edge.get("target"), target_id, out)
        ]
        if len(branches_to_target) < 2:
            continue
        owner_branches = [
            edge for edge in branches_to_target
            if _can_reach(edge.get("target"), owner, out)
        ]
        if 0 < len(owner_branches) < len(branches_to_target):
            return False
    return True


def _available_producers_for(
        node_id: str,
        nodes: list[dict],
        edges: list[dict],
        producers: dict[str, tuple[str, Optional[str]]],
) -> dict[str, tuple[str, Optional[str]]]:
    upstream = _upstream_node_ids(node_id, edges)
    out = dict(_SYSTEM_PRODUCERS)
    for name, (type_, owner) in producers.items():
        if owner == "system" or (
                owner in upstream and _branch_guaranteed_owner(
                    owner, node_id, nodes, edges)):
            out[name] = (type_, owner)
    return out


def _available_body_producers_for(
        body_node_id: str,
        body_nodes: list[dict],
        body_edges: list[dict],
        parent_available: dict[str, tuple[str, Optional[str]]],
        for_node_id: str,
) -> dict[str, tuple[str, Optional[str]]]:
    body_producers = _build_producers(body_nodes)
    upstream = _upstream_node_ids(body_node_id, body_edges)
    out = dict(parent_available)
    out.update({
        "index": ("int", for_node_id),
        "item": (None, for_node_id),
        "len": ("int", for_node_id),
        "total": ("int", for_node_id),
    })
    for name, (type_, owner) in body_producers.items():
        if owner == "system" or (
                owner in upstream and _branch_guaranteed_owner(
                    owner, body_node_id, body_nodes, body_edges)):
            out[name] = (type_, owner)
    return out


def _static_return_keys(code: str) -> list[str]:
    """从 main 中静态提取 return {'key': ...} 的字符串 key。动态返回则为空。"""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    main_fn = next(
        (d for d in tree.body
         if isinstance(d, ast.FunctionDef) and d.name == "main"),
        None)
    if main_fn is None:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    class MainReturnVisitor(ast.NodeVisitor):
        def visit_Return(self, node: ast.Return) -> None:
            if not isinstance(node.value, ast.Dict):
                return
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value not in seen:
                        keys.append(key.value)
                        seen.add(key.value)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

    visitor = MainReturnVisitor()
    for statement in main_fn.body:
        visitor.visit(statement)
    return keys


def _type_compatible(producer_type: Optional[str],
                     declared: Optional[str]) -> bool:
    """类型兼容：相同兼容；int 可提升为 float；未知类型不报错。"""
    if producer_type is None or declared is None:
        return True
    if producer_type == declared:
        return True
    return producer_type == "int" and declared == "float"


def _template_var_refs(template: str,
                       producers: dict[str, tuple[str, Optional[str]]]
                       ) -> set[str]:
    """提取模板 `{{ ... }}` 中的变量引用名。

    含连字符的自动命名变量（如 ``for-1-output``）以整串出现在模板中，
    其标识符片段不作为独立引用处理。
    """
    consumed: set[str] = set()
    refs: set[str] = set()
    for name in producers:
        if not _IDENT_RE.fullmatch(name) and name in template:
            refs.add(name)
            consumed.update(p for p in _IDENT_RE.findall(name))
    for expr in _TEMPLATE_EXPR_RE.findall(template):
        for ident in _IDENT_RE.findall(expr):
            if ident in _JINJA_KEYWORDS or ident in consumed:
                continue
            refs.add(ident)
    return refs


def validate_workflow(wf: dict) -> dict:
    """校验工作流 dict，返回 {"errors": [...], "warnings": [...]}。"""
    errors: list[dict] = []
    warnings: list[dict] = []
    nodes = wf.get("nodes", []) or []
    edges = wf.get("edges", []) or []

    # --------------------------------------------------------------
    # 结构：节点 id 唯一性
    seen_ids: set[str] = set()
    seen_names: dict[str, str] = {}
    for n in nodes:
        if n["id"] in seen_ids:
            errors.append(_issue("DUPLICATE_NODE_ID", n["id"],
                                 f"节点 id 重复: {n['id']}"))
        seen_ids.add(n["id"])
        name = (n.get("name") or "").strip()
        if name:
            if name in seen_names:
                errors.append(_issue(
                    "DUPLICATE_NODE_NAME", n["id"],
                    f"节点名称重复: {name}（已被 {seen_names[name]} 使用）"))
            else:
                seen_names[name] = n["id"]

    by_id = {n["id"]: n for n in nodes}
    starts = [n for n in nodes if n.get("type") == "start"]
    ends = [n for n in nodes if n.get("type") == "end"]

    # 结构：Start / End 存在性与数量
    if not starts:
        errors.append(_issue("NO_START", None,
                             "工作流缺少 Start 节点"))
    if len(starts) > 1:
        errors.append(_issue("MULTIPLE_STARTS", None,
                             f"工作流存在 {len(starts)} 个 Start 节点"))
    if not ends:
        errors.append(_issue("NO_END", None,
                             "工作流缺少 End 节点"))

    in_edges: dict[str, list[dict]] = {n["id"]: [] for n in nodes}
    out_edges: dict[str, list[dict]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.get("target") in in_edges:
            in_edges[e["target"]].append(e)
        if e.get("source") in out_edges:
            out_edges[e["source"]].append(e)

    for s in starts:
        if in_edges.get(s["id"]):
            errors.append(_issue("START_HAS_INPUT", s["id"],
                                 "Start 节点不允许有入边"))
    for e_ in ends:
        if out_edges.get(e_["id"]):
            errors.append(_issue("END_HAS_OUTPUT", e_["id"],
                                 "End 节点不允许有出边"))

    # 结构：环（retry 边豁免）
    if find_cycle(wf):
        errors.append(_issue("CYCLE", None,
                             "工作流存在环（retry 边除外）"))

    # 结构：可达性（仅在恰好一个 Start 时检查）
    if len(starts) == 1:
        for nid in unreachable_from_start(wf):
            errors.append(_issue("UNREACHABLE_NODE", nid,
                                 f"节点 {nid} 从 Start 不可达"))

    # 结构：每条 IF 条件和 ELSE 都必须各有一条出边，且每个出口只能连接一次。
    for n in nodes:
        if n.get("type") != "if":
            continue
        cfg = n.get("config", {}) or {}
        conditions = cfg.get("conditions") or [{}]
        has_multi_handles = any(
            str(edge.get("source_handle") or "").startswith("if-")
            for edge in out_edges.get(n["id"], []))
        required_handles = ([f"if-{index + 1}" for index in range(len(conditions))]
                            + ["else"]) if has_multi_handles or \
            cfg.get("branch_mode") == "multi" else ["if", "else"]
        handle_edges: dict[str, list[dict]] = {}
        for edge in out_edges.get(n["id"], []):
            handle_edges.setdefault(edge.get("source_handle") or "out", []).append(edge)
        missing = [handle for handle in required_handles if not handle_edges.get(handle)]
        if missing:
            errors.append(_issue(
                "IF_BRANCH_UNCONNECTED", n["id"],
                f"IF 节点 {n['id']} 的出口必须全部连接: {', '.join(missing)}"))
        repeated = [handle for handle in required_handles
                    if len(handle_edges.get(handle, [])) > 1]
        if repeated:
            errors.append(_issue(
                "IF_BRANCH_MULTIPLE_TARGETS", n["id"],
                f"IF 节点 {n['id']} 的出口只能连接一个节点: {', '.join(repeated)}"))

    for n in nodes:
        if n.get("type") != "code":
            continue
        for edge in out_edges.get(n["id"], []):
            if edge.get("source_handle") != "error":
                continue
            target = by_id.get(edge.get("target"))
            if not target or target.get("type") != "llm":
                errors.append(_issue(
                    "ERROR_BRANCH_TARGET_NOT_PROMPT", n["id"],
                    "Code error 出边只能连接 Prompt 节点"))

    # --------------------------------------------------------------
    # 变量产出表（供引用/重复/类型检查）
    producers = _build_producers(nodes)
    # 重复定义：按产出节点统计
    name_owners: dict[str, list[str]] = {
        name: [owner or "system"]
        for name, (_, owner) in _SYSTEM_PRODUCERS.items()
    }
    for n in nodes:
        ntype = n.get("type")
        cfg = n.get("config", {}) or {}
        names: list[str] = []
        if ntype == "start":
            names += [s["name"] for s in cfg.get("inputs", []) or []]
        elif ntype == "code":
            names += [
                s["name"] for s in (
                    cfg.get("outputs", []) or [
                        {"name": name}
                        for name in _static_return_keys(cfg.get("code", ""))
                    ])
            ]
        elif ntype in ("for", "aggregate"):
            names.append(_auto_output_name(n))
        if ntype == "code" and cfg.get("error_branch"):
            prefix = _safe_prefix(n)
            names += [f"{prefix}-error-type", f"{prefix}-error-message"]
        for name in names:
            name_owners.setdefault(name, []).append(n["id"])
    for name, owners in name_owners.items():
        if len(owners) > 1:
            errors.append(_issue(
                "DUPLICATE_VARIABLE", owners[-1],
                f"变量 {name} 被多个节点重复定义: {', '.join(owners)}"))

    # 变量名合法性：用户声明的名字（Start 输入 / Code 输出）
    def _check_name(name: str, node_id: str) -> None:
        if is_valid_var_name(name):
            return
        if name in _ALLOWED_RESERVED_OUTPUT and _IDENT_RE.fullmatch(name):
            return
        errors.append(_issue(
            "INVALID_VAR_NAME", node_id,
            f"变量名 {name!r} 不合法（须为 ASCII 标识符且非保留字）"))

    for n in nodes:
        cfg = n.get("config", {}) or {}
        if n.get("type") == "start":
            for spec in cfg.get("inputs", []) or []:
                _check_name(spec["name"], n["id"])
        elif n.get("type") == "code":
            for spec in cfg.get("outputs", []) or [
                    {"name": name} for name in _static_return_keys(cfg.get("code", ""))]:
                _check_name(spec["name"], n["id"])
            seen_params: set[str] = set()
            seen_py_params: dict[str, str] = {}
            for spec in cfg.get("inputs", []) or []:
                name = spec.get("name")
                if not _is_valid_param_name(name):
                    errors.append(_issue(
                        "INVALID_PARAM_NAME", n["id"],
                        f"Code 输入参数名 {name!r} 不合法（须为 Python 标识符且非关键字）"))
                    continue
                if name in seen_params:
                    errors.append(_issue(
                        "DUPLICATE_INPUT_PARAM", n["id"],
                        f"Code 输入参数重复: {name}"))
                seen_params.add(name)
                py_name = python_param_name(name)
                if py_name in seen_py_params and seen_py_params[py_name] != name:
                    errors.append(_issue(
                        "DUPLICATE_INPUT_PARAM", n["id"],
                        f"Code 输入参数 {seen_py_params[py_name]!r} 与 "
                        f"{name!r} 会映射到同一个 Python 形参 {py_name!r}"))
                seen_py_params[py_name] = name
                type_ = spec.get("type") or "string"
                if type_ not in TYPES:
                    errors.append(_issue(
                        "INVALID_VAR_TYPE", n["id"],
                        f"Code 输入参数 {name} 的类型 {type_!r} 不受支持"))
                if "required" in spec and not isinstance(spec.get("required"), bool):
                    errors.append(_issue(
                        "INVALID_PARAM_REQUIRED", n["id"],
                        f"Code 输入参数 {name} 的 required 必须是布尔值"))

    # --------------------------------------------------------------
    # 节点配置与变量引用
    def _resolve_source(source: Any, node: dict,
                        available: dict[str, tuple[str, Optional[str]]],
                        locals_allowed: bool = False) -> None:
        """UNDEFINED_VARIABLE / TYPE_MISMATCH 检查。"""
        if not isinstance(source, str) or not source:
            errors.append(_issue("UNDEFINED_VARIABLE", node["id"],
                                 f"输入变量来源为空"))
            return
        if locals_allowed and source in ("index", "item", "len", "total"):
            return
        if source not in available:
            errors.append(_issue("UNDEFINED_VARIABLE", node["id"],
                                 f"引用了未定义的变量: {source}"))
            return

    for n in nodes:
        ntype = n.get("type")
        cfg = n.get("config", {}) or {}
        available = _available_producers_for(n["id"], nodes, edges, producers)

        if ntype == "llm":
            prompt = cfg.get("prompt", "")
            if not prompt or not prompt.strip():
                errors.append(_issue("EMPTY_PROMPT", n["id"],
                                     "LLM 节点提示词不能为空"))
            else:
                for ref in _template_var_refs(prompt, producers):
                    if ref not in available:
                        errors.append(_issue(
                            "UNDEFINED_VARIABLE", n["id"],
                            f"提示词模板引用了未定义的变量: {ref}"))

        elif ntype == "code":
            code = cfg.get("code", "")
            if not code or not code.strip():
                errors.append(_issue("EMPTY_CODE", n["id"],
                                     "Code 节点代码不能为空"))
            else:
                tree = None
                try:
                    tree = ast.parse(code)
                except SyntaxError as e:
                    errors.append(_issue(
                        "CODE_SYNTAX", n["id"],
                        f"Code 节点代码存在语法错误: {e.msg}"))
                if tree is not None:
                    main_fn = next(
                        (d for d in tree.body
                         if isinstance(d, ast.FunctionDef)
                         and d.name == "main"),
                        None)
                    if main_fn is None:
                        errors.append(_issue(
                            "NO_MAIN_FUNC", n["id"],
                            "Code 节点代码必须定义 main 函数"))
                    else:
                        params = [a.arg for a in main_fn.args.args]
                        declared = [s.get("name")
                                    for s in cfg.get("inputs", []) or []]
                        declared_params = [
                            python_param_name(name) for name in declared
                        ]
                        uses_params_dict = params == ["params"]
                        if not uses_params_dict and set(params) != set(declared_params):
                            errors.append(_issue(
                                "MAIN_PARAMS_MISMATCH", n["id"],
                                f"main 函数参数 {params} 与声明输入 "
                                f"{declared}（Python 形参 {declared_params}）"
                                f"不一一对应"))
            for spec in cfg.get("inputs", []) or []:
                if "source" not in spec:
                    continue
                source = spec.get("source")
                if isinstance(source, str) and source in available:
                    ptype, _ = available[source]
                    if not _type_compatible(ptype, spec.get("type")):
                        errors.append(_issue(
                            "TYPE_MISMATCH", n["id"],
                            f"输入 {spec.get('name')} 声明类型 {spec.get('type')} "
                            f"与来源 {source} 的类型 {ptype} 不兼容"))
                _resolve_source(source, n, available)

        elif ntype == "if":
            conditions = cfg.get("conditions")
            if not isinstance(conditions, list) or not conditions:
                conditions = [{
                    "variable": cfg.get("variable"),
                    "value_type": cfg.get("value_type"),
                    "value": cfg.get("value"),
                }]
            for cond in conditions:
                var = cond.get("variable")
                if isinstance(var, str) and var and var not in available \
                        and var not in ("index", "item", "len", "total"):
                    errors.append(_issue("UNDEFINED_VARIABLE", n["id"],
                                         f"IF 条件引用了未定义的变量: {var}"))
                if cond.get("value_type") == "variable":
                    ref = cond.get("value")
                    if isinstance(ref, str) and ref and ref not in available \
                            and ref not in ("index", "item", "len", "total"):
                        errors.append(_issue(
                            "UNDEFINED_VARIABLE", n["id"],
                            f"IF 比较引用了未定义的变量: {ref}"))

        elif ntype == "aggregate":
            inputs = cfg.get("inputs") or []
            direct_sources = {
                edge.get("source") for edge in in_edges.get(n["id"], [])
                if edge.get("source_handle") != RETRY_HANDLE
            }
            direct_variables = {
                name: producers[name]
                for name, (_, owner) in producers.items()
                if owner in direct_sources
            }
            expected_type = cfg.get("output_type")
            for raw in inputs:
                source = raw.get("source") if isinstance(raw, dict) else raw
                if not source:
                    errors.append(_issue(
                        "AGGREGATE_INPUT_REQUIRED", n["id"],
                        "聚合输入变量不能为空"))
                    continue
                if source not in direct_variables:
                    errors.append(_issue(
                        "AGGREGATE_INPUT_NOT_DIRECT", n["id"],
                        f"聚合输入 {source} 必须来自直接连接的上游节点"))
                    continue
                actual_type, _ = direct_variables[source]
                if actual_type is not None and actual_type != expected_type:
                    errors.append(_issue(
                        "TYPE_MISMATCH", n["id"],
                        f"聚合输入 {source} 的类型为 {actual_type}，必须与聚合类型 {expected_type} 一致"))

        elif ntype == "for":
            source = cfg.get("list_source")
            if isinstance(source, str) and source:
                if source in available:
                    ptype, _ = available[source]
                    if ptype is not None and ptype != "list":
                        errors.append(_issue(
                            "FOR_INPUT_NOT_LIST", n["id"],
                            f"For 列表来源 {source} 的类型为 {ptype}，"
                            f"必须是 list"))
                else:
                    errors.append(_issue(
                        "UNDEFINED_VARIABLE", n["id"],
                        f"For 列表来源变量未定义: {source}"))
            body = cfg.get("body") or {}
            body_nodes = body.get("nodes", []) or []
            body_edges = body.get("edges", []) or []
            body_producers = _build_producers(body_nodes)
            body_ref_producers = {**producers, **body_producers}
            for bn in body_nodes:
                body_available = _available_body_producers_for(
                    bn["id"], body_nodes, body_edges, available, n["id"])
                if bn.get("type") == "for":
                    errors.append(_issue("NESTED_FOR", n["id"],
                                         "For 循环体内不允许嵌套 For 节点"))
                if bn.get("type") == "code":
                    for spec in (bn.get("config", {}) or {}
                                 ).get("inputs", []) or []:
                        if "source" not in spec:
                            continue
                        source = spec.get("source")
                        if isinstance(source, str) and source in body_available:
                            ptype, _ = body_available[source]
                            if not _type_compatible(ptype, spec.get("type")):
                                errors.append(_issue(
                                    "TYPE_MISMATCH", bn["id"],
                                    f"输入 {spec.get('name')} 声明类型 {spec.get('type')} "
                                    f"与来源 {source} 的类型 {ptype} 不兼容"))
                        _resolve_source(source, bn, body_available,
                                        locals_allowed=True)
                elif bn.get("type") == "llm":
                    prompt = (bn.get("config", {}) or {}).get("prompt", "")
                    for ref in _template_var_refs(prompt, body_ref_producers):
                        if ref not in body_available:
                            errors.append(_issue(
                                "UNDEFINED_VARIABLE", bn["id"],
                                f"循环体 Prompt 引用了未定义的变量: {ref}"))
                elif bn.get("type") == "if":
                    bcfg = bn.get("config", {}) or {}
                    conditions = bcfg.get("conditions") or [{
                        "variable": bcfg.get("variable"),
                        "value_type": bcfg.get("value_type"),
                        "value": bcfg.get("value"),
                    }]
                    for cond in conditions:
                        var = cond.get("variable")
                        if isinstance(var, str) and var and var not in body_available:
                            errors.append(_issue(
                                "UNDEFINED_VARIABLE", bn["id"],
                                f"循环体 IF 条件引用了未定义的变量: {var}"))
                        if cond.get("value_type") == "variable":
                            ref = cond.get("value")
                            if isinstance(ref, str) and ref and ref not in body_available:
                                errors.append(_issue(
                                "UNDEFINED_VARIABLE", bn["id"],
                                f"循环体 IF 比较引用了未定义的变量: {ref}"))
                elif bn.get("type") == "aggregate":
                    bcfg = bn.get("config", {}) or {}
                    direct_sources = {
                        edge.get("source") for edge in body_edges
                        if edge.get("target") == bn["id"] and
                        edge.get("source_handle") != RETRY_HANDLE
                    }
                    for raw in bcfg.get("inputs", []) or []:
                        source = raw.get("source") if isinstance(raw, dict) else raw
                        producer = body_ref_producers.get(source)
                        if not source or not producer or producer[1] not in direct_sources:
                            errors.append(_issue(
                                "AGGREGATE_INPUT_NOT_DIRECT", bn["id"],
                                f"循环体聚合输入 {source or '<空>'} 必须来自直接连接的上游节点"))
                        elif producer[0] is not None and producer[0] != bcfg.get("output_type"):
                            errors.append(_issue(
                                "TYPE_MISMATCH", bn["id"],
                                f"循环体聚合输入 {source} 的类型必须与聚合类型一致"))

    return {"errors": errors, "warnings": warnings}
