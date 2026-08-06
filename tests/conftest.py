"""共享 fixtures：工作流构建工厂、假 LLM 服务、临时目录。

测试即契约：所有测试针对 backend.app 下的公开接口编写，
实现尚未存在（TDD 红灯阶段）。
"""
from __future__ import annotations

import copy
import inspect
import sys
from pathlib import Path

import pytest
import yaml

# 保证可以 import app.*
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---------------------------------------------------------------------------
# 工作流构建工厂
# ---------------------------------------------------------------------------

def make_node(node_id, type_, name=None, config=None):
    return {
        "id": node_id,
        "type": type_,
        "name": name or node_id,
        "config": config or {},
    }


def make_edge(source, target, source_handle="out", edge_id=None):
    return {
        "id": edge_id or f"e-{source}-{target}-{source_handle}",
        "source": source,
        "target": target,
        "source_handle": source_handle,
    }


def make_workflow(nodes, edges, wf_id="wf-test", name="测试工作流"):
    return {"id": wf_id, "name": name, "nodes": nodes, "edges": edges}


def start_node(node_id="start-1", inputs=None):
    """按已确认决策：Start 可声明输入变量，可被下游引用。"""
    return make_node(node_id, "start", "Start",
                     {"inputs": inputs if inputs is not None
                      else [{"name": "user_input", "type": "string"}]})


def end_node(node_id="end-1"):
    return make_node(node_id, "end", "End", {})


def code_node(node_id="code-1", inputs=None, outputs=None, code=None,
              error_branch=False, name=None):
    return make_node(
        node_id, "code", name or node_id,
        {
            "inputs": inputs if inputs is not None else [
                {"name": "user_input", "type": "string", "source": "user_input"}
            ],
            "outputs": outputs if outputs is not None else [
                {"name": "result", "type": "string"}
            ],
            "code": code if code is not None else (
                "def main(user_input):\n"
                "    return {\"result\": user_input}\n"
            ),
            "error_branch": error_branch,
        },
    )


def llm_node(node_id="llm-1", prompt="你好 {{ user_input }}", name=None):
    return make_node(node_id, "llm", name or node_id, {"prompt": prompt})


def if_node(node_id="if-1", variable="user_input", operator="不为空",
            value=None, value_type="constant"):
    cfg = {"variable": variable, "operator": operator}
    if operator not in ("为空", "不为空"):
        cfg["value"] = value if value is not None else "x"
        cfg["value_type"] = value_type  # constant | variable
    return make_node(node_id, "if", node_id, cfg)


def for_node(node_id="for-1", list_source="items", body_nodes=None,
             body_edges=None, name=None):
    """循环体子图：body 内必须含 body-start / body-end 虚拟边界节点。"""
    body_nodes = body_nodes if body_nodes is not None else []
    body_edges = body_edges if body_edges is not None else []
    return make_node(
        node_id, "for", name or node_id,
        {
            "list_source": list_source,
            "body": {"nodes": body_nodes, "edges": body_edges},
        },
    )


def aggregate_node(node_id="agg-1", output_type="string", name=None):
    """输入变量由连线解析；必须同类型；输出自动命名。"""
    return make_node(node_id, "aggregate", name or node_id,
                     {"output_type": output_type})


@pytest.fixture
def factories():
    return {
        "node": make_node,
        "edge": make_edge,
        "workflow": make_workflow,
        "start": start_node,
        "end": end_node,
        "code": code_node,
        "llm": llm_node,
        "if_": if_node,
        "for_": for_node,
        "aggregate": aggregate_node,
    }


@pytest.fixture
def simple_workflow(factories):
    """start -> code -> end 的最小可运行工作流。"""
    return factories["workflow"](
        [factories["start"](), factories["code"](), factories["end"]()],
        [factories["edge"]("start-1", "code-1"),
         factories["edge"]("code-1", "end-1")],
    )


@pytest.fixture
def linear_llm_workflow(factories):
    """start -> llm -> end。"""
    return factories["workflow"](
        [factories["start"](), factories["llm"](), factories["end"]()],
        [factories["edge"]("start-1", "llm-1"),
         factories["edge"]("llm-1", "end-1")],
    )


