"""依赖注入钩子（测试中可 monkeypatch 替换 LLM 服务）。"""
from __future__ import annotations

from typing import Any


class CodeService:
    """面向引擎的 Code 执行服务：委托给子进程执行器。"""

    def __init__(self, python_path: str | None = None):
        self.python_path = python_path

    def run(self, code: str, args: dict, timeout: int = 30,
            node_id: str = "") -> dict:
        from .services.code_runner import run_code
        return run_code(code, args, timeout=timeout,
                        python_path=self.python_path, node_id=node_id)


def build_code_service(config: dict[str, Any]) -> CodeService:
    return CodeService(config.get("python_path"))


def build_llm_service(config: dict[str, Any]):
    from .services.llm_client import LLMClient
    return LLMClient(config)
