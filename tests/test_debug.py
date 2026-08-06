"""单节点调试测试（PRD 4.2 单节点调试 + 7.3 运行规则）。

契约：app.services.debug_service.DebugService(code_service)
- debug_node(wf_dict, node_id, inputs=None) -> dict
- 首次调试需要显式入参；后续同节点可复用缓存入参
- 未传入参且无缓存 -> 报 MissingDebugInputsError
- 不执行上游节点；不写正式运行上下文；新输入覆盖旧缓存
- clear_cache() 对应页面刷新清缓存
"""
import pytest

from app.services.debug_service import (  # TDD：尚不存在
    DebugService, MissingDebugInputsError,
)


@pytest.fixture
def svc(inline_code):
    return DebugService(code_service=inline_code)


class TestDebugNode:
    def test_first_run_requires_inputs(self, svc, simple_workflow):
        with pytest.raises(MissingDebugInputsError):
            svc.debug_node(simple_workflow, "code-1")

    def test_debug_with_manual_inputs(self, svc, simple_workflow):
        r = svc.debug_node(simple_workflow, "code-1",
                           inputs={"user_input": "手动"})
        assert r["ok"] is True
        assert r["result"] == {"result": "手动"}

    def test_debug_params_style_keeps_raw_hyphen_name(self, svc, factories):
        node = factories["code"](
            "code-1",
            inputs=[{
                "name": "arg-1",
                "type": "string",
                "required": True,
            }],
            outputs=[{"name": "result", "type": "string"}],
            code="def main(params):\n    return {\"result\": params[\"arg-1\"].upper()}\n")
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[{"name": "arg-1", "type": "string"}]),
             node, factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "end-1")])
        r = svc.debug_node(wf, "code-1", inputs={"arg-1": "debug"})
        assert r["ok"] is True
        assert r["result"] == {"result": "DEBUG"}

    def test_cached_inputs_reused(self, svc, simple_workflow):
        svc.debug_node(simple_workflow, "code-1", inputs={"user_input": "缓存"})
        r = svc.debug_node(simple_workflow, "code-1")
        assert r["result"] == {"result": "缓存"}
        assert r["cache_hit"] is True

    def test_new_inputs_replace_cache(self, svc, simple_workflow):
        r = svc.debug_node(simple_workflow, "code-1",
                           inputs={"user_input": "新"})
        assert r["result"] == {"result": "新"}
        r2 = svc.debug_node(simple_workflow, "code-1")
        assert r2["result"] == {"result": "新"}
        assert r2["cache_hit"] is True

    def test_upstream_not_executed(self, svc, factories, inline_code):
        """只执行当前节点，不执行上游。"""
        upstream = factories["code"]("up", inputs=[], outputs=[
            {"name": "uv", "type": "string"}],
            code="def main():\n    return {\"result\": \"up\"}\n")
        down = factories["code"](
            "down",
            inputs=[{"name": "v", "type": "string", "source": "uv"}],
            outputs=[{"name": "dv", "type": "string"}],
            code="def main(v):\n    return {\"result\": v + \"!\"}\n")
        wf = factories["workflow"](
            [factories["start"](), upstream, down, factories["end"]()],
            [factories["edge"]("start-1", "up"),
             factories["edge"]("up", "down"),
             factories["edge"]("down", "end-1")])
        svc.debug_node(wf, "down", inputs={"v": "值"})
        # 只调用了一次代码服务（down），upstream 未执行
        assert len(inline_code.calls) == 1
        assert "def main(v):" in inline_code.calls[0][0]

    def test_debug_failure_reported(self, svc, simple_workflow):
        simple_workflow["nodes"][1]["config"]["code"] = (
            "def main(user_input):\n    raise RuntimeError(\"调试炸\")\n")
        r = svc.debug_node(simple_workflow, "code-1",
                           inputs={"user_input": "x"})
        assert r["ok"] is False
        assert r["error_type"] == "RuntimeError"

    def test_clear_cache(self, svc, simple_workflow):
        svc.debug_node(simple_workflow, "code-1", inputs={"user_input": "x"})
        svc.clear_cache()
        with pytest.raises(MissingDebugInputsError):
            svc.debug_node(simple_workflow, "code-1")

    def test_debug_not_in_run_context(self, svc, simple_workflow):
        """调试结果不写入正式全流程上下文（返回独立结果对象）。"""
        r = svc.debug_node(simple_workflow, "code-1",
                           inputs={"user_input": "隔离"})
        assert "variables" not in r or r.get("isolated") is True

    def test_unknown_node_raises(self, svc, simple_workflow):
        with pytest.raises(KeyError):
            svc.debug_node(simple_workflow, "ghost",
                           inputs={"user_input": "x"})

    def test_debug_llm_node(self, factories, inline_code, fake_llm):
        """Prompt 节点调试：只渲染模板，不调用 LLM 服务。"""
        svc = DebugService(code_service=inline_code, llm_service=fake_llm)
        wf = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", "问: {{ q }}"),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        import asyncio
        # Python 3.12+ 同步上下文无隐式事件循环，须用 asyncio.run
        r = asyncio.run(svc.debug_node_async(wf, "l1", inputs={"q": "好"}))
        assert r["prompt"] == "问: 好"
        assert "content" not in r
        assert fake_llm.calls == []