# ---------------------------------------------------------------------------
# 假 LLM 服务（引擎测试注入，替代真实 HTTP 调用）
# ---------------------------------------------------------------------------

class FakeLLMService:
    """记录 prompt、返回预设内容，可模拟失败与流式。"""

    def __init__(self, reply="LLM回复", thinking="", usage=None, failures=None):
        self.reply = reply
        self.thinking = thinking
        self.usage = usage or {"prompt_tokens": 10, "completion_tokens": 5,
                               "thinking_tokens": None}
        self.calls: list[str] = []
        # failures: 调用次数 -> 异常（前 N 次失败）
        self.failures = list(failures or [])

    async def complete(self, prompt: str, node_id: str = "", **kwargs):
        self.calls.append(prompt)
        if self.failures:
            raise self.failures.pop(0)
        return {
            "content": self.reply,
            "thinking": self.thinking,
            "usage": self.usage,
        }

    async def stream(self, prompt: str, node_id: str = "", **kwargs):
        self.calls.append(prompt)
        if self.failures:
            raise self.failures.pop(0)
        for ch in self.reply:
            yield ch

    async def complete_events(self, prompt: str, messages=None, **kwargs):
        """模拟 Agent 流式：产出单轮模型回合与最终结果。"""
        self.calls.append(prompt)
        if self.failures:
            raise self.failures.pop(0)
        model_item = {
            "type": "model",
            "turn": 1,
            "content": self.reply,
            "thinking": self.thinking,
            "tool_calls": [],
            "usage": self.usage,
        }
        yield {"event": "agent_model", "item": model_item}
        msgs = list(messages or []) + [{"role": "user", "content": prompt}]
        yield {"event": "agent_final", "response": {
            "content": self.reply,
            "thinking": self.thinking,
            "usage": self.usage,
            "tool_results": [],
            "trace": [model_item],
            "messages": msgs,
        }}


@pytest.fixture
def fake_llm():
    return FakeLLMService()


# ---------------------------------------------------------------------------
# 代码执行服务（引擎测试注入，真实子进程执行器在 test_code_runner 单测）
# ---------------------------------------------------------------------------

class InlineCodeService:
    """在当前进程内执行 Code 节点代码（引擎测试用，不走子进程）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def run(self, code: str, args: dict, timeout: int = 30, node_id: str = ""):
        self.calls.append((code, dict(args)))
        ns: dict = {}
        try:
            exec(compile(code, f"<{node_id}>", "exec"), ns)
            main = ns["main"]
            sig = inspect.signature(main)
            params = list(sig.parameters.values())
            if len(params) == 1 and params[0].name == "params":
                result = main(dict(args))
            else:
                from app.engine.naming import python_arg_map
                result = main(**python_arg_map(args))
            return {"ok": True, "result": result, "stdout": "", "stderr": "",
                    "error_type": None, "error_message": None, "duration_ms": 1}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "result": None, "stdout": "", "stderr": "",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc), "duration_ms": 1}


@pytest.fixture
def inline_code():
    return InlineCodeService()


# ---------------------------------------------------------------------------
# 引擎 / 配置 fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def engine_factory(fake_llm, inline_code):
    """构造引擎：注入假 LLM 与内联代码服务。"""
    from app.engine.executor import Engine  # TDD：尚不存在
    from app.engine.models import Workflow  # TDD：尚不存在

    def build(workflow_dict):
        wf = Workflow.from_dict(copy.deepcopy(workflow_dict))
        return Engine(wf, llm_service=fake_llm, code_service=inline_code)

    return build


@pytest.fixture
def default_config():
    return {
        "python_path": sys.executable,
        "providers": {
            "openai": {"api_key": "", "base_url": "https://api.openai.com/v1"},
            "anthropic": {"api_key": "", "base_url": "https://api.anthropic.com"},
            "compatible": {"api_key": "", "base_url": "http://localhost:8000/v1"},
        },
        "default_provider": "compatible",
        "default_model": "test-model",
        "stream": True,
        "timeout_seconds": 60,
        "max_retries": 2,
    }


@pytest.fixture
def yaml_file(tmp_path):
    def write(data, filename="workflow.yaml"):
        p = tmp_path / filename
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p
    return write
