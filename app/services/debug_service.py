"""单节点调试服务（PRD 4.2 单节点调试 + 7.3 运行规则）。

- 只执行目标节点本身，绝不执行上游节点
- 入参缓存：调试过的节点再次调试可免传入参（页面刷新 clear_cache）
- 调试结果独立返回，不写入正式全流程运行上下文
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

from ..engine.context import VariableContext
from ..engine.naming import python_param_name
from ..engine.variables import check_type


def _coerce_bool(value: Any, type_: str) -> Any:
    """系统无 bool 类型，bool 值按数值语义归一化为 int（同 executor）。"""
    if isinstance(value, bool):
        if type_ == "float":
            return float(int(value))
        if type_ == "int":
            return int(value)
    return value


class MissingDebugInputsError(Exception):
    """节点从未调试过且未提供入参。"""


def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "int"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return "string"


class DebugService:
    def __init__(self, code_service: Any = None, llm_service: Any = None):
        self.code_service = code_service
        self.llm_service = llm_service
        self._input_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """清空全部调试入参缓存（对应页面刷新）。"""
        self._input_cache.clear()

    # ------------------------------------------------------------------
    def debug_node(self, wf: dict, node_id: str,
                   inputs: Optional[dict] = None) -> dict:
        """同步调试（适用于 Code 节点；LLM 节点请用 debug_node_async）。"""
        node = self._find_node(wf, node_id)
        if node.get("type") == "llm":
            return asyncio.run(
                self.debug_node_async(wf, node_id, inputs=inputs))
        inputs, cache_hit = self._resolve_inputs(wf, node_id, inputs)
        return self._debug_code(node, inputs, cache_hit)

    async def debug_node_async(self, wf: dict, node_id: str,
                               inputs: Optional[dict] = None) -> dict:
        """异步调试（Code / LLM 节点均适用）。"""
        node = self._find_node(wf, node_id)
        inputs, cache_hit = self._resolve_inputs(wf, node_id, inputs)
        if node.get("type") == "llm":
            return await self._debug_llm(node, inputs, cache_hit)
        return self._debug_code(node, inputs, cache_hit)

    # ------------------------------------------------------------------
    def _find_node(self, wf: dict, node_id: str) -> dict:
        for n in wf.get("nodes", []) or []:
            if n.get("id") == node_id:
                return n
            # For body nodes live in the nested graph stored on the parent
            # node.  Debugging executes the selected node in isolation, so
            # recursively locating its config is sufficient here.
            body = (n.get("config", {}) or {}).get("body")
            if isinstance(body, dict):
                try:
                    return self._find_node(body, node_id)
                except KeyError:
                    pass
        raise KeyError(f"节点不存在: {node_id}")

    def _resolve_inputs(self, wf: dict, node_id: str,
                        inputs: Optional[dict]) -> tuple[dict, bool]:
        """返回 (入参, 是否命中缓存)。"""
        if inputs is not None:
            resolved = dict(inputs)
            self._input_cache[node_id] = resolved
            return resolved, False
        cached = self._input_cache.get(node_id)
        if cached is not None:
            return dict(cached), True
        raise MissingDebugInputsError(
            f"节点 {node_id} 调试必须提供入参")

    # ------------------------------------------------------------------
    def _debug_code(self, node: dict, inputs: dict,
                    cache_hit: bool) -> dict:
        if self.code_service is None:
            raise RuntimeError("未配置 Code 执行服务")
        cfg = node.get("config", {}) or {}
        args = {}
        for spec in cfg.get("inputs", []) or []:
            name = spec.get("name")
            if not name:
                raise ValueError("Code 输入参数名不能为空")
            legacy_name = python_param_name(name)
            if name in inputs:
                value = inputs.get(name)
            elif legacy_name in inputs:
                value = inputs.get(legacy_name)
            elif spec.get("required", True) is False:
                value = None
            else:
                raise MissingDebugInputsError(
                    f"节点 {node['id']} 缺少必填调试参数: {name}")
            type_ = spec.get("type")
            if type_ and not check_type(value, type_):
                raise TypeError(
                    f"调试参数 {name} 声明类型 {type_}，实际值 {value!r} 不符")
            args[name] = value
        resp = self.code_service.run(cfg.get("code", ""), args,
                                     timeout=cfg.get("timeout", 30),
                                     node_id=node["id"])
        return {
            "ok": resp.get("ok", False),
            "result": resp.get("result"),
            "stdout": resp.get("stdout", ""),
            "stderr": resp.get("stderr", ""),
            "error_type": resp.get("error_type"),
            "error_message": resp.get("error_message"),
            "cache_hit": cache_hit,
            "isolated": True,
        }

    async def _debug_llm(self, node: dict, inputs: dict,
                         cache_hit: bool) -> dict:
        cfg = node.get("config", {}) or {}
        ctx = VariableContext()
        for name, value in inputs.items():
            type_ = _infer_type(value)
            value = _coerce_bool(value, type_)
            ctx.define_system(name, type_, value, "debug")
        prompt = ctx.render(cfg.get("prompt", ""))
        return {
            "ok": True,
            "prompt": prompt,
            "cache_hit": cache_hit,
            "isolated": True,
        }
