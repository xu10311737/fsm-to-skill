"""端到端运行测试（PRD 第 7 章运行规则 + 变量全局规则）。

覆盖：严格串行、Start 输入注入、多 End、变量顺序写入不可重复、
运行统计（耗时/LLM 调用次数）、运行记录结构。
"""
import json


class TestSerialExecution:
    async def test_full_chain_serial(self, engine_factory, factories,
                                     fake_llm):
        """start -> code -> prompt 后暂停，等待外部 Agent 输入。"""
        code = factories["code"](
            "c1", outputs=[{"name": "upper", "type": "string"}],
            code="def main(user_input):\n"
                 "    return {\"result\": user_input.upper()}\n")
        llm = factories["llm"]("llm-1", prompt="翻译: {{ upper }}")
        wf = factories["workflow"](
            [factories["start"](), code, llm, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "llm-1"),
             factories["edge"]("llm-1", "end-1")],
        )
        events = []
        result = await engine_factory(wf).run(
            {"user_input": "abc", "task-id": "task-chain"},
            on_event=events.append)
        assert result["status"] == "waiting"
        assert result["waiting_node"] == "llm-1"
        assert fake_llm.calls == []
        assert result["variables"]["task-id"]["value"] == "task-chain"
        assert "llm-1-output" not in result["variables"]
        assert result["node_records"]["llm-1"]["prompt"] == (
            "翻译: ABC\n\n---\ntask-id: task-chain")
        started = [e["node_id"] for e in events
                   if e["event"] == "node_started"]
        assert started == ["start-1", "c1", "llm-1"]
        assert "end-1" not in result["node_records"]

    async def test_start_inputs_become_variables(self, engine_factory,
                                                 simple_workflow):
        """用户决策：Start 声明的输入变量可被下游引用。"""
        result = await engine_factory(simple_workflow).run(
            {"user_input": "注入值"})
        assert result["variables"]["user_input"]["value"] == "注入值"
        assert result["variables"]["user_input"]["owner"] == "start-1"

    async def test_prompt_pauses_before_downstream_code(
            self, engine_factory, factories, fake_llm):
        """Prompt 是 Agent 出口，后续 Code 只能由外部 step-param 调用。"""
        prompt = factories["llm"]("prompt-1", prompt="请继续: {{ user_input }}")
        downstream = factories["code"](
            "code-after-prompt",
            inputs=[{"name": "arg-1", "type": "string", "required": True}],
            code='def main(params):\n    return {"result": params["arg-1"]}\n',
        )
        wf = factories["workflow"](
            [factories["start"](), prompt, downstream, factories["end"]()],
            [factories["edge"]("start-1", "prompt-1"),
             factories["edge"]("prompt-1", "code-after-prompt"),
             factories["edge"]("code-after-prompt", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "waiting"
        assert result["waiting_node"] == "prompt-1"
        assert "code-after-prompt" not in result["node_records"]
        assert fake_llm.calls == []

    async def test_missing_required_input_fails(self, engine_factory,
                                                simple_workflow):
        result = await engine_factory(simple_workflow).run({})
        assert result["status"] == "failed"
        assert result["failed_node"] == "start-1"

    async def test_input_type_checked(self, engine_factory, factories):
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "n", "type": "int"}]),
             factories["end"]()],
            [factories["edge"]("s", "end-1")],
        )
        result = await engine_factory(wf).run({"n": "not-int"})
        assert result["status"] == "failed"


