"""工作流执行引擎（PRD 第 7 章）。

执行模型：事件驱动队列。节点完成时按结果触发出边（out/if/else/error/retry）。
节点的全部非 retry 入边源进入终态后，任一入边被触发则执行，否则标记
skipped；被跳过节点在其入边迟到触发时复活（retry 重入场景）。严格串行。
到达任一 End 即成功并停止。节点失败且无异常分支 -> 工作流失败；有异常分支
-> 写入 <节点id>-error-type/-error-message 并触发 error 边继续。
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from contextlib import contextmanager
from typing import Any, Callable, Optional
import uuid

from .command_format import format_step_command
from .context import VariableContext, render_template
from .models import Edge, Node, Workflow
from .variables import check_type
from .topo import topo_sort

Event = dict[str, Any]
OnEvent = Optional[Callable[[Event], None]]
TERMINAL = ("success", "failed", "skipped")
MAX_NODE_EXECUTIONS = 50
TASK_ID_VAR = "task-id"


class NodeFailure(Exception):
    def __init__(self, error_type: str, error_message: str):
        super().__init__(f"{error_type}: {error_message}")
        self.error_type = error_type
        self.error_message = error_message


class PromptExit(Exception):
    """Prompt nodes are Agent exits and pause the current automatic run."""

    def __init__(self, node_id: str, prompt: str):
        super().__init__(node_id)
        self.node_id = node_id
        self.prompt = prompt


def _classify(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, NodeFailure):
        return exc.error_type, exc.error_message
    if isinstance(exc, KeyError):
        return "VariableError", f"变量未定义: {exc}"
    if isinstance(exc, ValueError):
        return "VariableError", str(exc)
    if isinstance(exc, TypeError):
        return "TypeError", str(exc)
    return type(exc).__name__, str(exc)


def _node_var_base(node: Node) -> str:
    raw = str(node.name or node.id or "node").strip()
    raw = re.sub(r"\s+", "-", raw)
    value = re.sub(r"[^0-9A-Za-z_-]", "-", raw)
    value = re.sub(r"-+", "-", value).strip("-")
    if not value:
        value = str(node.id or "node")
    if value[0].isdigit():
        value = f"node-{value}"
    return value


def _safe_prefix(node: Node) -> str:
    return _node_var_base(node)


def _auto_output_name(node: Node) -> str:
    return f"{_node_var_base(node)}-output"


def _coerce_bool(value: Any, type_: str) -> Any:
    """系统无 bool 类型（PRD 第 9 章），bool 值按数值语义归一化为 int。

    ``_infer_type(True)`` 推断为 "int"，但 ``check_type(True, "int")``
    会拒绝 bool（bool 既不是 int 也不是 float）。为保证 Code 节点输出
    bool、For 遍历含 bool 的 list 等工作流能正常执行，写入上下文前把
    bool 归一化为 0/1。
    """
    if isinstance(value, bool):
        if type_ == "float":
            return float(int(value))
        if type_ == "int":
            return int(value)
    return value


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "int"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "string"


class Engine:
    def __init__(self, workflow: Workflow, llm_service=None, code_service=None):
        self.wf = workflow
        self.llm_service = llm_service
        self.code_service = code_service
        self._visible_owner_stack: list[set[str]] = []
        self._upstream_owners: dict[str, set[str]] = self._build_upstream_owners()

    async def run(self, inputs: dict[str, Any], on_event: OnEvent = None,
                  stream: bool = False) -> dict[str, Any]:
        t0 = time.perf_counter()
        ctx = VariableContext()
        task_id = str(inputs.get(TASK_ID_VAR) or inputs.get("task_id") or
                      f"task-{uuid.uuid4().hex[:12]}")
        ctx.define_system(TASK_ID_VAR, "string", task_id, "system")
        status: dict[str, str] = {n.id: "pending" for n in self.wf.nodes}
        records: dict[str, dict[str, Any]] = {}
        fired: set[tuple[str, str]] = set()
        queue: deque[str] = deque()
        queued: set[str] = set()
        exec_count: dict[str, int] = {}
        result: dict[str, Any] = {"status": "success", "failed_node": None,
                                  "end_node": None, "waiting_node": None,
                                  "waiting_prompt": None,
                                  "llm_call_count": 0,
                                  "task-id": task_id, "task_id": task_id}
        # 供节点处理器访问的运行态（Start 输入校验 / Aggregate 上游状态）
        self._run_inputs = inputs
        self._run_status = status

        def emit(ev: Event) -> None:
            if on_event is not None:
                on_event(ev)

        def schedule(nid: str, revive: bool = False) -> None:
            if nid in queued:
                return
            st = status.get(nid)
            if st == "skipped" or (revive and st == "failed"):
                # skipped：迟到触发 -> 复活
                # failed + revive：retry 边重入 -> 复活重试
                status[nid] = "pending"
                records.pop(nid, None)
                st = "pending"
            if st == "pending":
                queue.append(nid)
                queued.add(nid)

        def sweep() -> None:
            changed = True
            while changed:
                changed = False
                for n in self.wf.nodes:
                    st = status[n.id]
                    if st not in ("pending", "skipped") or n.id in queued:
                        continue
                    ins = self.wf.in_edges(n.id)
                    if not ins:
                        continue
                    if not all(status[e.source] in TERMINAL for e in ins):
                        continue
                    if any((e.source, e.source_handle) in fired for e in ins):
                        schedule(n.id)
                        changed = True
                    elif st == "pending":
                        status[n.id] = "skipped"
                        records[n.id] = {"status": "skipped", "duration_ms": 0}
                        changed = True

        try:
            order = topo_sort(self.wf.to_dict())
        except ValueError as e:
            result.update(status="failed")
            return self._finish(result, records, ctx, t0, emit, str(e))
        pos = {nid: i for i, nid in enumerate(order)}
        seeds = sorted([n.id for n in self.wf.nodes if not self.wf.in_edges(n.id)],
                       key=lambda x: pos.get(x, 0))
        for nid in seeds:
            schedule(nid)

        stopped = False
        while queue and not stopped:
            nid = queue.popleft()
            queued.discard(nid)
            if status.get(nid) != "pending":
                continue
            node = self.wf.node_by_id(nid)
            if node is None:
                continue
            exec_count[nid] = exec_count.get(nid, 0) + 1
            if exec_count[nid] > MAX_NODE_EXECUTIONS:
                result.update(status="failed", failed_node=nid)
                records[nid] = {"status": "failed", "duration_ms": 0,
                                "error_type": "RetryLimitError",
                                "error_message": "节点重试次数超过上限"}
                break
            status[nid] = "running"
            emit({"event": "node_started", "node_id": nid})
            started = time.perf_counter()
            record: dict[str, Any] = {}
            ctx.remove_owned(nid)  # retry 重入前清理上次写入
            paused = False
            try:
                with self._visibility_for(nid):
                    fire_handles = await self._execute(
                        node, ctx, record, emit, stream, result)
            except PromptExit as pause:
                fire_handles = set()
                paused = True
                result.update(status="waiting",
                              waiting_node=pause.node_id,
                              waiting_prompt=pause.prompt)
                if not record.get("status"):
                    record["status"] = (
                        "success" if pause.node_id == nid else "waiting")
                if pause.node_id != nid:
                    record["waiting_node"] = pause.node_id
            except Exception as exc:  # noqa: BLE001
                err_type, err_msg = _classify(exc)
                record.update(error_type=err_type, error_message=err_msg)
                record["status"] = "failed"
                if node.config.get("error_branch") and self._has_error_edge(nid):
                    prefix = _safe_prefix(node)
                    try:
                        ctx.define(f"{prefix}-error-type", "string", err_type, nid)
                        ctx.define(f"{prefix}-error-message", "string", err_msg, nid)
                    except (ValueError, TypeError):
                        pass
                    fire_handles = {"error"}
                else:
                    result.update(status="failed", failed_node=nid)
                    fire_handles = set()
            record["duration_ms"] = (time.perf_counter() - started) * 1000
            status[nid] = record["status"]
            records[nid] = record
            emit({"event": "node_finished", "node_id": nid,
                  "status": record["status"],
                  "duration_ms": record["duration_ms"]})
            if result["status"] == "failed":
                break
            if paused:
                break
            for edge in self.wf.out_edges(nid, include_retry=True):
                h = edge.source_handle
                if h not in fire_handles:
                    # retry 边在源节点成功（触发 out）时激活
                    if not (h == "retry" and "out" in fire_handles):
                        continue
                fired.add((edge.source, h))
                if h == "retry":
                    schedule(edge.target, revive=True)
            if node.type == "end" and record["status"] == "success":
                result["end_node"] = nid
                stopped = True
            sweep()

        if result["status"] != "waiting":
            for n in self.wf.nodes:  # 收尾：未执行节点标记 skipped
                if status[n.id] in ("pending", "running"):
                    status[n.id] = "skipped"
                    records.setdefault(n.id, {"status": "skipped",
                                              "duration_ms": 0})
        return self._finish(result, records, ctx, t0, emit)

    def _finish(self, result, records, ctx, t0, emit, error=None):
        result["variables"] = ctx.snapshot()
        result["node_records"] = records
        result["total_duration_ms"] = (time.perf_counter() - t0) * 1000
        if error:
            result["error"] = error
        emit({"event": "workflow_finished", "status": result["status"],
              "failed_node": result["failed_node"],
              "end_node": result["end_node"],
              "waiting_node": result.get("waiting_node"),
              "total_duration_ms": result["total_duration_ms"],
              "result": result})
        return result

    def _has_error_edge(self, nid: str) -> bool:
        return any(e.source_handle == "error"
                   for e in self.wf.out_edges(nid))

    def _build_upstream_owners(self) -> dict[str, set[str]]:
        reverse: dict[str, list[str]] = {n.id: [] for n in self.wf.nodes}
        for edge in self.wf.edges:
            if edge.source_handle == "retry":
                continue
            reverse.setdefault(edge.target, []).append(edge.source)
        out: dict[str, set[str]] = {}
        for node in self.wf.nodes:
            seen: set[str] = set()
            queue = deque(reverse.get(node.id, []))
            while queue:
                nid = queue.popleft()
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                queue.extend(reverse.get(nid, []))
            out[node.id] = {
                owner for owner in seen
                if self._branch_guaranteed_owner(owner, node.id)
            }
        return out

    def _out_map(self) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = {}
        for edge in self.wf.edges:
            if edge.source_handle == "retry":
                continue
            out.setdefault(edge.source, []).append(edge)
        return out

    def _can_reach(self, from_id: str, target_id: str,
                   out: dict[str, list[Edge]],
                   seen: Optional[set[str]] = None) -> bool:
        if not from_id:
            return False
        if from_id == target_id:
            return True
        seen = seen or set()
        if from_id in seen:
            return False
        seen.add(from_id)
        return any(self._can_reach(edge.target, target_id, out, seen)
                   for edge in out.get(from_id, []))

    def _branch_guaranteed_owner(self, owner_id: str,
                                 target_id: str) -> bool:
        out = self._out_map()
        for node in self.wf.nodes:
            if node.type != "if":
                continue
            branches = [
                edge for edge in out.get(node.id, [])
                if edge.source_handle == "else" or
                str(edge.source_handle or "").startswith("if")
            ]
            branches_to_target = [
                edge for edge in branches
                if self._can_reach(edge.target, target_id, out)
            ]
            if len(branches_to_target) < 2:
                continue
            owner_branches = [
                edge for edge in branches_to_target
                if self._can_reach(edge.target, owner_id, out)
            ]
            if 0 < len(owner_branches) < len(branches_to_target):
                return False
        return True

    @contextmanager
    def _visibility_for(self, node_id: str):
        owners = set(self._upstream_owners.get(node_id, set()))
        owners.add("system")
        self._visible_owner_stack.append(owners)
        try:
            yield
        finally:
            self._visible_owner_stack.pop()

    @contextmanager
    def _visibility(self, owners: set[str]):
        scoped = set(owners)
        scoped.add("system")
        self._visible_owner_stack.append(scoped)
        try:
            yield
        finally:
            self._visible_owner_stack.pop()

    def _var_visible(self, ctx: VariableContext, name: str) -> bool:
        if not ctx.has(name):
            return False
        if not self._visible_owner_stack:
            return True
        try:
            owner = ctx.owner_of(name)
        except KeyError:
            return False
        return owner in self._visible_owner_stack[-1]

    def _visible_has(self, ctx: VariableContext, name: str) -> bool:
        return ctx.has(name) and self._var_visible(ctx, name)

    def _visible_values(self, ctx: VariableContext) -> dict[str, Any]:
        return {
            name: ctx.get(name)
            for name in ctx.names()
            if self._var_visible(ctx, name)
        }

    async def _execute(self, node: Node, ctx: VariableContext,
                       record: dict, emit, stream: bool,
                       result: dict) -> set[str]:
        """执行单节点，返回应触发的出边 handle 集合。"""
        handler = getattr(self, f"_run_{node.type}", None)
        if handler is None:
            raise NodeFailure("ConfigError", f"未知节点类型: {node.type}")
        return await handler(node, ctx, record, emit, stream, result)

    # ------------------------------------------------------------------
    async def _run_start(self, node, ctx, record, emit, stream, result):
        for spec in node.config.get("inputs", []):
            name, type_ = spec["name"], spec["type"]
            if name not in self._run_inputs:
                raise NodeFailure("InputError", f"缺少必需输入变量: {name}")
            value = self._run_inputs[name]
            try:
                ctx.define(name, type_, value, node.id)
            except TypeError:
                raise NodeFailure(
                    "InputError",
                    f"输入变量 {name} 声明类型 {type_}，实际值 {value!r} 不符")
        record["status"] = "success"
        return {"out"}

    async def _run_end(self, node, ctx, record, emit, stream, result):
        record["status"] = "success"
        return set()

    async def _run_code(self, node, ctx, record, emit, stream, result):
        if self.code_service is None:
            raise NodeFailure("ConfigError", "未配置 Code 执行服务")
        cfg = node.config
        args = {}
        for spec in cfg.get("inputs", []):
            name = spec.get("name")
            if not name:
                raise NodeFailure("ConfigError", "Code 输入参数名不能为空")
            src = spec.get("source") or name
            if self._visible_has(ctx, src):
                value = ctx.get(src)
            elif spec.get("required", True) is False:
                value = None
            else:
                raise NodeFailure(
                    "VariableError",
                    f"输入参数 {name} 未提供（期望变量或 step 参数: {src}）")
            type_ = spec.get("type")
            if "source" not in spec and type_ and not check_type(value, type_):
                raise NodeFailure(
                    "TypeError",
                    f"输入参数 {name} 声明类型 {type_}，实际值 {value!r} 不符")
            args[name] = value
        resp = self.code_service.run(cfg.get("code", ""), args,
                                     timeout=cfg.get("timeout", 30),
                                     node_id=node.id)
        record["stdout"] = resp.get("stdout", "")
        record["stderr"] = resp.get("stderr", "")
        if not resp.get("ok"):
            raise NodeFailure(resp.get("error_type", "RuntimeError"),
                              resp.get("error_message", "脚本执行失败"))
        returned = resp.get("result")
        if not isinstance(returned, dict):
            raise NodeFailure("ContractError",
                              "脚本 main 必须返回 dict，例如 {\"result\": <值>}")
        declared = [out for out in (cfg.get("outputs", []) or [])
                    if out.get("name")]
        declared_by_name = {out["name"]: out for out in declared}
        if (len(declared) == 1 and set(returned) == {"result"} and
                declared[0]["name"] != "result"):
            # 兼容旧工作流：单输出节点曾允许把 return {"result": v}
            # 写入一个自定义声明名。
            outputs = declared
            values = {declared[0]["name"]: returned["result"]}
        else:
            outputs = [
                {
                    "name": key,
                    "type": declared_by_name.get(key, {}).get("type") or
                    _infer_type(value),
                }
                for key, value in returned.items()
            ]
            values = dict(returned)
        record["result"] = returned
        record["outputs"] = {}
        for out in outputs:
            name = out["name"]
            value = _coerce_bool(values[name], out["type"] or "")
            type_ = out["type"] or _infer_type(value)
            try:
                ctx.define_system(name, type_, value, node.id)
            except TypeError:
                raise NodeFailure(
                    "TypeError",
                    f"输出 {name} 声明类型 {type_}，实际值 {value!r} 不符")
            record["outputs"][name] = value
        record["status"] = "success"
        return {"out"}

    async def _run_llm(self, node, ctx, record, emit, stream, result):
        cfg = node.config
        try:
            prompt = render_template(
                cfg.get("prompt", ""), self._visible_values(ctx))
        except KeyError as e:
            raise NodeFailure("TemplateError", str(e))
        prompt = self._append_loop_context(prompt, ctx)
        prompt = self._append_task_context(prompt, ctx)
        prompt = self._append_next_step_context(prompt, node, ctx)
        record["prompt"] = prompt
        record["prompt_output"] = prompt
        record["status"] = "success"
        raise PromptExit(node.id, prompt)

    async def _run_if(self, node, ctx, record, emit, stream, result):
        cfg = node.config
        raw_conditions = cfg.get("conditions")
        conditions = raw_conditions if isinstance(raw_conditions, list) and raw_conditions else [{
            "variable": cfg.get("variable"),
            "operator": cfg.get("operator"),
            "value": cfg.get("value"),
            "value_type": cfg.get("value_type", "constant"),
        }]
        results = [self._eval_condition(cond, ctx) for cond in conditions]
        matched_index = next((index for index, passed in enumerate(results)
                              if passed), None)
        # 旧工作流用 if/else + 条件组合；新版逐条件匹配并走 if-1…if-n。
        legacy = cfg.get("branch_mode") != "multi" and any(
            edge.source_handle == "if" for edge in self.wf.out_edges(node.id))
        cond = (any(results) if cfg.get("combinator") == "or" else all(results)) \
            if legacy else matched_index is not None
        record["condition_results"] = results
        record["condition_result"] = cond
        if matched_index is not None:
            record["matched_condition"] = matched_index + 1
        record["status"] = "success"
        if legacy:
            return {"if"} if cond else {"else"}
        return {f"if-{matched_index + 1}"} if matched_index is not None else {"else"}

    def _eval_condition(self, cfg: dict, ctx: VariableContext) -> bool:
        var = cfg.get("variable")
        if not var or not self._visible_has(ctx, var):
            raise NodeFailure("VariableError", f"IF 条件变量未定义: {var}")
        actual = ctx.get(var)
        op = cfg.get("operator")
        if op in ("为空", "不为空"):
            empty = actual is None or (
                isinstance(actual, (str, list, dict)) and len(actual) == 0)
            cond = empty if op == "为空" else not empty
        else:
            if cfg.get("value_type", "constant") == "variable":
                ref = cfg.get("value")
                if not ref or not self._visible_has(ctx, ref):
                    raise NodeFailure("VariableError",
                                      f"IF 比较变量未定义: {ref}")
                cmp_val = ctx.get(ref)
            else:
                cmp_val = cfg.get("value")
            try:
                if op == "包含":
                    cond = cmp_val in actual
                elif op == "不包含":
                    cond = cmp_val not in actual
                elif op == "开始是":
                    cond = str(actual).startswith(str(cmp_val))
                elif op == "结束是":
                    cond = str(actual).endswith(str(cmp_val))
                elif op == "是":
                    cond = actual == cmp_val
                elif op == "不是":
                    cond = actual != cmp_val
                else:
                    raise NodeFailure("ConfigError", f"未知 IF 运算符: {op}")
            except TypeError as e:
                raise NodeFailure("TypeError", f"IF 比较类型不匹配: {e}")
        return cond

    async def _run_for(self, node, ctx, record, emit, stream, result):
        cfg = node.config
        source = cfg.get("list_source")
        if not source or not self._visible_has(ctx, source):
            raise NodeFailure("VariableError",
                              f"For 列表来源变量未定义: {source}")
        items = ctx.get(source)
        if not isinstance(items, list):
            raise NodeFailure(
                "TypeError",
                f"For 列表来源 {source} 的类型必须是 list，"
                f"实际为 {type(items).__name__}")
        body = cfg.get("body") or {}
        body_nodes = [Node.from_dict(d) for d in (body.get("nodes") or [])]
        body_edges = [Edge.from_dict(d) for d in (body.get("edges") or [])]
        collect_name = cfg.get("collect") or self._infer_body_collect(
            body_nodes, body_edges)
        collected: list[Any] = []
        for idx, item in enumerate(items):
            child = VariableContext(parent=ctx)
            child.define_system("index", "int", idx, node.id, local_only=True)
            item_type = _infer_type(item)
            item = _coerce_bool(item, item_type)
            child.define_system("item", item_type, item, node.id,
                                local_only=True)
            child.define_system("len", "int", len(items), node.id,
                                local_only=True)
            child.define_system("total", "int", len(items), node.id,
                                local_only=True)
            if body_nodes:
                await self._run_body(body_nodes, body_edges, child,
                                     emit, stream, result, node.id)
            if not body_nodes:
                collected.append(item)
                continue
            if not collect_name or not child.has(collect_name):
                raise NodeFailure(
                    "ConfigError",
                    f"For 自动收集变量 {collect_name!r} 未在循环体中产生")
            collected.append(child.get(collect_name))
        ctx.define_system(_auto_output_name(node), "list", collected, node.id)
        record["collect"] = collect_name or "item"
        record["status"] = "success"
        return {"out"}

    def _append_loop_context(self, prompt: str, ctx: VariableContext) -> str:
        if not (ctx.has("index") and (ctx.has("len") or ctx.has("total"))):
            return prompt
        idx = ctx.get("index")
        total = ctx.get("len") if ctx.has("len") else ctx.get("total")
        try:
            current = int(idx) + 1
            total_i = int(total)
        except (TypeError, ValueError):
            return prompt
        item_line = ""
        if ctx.has("item"):
            item = ctx.get("item")
            if isinstance(item, (list, dict)):
                item_text = json.dumps(item, ensure_ascii=False)
            else:
                item_text = str(item)
            if len(item_text) > 300:
                item_text = item_text[:300] + "..."
            item_line = f"\n当前 item: {item_text}"
        return (
            prompt.rstrip()
            + f"\n\n---\n循环上下文: 当前轮次 {current}/{total_i}"
            + item_line
        )

    def _append_task_context(self, prompt: str,
                             ctx: VariableContext) -> str:
        if not ctx.has(TASK_ID_VAR):
            return prompt
        task_id = ctx.get(TASK_ID_VAR)
        if not task_id:
            return prompt
        return prompt.rstrip() + f"\n\n---\ntask-id: {task_id}"

    def _append_next_step_context(self, prompt: str, node: Node,
                                  ctx: VariableContext) -> str:
        entries = self._next_code_entries(node.id)
        if not entries:
            return prompt
        task_id = ctx.get(TASK_ID_VAR) if ctx.has(TASK_ID_VAR) else "<task-id>"
        schema_md = self._format_next_code_schema(entries)
        first = entries[0]
        step_param = {
            spec["name"]: f"<{spec['type']}>"
            for spec in first["input_schema"]
        }
        command = format_step_command(
            node.config.get("command_context"),
            str(task_id),
            first["step_id"],
            step_param,
        )
        rendered = (
            "-------------\n"
            "## 下一个step待执行命令:\n\n"
            f"{command}\n"
            "-------------\n"
            "**step-param 入参说明**:\n\n"
            f"{schema_md}\n"
            "-------------"
        )
        return prompt.rstrip() + "\n\n" + rendered

    def _next_code_entries(self, prompt_id: str) -> list[dict[str, Any]]:
        by_id = {n.id: n for n in self.wf.nodes}
        out_map: dict[str, list[Edge]] = {}
        for edge in self.wf.edges:
            if edge.source_handle in ("error", "retry"):
                continue
            out_map.setdefault(edge.source, []).append(edge)
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        queue = [edge.target for edge in out_map.get(prompt_id, [])]
        while queue:
            nid = queue.pop(0)
            if not nid or nid in seen:
                continue
            seen.add(nid)
            next_node = by_id.get(nid)
            if next_node is None:
                continue
            if next_node.type == "code":
                found.append({
                    "step_id": nid,
                    "node_name": next_node.name or nid,
                    "input_schema": self._code_input_schema(next_node),
                })
                continue
            if next_node.type in ("llm", "end"):
                continue
            queue.extend(edge.target for edge in out_map.get(nid, []))
        return found

    @staticmethod
    def _code_input_schema(node: Node) -> list[dict[str, Any]]:
        return [{
            "name": spec.get("name") or f"arg-{i + 1}",
            "description": spec.get("description") or "",
            "type": spec.get("type") or "string",
            "required": spec.get("required", True) is not False,
        } for i, spec in enumerate((node.config or {}).get("inputs", []) or [])
          if not spec.get("source")]

    @staticmethod
    def _format_next_code_schema(entries: list[dict[str, Any]]) -> str:
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
                    f"{spec['description']} |")
            lines.append("")
        return "\n".join(lines).rstrip()

    def _infer_body_collect(self, body_nodes: list[Node],
                            body_edges: list[Edge]) -> str:
        if not body_nodes:
            return "item"
        outgoing = {e.source for e in body_edges}
        terminal = next((n for n in reversed(body_nodes)
                         if n.id not in outgoing), body_nodes[-1])
        if terminal.type == "code":
            outputs = terminal.config.get("outputs", []) or []
            return outputs[0].get("name", "") if outputs else ""
        if terminal.type == "aggregate":
            return _auto_output_name(terminal)
        return ""

    async def _run_body(self, body_nodes, body_edges, child,
                        emit, stream, result, for_node_id: str) -> None:
        """执行 For 循环体子图：零入度种子 + 边触发，任一节点失败即中断。"""
        if not body_nodes:
            return
        by_id = {n.id: n for n in body_nodes}
        in_map: dict[str, list[Edge]] = {n.id: [] for n in body_nodes}
        out_map: dict[str, list[Edge]] = {}
        reverse: dict[str, list[str]] = {n.id: [] for n in body_nodes}
        for e in body_edges:
            in_map.setdefault(e.target, []).append(e)
            out_map.setdefault(e.source, []).append(e)
            if e.source_handle != "retry":
                reverse.setdefault(e.target, []).append(e.source)
        done: set[str] = set()
        queue: deque[str] = deque(
            n.id for n in body_nodes if not in_map.get(n.id))
        while queue:
            nid = queue.popleft()
            if nid in done:
                continue
            bnode = by_id[nid]
            emit({"event": "node_started", "node_id": nid})
            rec: dict[str, Any] = {}
            try:
                owners = set(self._visible_owner_stack[-1]) \
                    if self._visible_owner_stack else {"system"}
                owners.add(for_node_id)
                owners.update(self._body_upstream_owners(
                    nid, reverse, body_nodes, out_map))
                with self._visibility(owners):
                    handles = await self._execute(bnode, child, rec,
                                                  emit, stream, result)
            except PromptExit:
                emit({"event": "node_finished", "node_id": nid,
                      "status": rec.get("status", "success"),
                      "duration_ms": rec.get("duration_ms", 0)})
                raise
            except Exception as exc:  # noqa: BLE001
                has_error_edge = (
                    bnode.type == "code" and
                    bnode.config.get("error_branch") and
                    any(edge.source_handle == "error"
                        for edge in out_map.get(nid, [])))
                if not has_error_edge:
                    err_type, err_msg = _classify(exc)
                    emit({"event": "node_finished", "node_id": nid,
                          "status": "failed", "duration_ms": 0})
                    raise NodeFailure(err_type, err_msg)
                err_type, err_msg = _classify(exc)
                prefix = _safe_prefix(bnode)
                try:
                    child.define_system(f"{prefix}-error-type", "string",
                                        err_type, bnode.id)
                    child.define_system(f"{prefix}-error-message", "string",
                                        err_msg, bnode.id)
                except (TypeError, ValueError):
                    pass
                rec.update(status="failed", error_type=err_type,
                           error_message=err_msg)
                handles = {"error"}
            emit({"event": "node_finished", "node_id": nid,
                  "status": rec.get("status", "success"),
                  "duration_ms": rec.get("duration_ms", 0)})
            done.add(nid)
            for e in out_map.get(nid, []):
                if e.source_handle in handles and e.target not in done:
                    queue.append(e.target)

    def _body_upstream_owners(self, node_id: str,
                              reverse: dict[str, list[str]],
                              body_nodes: list[Node],
                              out_map: dict[str, list[Edge]]) -> set[str]:
        seen: set[str] = set()
        queue = deque(reverse.get(node_id, []))
        while queue:
            nid = queue.popleft()
            if not nid or nid in seen:
                continue
            seen.add(nid)
            queue.extend(reverse.get(nid, []))
        return {
            owner for owner in seen
            if self._body_branch_guaranteed_owner(
                owner, node_id, body_nodes, out_map)
        }

    def _body_can_reach(self, from_id: str, target_id: str,
                        out_map: dict[str, list[Edge]],
                        seen: Optional[set[str]] = None) -> bool:
        if not from_id:
            return False
        if from_id == target_id:
            return True
        seen = seen or set()
        if from_id in seen:
            return False
        seen.add(from_id)
        return any(self._body_can_reach(edge.target, target_id,
                                        out_map, seen)
                   for edge in out_map.get(from_id, []))

    def _body_branch_guaranteed_owner(
            self, owner_id: str, target_id: str,
            body_nodes: list[Node],
            out_map: dict[str, list[Edge]]) -> bool:
        for node in body_nodes:
            if node.type != "if":
                continue
            branches = [
                edge for edge in out_map.get(node.id, [])
                if edge.source_handle == "else" or
                str(edge.source_handle or "").startswith("if")
            ]
            branches_to_target = [
                edge for edge in branches
                if self._body_can_reach(edge.target, target_id, out_map)
            ]
            if len(branches_to_target) < 2:
                continue
            owner_branches = [
                edge for edge in branches_to_target
                if self._body_can_reach(edge.target, owner_id, out_map)
            ]
            if 0 < len(owner_branches) < len(branches_to_target):
                return False
        return True

    async def _run_aggregate(self, node, ctx, record, emit, stream, result):
        cfg = node.config
        output_type = cfg.get("output_type")
        values: list[Any] = []
        selected = [
            raw.get("source") if isinstance(raw, dict) else raw
            for raw in (cfg.get("inputs") or [])
        ]
        selected = [name for name in selected if name]
        selected_set = set(selected)
        direct_sources = {edge.source for edge in self.wf.in_edges(node.id)
                          if edge.source_handle != "retry"}
        for e in self.wf.in_edges(node.id):
            if e.source_handle == "retry":
                continue
            if self._run_status.get(e.source) != "success":
                continue  # 未命中的分支不参与聚合
            for name in self._node_output_names(e.source):
                if selected and name not in selected_set:
                    continue
                if not ctx.has(name):
                    continue
                value = ctx.get(name)
                if not check_type(value, output_type):
                    raise NodeFailure(
                        "TypeError",
                        f"聚合输入 {name} 的类型与输出类型 {output_type} 不一致")
                values.append(value)
        if selected:
            available_names = {
                name for source_id in direct_sources
                for name in self._node_output_names(source_id)
            }
            invalid = [name for name in selected if name not in available_names]
            if invalid:
                raise NodeFailure(
                    "ConfigError",
                    f"聚合输入必须来自直接连接的上游节点: {', '.join(invalid)}")
        record["inputs"] = selected
        if output_type == "string":
            agg: Any = "\n".join(values)
        elif output_type in ("int", "float"):
            agg = sum(values)
        elif output_type == "list":
            agg = []
            for v in values:
                agg.extend(v)
        elif output_type == "dict":
            agg = {}
            for v in values:
                agg.update(v)
        else:
            raise NodeFailure("ConfigError",
                              f"未知聚合输出类型: {output_type}")
        ctx.define_system(_auto_output_name(node), output_type, agg, node.id)
        record["status"] = "success"
        return {"out"}

    def _node_output_names(self, source_id: str) -> list[str]:
        """上游节点产出的变量名集合（用于 Aggregate 输入解析）。"""
        n = self.wf.node_by_id(source_id)
        if n is None:
            return []
        if n.type == "code":
            return [o["name"] for o in n.config.get("outputs", [])]
        if n.type in ("for", "aggregate"):
            return [_auto_output_name(n)]
        if n.type == "start":
            return [i["name"] for i in n.config.get("inputs", [])]
        return []
