"""Prompt 节点引擎测试。

Prompt 节点只负责渲染并输出 prompt 字符串；它不是模型调用节点。
"""


class TestPromptNode:
    async def test_prompt_rendered_but_not_sent_to_model(
            self, engine_factory, linear_llm_workflow, fake_llm):
        result = await engine_factory(linear_llm_workflow).run(
            {"user_input": "世界", "task-id": "task-test"})
        rec = result["node_records"]["llm-1"]
        assert result["status"] == "waiting"
        assert result["waiting_node"] == "llm-1"
        assert rec["prompt"] == "你好 世界\n\n---\ntask-id: task-test"
        assert rec["prompt_output"] == rec["prompt"]
        assert fake_llm.calls == []

    async def test_prompt_does_not_create_output_or_model_fields(
            self, engine_factory, linear_llm_workflow, fake_llm):
        fake_llm.reply = "模型回复内容"
        result = await engine_factory(linear_llm_workflow).run(
            {"user_input": "x"})
        rec = result["node_records"]["llm-1"]
        assert "llm-1-output" not in result["variables"]
        assert "content" not in rec
        assert "thinking" not in rec
        assert "usage" not in rec
        assert fake_llm.calls == []

    async def test_prompt_recorded_in_node_record(self, engine_factory,
                                                  linear_llm_workflow):
        result = await engine_factory(linear_llm_workflow).run(
            {"user_input": "abc", "task-id": "task-test"})
        rec = result["node_records"]["llm-1"]
        assert rec["prompt"] == "你好 abc\n\n---\ntask-id: task-test"
        assert rec["status"] == "success"

    async def test_user_message_is_not_system_variable(self, engine_factory,
                                                       factories, fake_llm):
        node = factories["llm"]("llm-1", prompt="任务: {{ user-message }}")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[]), node, factories["end"]()],
            [factories["edge"]("s", "llm-1"),
             factories["edge"]("llm-1", "end-1")],
        )
        result = await engine_factory(wf).run({
            "user-message": "整理数据",
            "task-id": "task-msg",
        })
        assert result["status"] == "failed"
        assert result["failed_node"] == "llm-1"
        assert fake_llm.calls == []

    async def test_model_failure_does_not_affect_prompt_node(
            self, engine_factory, linear_llm_workflow, fake_llm):
        fake_llm.failures = [ConnectionError("网络错误")]
        result = await engine_factory(linear_llm_workflow).run(
            {"user_input": "x"})
        assert result["status"] == "waiting"
        assert result["node_records"]["llm-1"]["status"] == "success"
        assert fake_llm.calls == []

    async def test_template_render_error_fails(self, engine_factory,
                                               factories):
        node = factories["llm"]("llm-1", prompt="{{ ghost_var }}")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[]), node, factories["end"]()],
            [factories["edge"]("s", "llm-1"),
             factories["edge"]("llm-1", "end-1")],
        )
        result = await engine_factory(wf).run({})
        assert result["status"] == "failed"
        assert result["failed_node"] == "llm-1"

    async def test_stream_does_not_emit_model_tokens(
            self, engine_factory, factories, fake_llm):
        fake_llm.reply = "abc"
        wf = factories["workflow"](
            [factories["start"](), factories["llm"](), factories["end"]()],
            [factories["edge"]("start-1", "llm-1"),
             factories["edge"]("llm-1", "end-1")],
        )
        events = []
        engine = engine_factory(wf)
        await engine.run({"user_input": "x"}, on_event=events.append,
                         stream=True)
        tokens = [e["token"] for e in events if e["event"] == "llm_token"]
        assert tokens == []
        assert fake_llm.calls == []

    async def test_list_dict_vars_rendered_json(self, engine_factory,
                                                factories, fake_llm):
        node = factories["llm"]("llm-1", prompt="数据: {{ payload }}")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "payload", "type": "dict"}]),
             node, factories["end"]()],
            [factories["edge"]("s", "llm-1"),
             factories["edge"]("llm-1", "end-1")],
        )
        result = await engine_factory(wf).run({
            "payload": {"语言": "中文"},
            "task-id": "task-json",
        })
        assert result["status"] == "waiting"
        assert result["waiting_node"] == "llm-1"
        assert result["node_records"]["llm-1"]["prompt"] == (
            '数据: {"语言": "中文"}\n\n---\ntask-id: task-json')
        assert fake_llm.calls == []