class TestEndSemantics:
    async def test_multiple_ends_first_reached_wins(self, engine_factory,
                                                    factories):
        """到达任一 End 即成功；记录到达的 End。"""
        a = factories["code"]("a", inputs=[], outputs=[
            {"name": "x", "type": "string"}],
            code="def main():\n    return {\"result\": \"a\"}\n")
        b = factories["code"]("b", inputs=[], outputs=[
            {"name": "y", "type": "string"}],
            code="def main():\n    return {\"result\": \"b\"}\n")
        wf = factories["workflow"](
            [factories["start"](), a, b,
             factories["end"]("e1"), factories["end"]("e2")],
            [factories["edge"]("start-1", "a"),
             factories["edge"]("start-1", "b"),
             factories["edge"]("a", "e1"),
             factories["edge"]("b", "e2")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "success"
        assert result["end_node"] in ("e1", "e2")

    async def test_end_has_no_outgoing(self, engine_factory, factories):
        """End 无出边：拓扑上 End 后不再有节点执行。"""
        wf = factories["workflow"](
            [factories["start"](), factories["end"]()],
            [factories["edge"]("start-1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "success"
        assert result["node_records"]["end-1"]["status"] == "success"


class TestVariableWriteRules:
    async def test_duplicate_output_name_rejected(self, engine_factory,
                                                  factories):
        """同名变量不覆盖：两个节点输出同名变量 -> 后者失败。"""
        c1 = factories["code"]("c1", inputs=[], outputs=[
            {"name": "v", "type": "string"}],
            code="def main():\n    return {\"result\": \"1\"}\n")
        c2 = factories["code"]("c2", inputs=[], outputs=[
            {"name": "v", "type": "string"}],
            code="def main():\n    return {\"result\": \"2\"}\n")
        wf = factories["workflow"](
            [factories["start"](), c1, c2, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "c2"),
             factories["edge"]("c2", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "failed"
        assert result["failed_node"] == "c2"
        assert result["variables"]["v"]["value"] == "1"  # 未被覆盖

    async def test_if_join_single_branch_variable_not_visible(
            self, engine_factory, factories):
        """IF 汇合后的节点不能依赖只在某一分支产生的变量。"""
        cond = factories["if_"]("cond")
        a = factories["code"]("a", inputs=[], outputs=[
            {"name": "a_val", "type": "string"}],
            code="def main():\n    return {\"a_val\": \"A\"}\n")
        b = factories["code"]("b", inputs=[], outputs=[
            {"name": "b_val", "type": "string"}],
            code="def main():\n    return {\"b_val\": \"B\"}\n")
        join = factories["llm"]("join", prompt="{{ a_val }}")
        wf = factories["workflow"](
            [factories["start"](), cond, a, b, join, factories["end"]()],
            [factories["edge"]("start-1", "cond"),
             factories["edge"]("cond", "a", "if"),
             factories["edge"]("cond", "b", "else"),
             factories["edge"]("a", "join"),
             factories["edge"]("b", "join"),
             factories["edge"]("join", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "go"})
        assert result["status"] == "failed"
        assert result["failed_node"] == "join"
        assert result["node_records"]["join"]["error_type"] == "TemplateError"


class TestRunStats:
    async def test_duration_recorded(self, engine_factory, simple_workflow):
        result = await engine_factory(simple_workflow).run(
            {"user_input": "x"})
        for nid in ("start-1", "code-1", "end-1"):
            assert result["node_records"][nid]["duration_ms"] >= 0
        assert result["total_duration_ms"] >= 0

    async def test_prompt_nodes_do_not_increment_llm_call_count(
            self, engine_factory, factories, fake_llm):
        llm1 = factories["llm"]("llm-1", prompt="一 {{ user_input }}")
        llm2 = factories["llm"]("llm-2", prompt="二")
        wf = factories["workflow"](
            [factories["start"](), llm1, llm2, factories["end"]()],
            [factories["edge"]("start-1", "llm-1"),
             factories["edge"]("llm-1", "llm-2"),
             factories["edge"]("llm-2", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["llm_call_count"] == 0
        assert fake_llm.calls == []

    async def test_run_record_serializable(self, engine_factory,
                                           simple_workflow):
        """运行记录可 JSON 序列化（用于本地保存与回放）。"""
        result = await engine_factory(simple_workflow).run(
            {"user_input": "x"})
        json.dumps(result, ensure_ascii=False)

    async def test_stdout_captured(self, engine_factory, factories,
                                   inline_code):
        """脚本 stdout 进入节点记录（PRD 7.2）。

        内联执行器不捕获 print，这里断言契约字段存在；
        真实 stdout 捕获在 test_code_runner 子进程测试中验证。
        """
        result = await engine_factory(
            factories["workflow"](
                [factories["start"](), factories["code"](), factories["end"]()],
                [factories["edge"]("start-1", "code-1"),
                 factories["edge"]("code-1", "end-1")],
            )).run({"user_input": "x"})
        assert "stdout" in result["node_records"]["code-1"]

    async def test_fresh_context_per_run(self, engine_factory, simple_workflow):
        """每次全流程运行从 Start 重新建立变量上下文。"""
        engine = engine_factory(simple_workflow)
        r1 = await engine.run({"user_input": "第一次"})
        r2 = await engine.run({"user_input": "第二次"})
        assert r1["variables"]["result"]["value"] == "第一次"
        assert r2["variables"]["result"]["value"] == "第二次"
