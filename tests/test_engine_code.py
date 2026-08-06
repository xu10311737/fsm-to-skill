"""Code 节点引擎测试（PRD 4.2 + 异常分支）。

契约：Engine.run(inputs, on_event=None) -> RunResult(dict)
RunResult: {
  "status": "success" | "failed",
  "variables": dict,           # 最终变量快照
  "node_records": {node_id: {"status": ..., "duration_ms": int,
                             "stdout": str, "error_type": ..., "error_message": ...}},
  "failed_node": str | None,
}
"""
import pytest


class TestCodeExecution:
    async def test_basic_code_node(self, engine_factory, simple_workflow):
        engine = engine_factory(simple_workflow)
        result = await engine.run({"user_input": "hello"})
        assert result["status"] == "success"
        assert result["variables"]["user_input"]["value"] == "hello"
        # code 输出 result 与 main 返回值一致
        assert result["variables"]["result"]["value"] == "hello"
        assert result["node_records"]["code-1"]["status"] == "success"

    async def test_main_args_match_inputs(self, engine_factory, factories,
                                          inline_code):
        """main 参数与输入变量一一对应，按名称传参。"""
        code = (
            "def main(a, b):\n"
            "    return {\"result\": a + b}\n"
        )
        node = factories["code"]("sum", inputs=[
            {"name": "a", "type": "int", "source": "x"},
            {"name": "b", "type": "int", "source": "y"},
        ], outputs=[{"name": "total", "type": "int"}], code=code)
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "x", "type": "int"},
                                             {"name": "y", "type": "int"}]),
             node, factories["end"]()],
            [factories["edge"]("s", "sum"), factories["edge"]("sum", "end-1")],
        )
        result = await engine_factory(wf).run({"x": 2, "y": 3})
        assert result["status"] == "success"
        assert result["variables"]["total"]["value"] == 5
        # 传给 main 的实参正是声明的输入
        _, args = inline_code.calls[-1]
        assert args == {"a": 2, "b": 3}

    async def test_params_dict_style_receives_raw_hyphen_names(
            self, engine_factory, factories, inline_code):
        code = (
            "def main(params):\n"
            "    return {\"result\": params[\"arg-1\"].upper()}\n"
        )
        node = factories["code"](
            "sum", inputs=[{"name": "arg-1", "type": "string", "source": "x"}],
            outputs=[{"name": "upper", "type": "string"}], code=code)
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "x", "type": "string"}]),
             node, factories["end"]()],
            [factories["edge"]("s", "sum"), factories["edge"]("sum", "end-1")],
        )
        result = await engine_factory(wf).run({"x": "hello"})
        assert result["status"] == "success"
        assert result["variables"]["upper"]["value"] == "HELLO"
        _, args = inline_code.calls[-1]
        assert args == {"arg-1": "hello"}

    async def test_arg_schema_without_source_reads_same_named_input(
            self, engine_factory, factories, inline_code):
        """新 Code 参数 schema 不配置 source，按参数名取运行上下文值。"""
        code = (
            "def main(arg1):\n"
            "    return {\"result\": arg1.upper()}\n"
        )
        node = factories["code"](
            "c1",
            inputs=[{
                "name": "arg1",
                "description": "输入参数",
                "type": "string",
                "required": True,
            }],
            outputs=[{"name": "upper", "type": "string"}],
            code=code)
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "arg1", "type": "string"}]),
             node, factories["end"]()],
            [factories["edge"]("s", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"arg1": "hello"})
        assert result["status"] == "success"
        assert result["variables"]["upper"]["value"] == "HELLO"
        _, args = inline_code.calls[-1]
        assert args == {"arg1": "hello"}

    async def test_output_written_under_declared_name(self, engine_factory,
                                                      factories):
        """返回 {'result': v} 写入声明的输出变量名。"""
        node = factories["code"](
            "c1",
            code="def main(user_input):\n    return {\"result\": user_input + \"!\"}\n",
        )
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["variables"]["result"]["value"] == "hi!"

    async def test_missing_main_fails(self, engine_factory, factories):
        node = factories["code"]("c1", code="x = 1\n")
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "failed"
        assert result["failed_node"] == "c1"

    async def test_non_dict_return_fails(self, engine_factory, factories):
        node = factories["code"](
            "c1", code="def main(user_input):\n    return 42\n")
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "failed"
        rec = result["node_records"]["c1"]
        assert rec["status"] == "failed"

    async def test_return_dict_keys_auto_define_outputs(self, engine_factory,
                                                        factories):
        node = factories["code"](
            "c1",
            code=(
                "def main(user_input):\n"
                "    return {\"other\": 1, \"other2\": user_input}\n"
            ))
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "success"
        assert result["variables"]["other"]["value"] == 1
        assert result["variables"]["other2"]["value"] == "hi"

    async def test_output_type_mismatch_fails(self, engine_factory, factories):
        """result 值不符合声明输出类型 -> 节点失败。"""
        node = factories["code"](
            "c1", outputs=[{"name": "n", "type": "int"}],
            code="def main(user_input):\n    return {\"result\": \"str\"}\n",
        )
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "failed"
        assert "类型" in result["node_records"]["c1"]["error_message"] or \
               result["node_records"]["c1"]["error_type"] is not None

    async def test_runtime_exception_fails_workflow(self, engine_factory,
                                                    factories):
        node = factories["code"](
            "c1", code="def main(user_input):\n    raise RuntimeError(\"炸了\")\n")
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "failed"
        rec = result["node_records"]["c1"]
        assert rec["error_type"] == "RuntimeError"
        assert "炸了" in rec["error_message"]
        # 失败后下游不执行
        assert "end-1" not in result["node_records"] or \
            result["node_records"]["end-1"]["status"] in ("skipped", "pending")


class TestErrorBranch:
    async def test_error_branch_captures_and_continues(self, engine_factory,
                                                       factories):
        """开启异常分支：失败后走 error 边，写入只读错误变量，流程继续。"""
        bad = factories["code"](
            "c1", error_branch=True,
            code="def main(user_input):\n    raise ValueError(\"bad\")\n",
        )
        handler = factories["code"](
            "h1",
            inputs=[{"name": "err", "type": "string",
                     "source": "c1-error-message"}],
            outputs=[{"name": "handled", "type": "string"}],
            code="def main(err):\n    return {\"result\": \"handled:\" + err}\n",
        )
        wf = factories["workflow"](
            [factories["start"](), bad, handler, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "h1", "error"),
             factories["edge"]("h1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "success"
        assert result["variables"]["c1-error-type"]["value"] == "ValueError"
        assert "bad" in result["variables"]["c1-error-message"]["value"]
        assert result["variables"]["handled"]["value"].startswith("handled:")

    async def test_error_variables_readonly_autonamed(self, engine_factory,
                                                      factories):
        bad = factories["code"](
            "my_node", name="my_node", error_branch=True,
            code="def main(user_input):\n    raise KeyError(\"k\")\n",
        )
        wf = factories["workflow"](
            [factories["start"](), bad, factories["end"]()],
            [factories["edge"]("start-1", "my_node"),
             factories["edge"]("my_node", "end-1", "error")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "success"
        assert result["variables"]["my_node-error-type"]["value"] == "KeyError"

    async def test_success_goes_normal_edge_not_error(self, engine_factory,
                                                      factories):
        """成功时走 out 边，不走 error 边。"""
        ok = factories["code"]("c1", error_branch=True)
        on_ok = factories["code"]("ok_path", inputs=[], outputs=[
            {"name": "p", "type": "string"}],
            code="def main():\n    return {\"result\": \"ok\"}\n")
        on_err = factories["code"]("err_path", inputs=[], outputs=[
            {"name": "p2", "type": "string"}],
            code="def main():\n    return {\"result\": \"err\"}\n")
        wf = factories["workflow"](
            [factories["start"](), ok, on_ok, on_err, factories["end"]("e1"),
             factories["end"]("e2")],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "ok_path", "out"),
             factories["edge"]("c1", "err_path", "error"),
             factories["edge"]("ok_path", "e1"),
             factories["edge"]("err_path", "e2")],
        )
        result = await engine_factory(wf).run({"user_input": "hi"})
        assert result["status"] == "success"
        assert result["variables"]["p"]["value"] == "ok"
        assert "p2" not in result["variables"]
        assert result["node_records"]["err_path"]["status"] == "skipped"

    async def test_retry_edge_reexecutes_code(self, engine_factory, factories,
                                              inline_code):
        """异常处理后回连重试：第一次失败 -> handler -> retry 回 c1 -> 成功。"""
        flaky_code = (
            "def main(user_input):\n"
            "    import os\n"
            "    if os.environ.get('FLAKY_DONE') != '1':\n"
            "        raise RuntimeError('first')\n"
            "    return {\"result\": \"done\"}\n"
        )
        c1 = factories["code"]("c1", error_branch=True, code=flaky_code)
        handler = factories["code"](
            "h1", inputs=[], outputs=[{"name": "fixed", "type": "string"}],
            code=("def main():\n    import os\n"
                  "    os.environ['FLAKY_DONE'] = '1'\n"
                  "    return {\"result\": \"fixed\"}\n"),
        )
        wf = factories["workflow"](
            [factories["start"](), c1, handler, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "h1", "error"),
             factories["edge"]("h1", "c1", "retry"),
             factories["edge"]("c1", "end-1")],
        )
        import os
        os.environ.pop("FLAKY_DONE", None)
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "success"
        # c1 被执行两次（h1 的代码也含 FLAKY 字样，须用 c1 独有的
        # environ.get('FLAKY_DONE') 读取调用区分）
        c1_calls = [c for c in inline_code.calls
                    if "environ.get('FLAKY_DONE')" in c[0]]
        assert len(c1_calls) == 2

    async def test_error_handler_failure_terminates(self, engine_factory,
                                                    factories):
        """错误捕获路径上的节点再次异常 -> 终止工作流。"""
        bad = factories["code"](
            "c1", error_branch=True,
            code="def main(user_input):\n    raise ValueError(\"x\")\n")
        worse = factories["code"](
            "h1", inputs=[], outputs=[{"name": "z", "type": "string"}],
            code="def main():\n    raise RuntimeError(\"handler也炸了\")\n")
        wf = factories["workflow"](
            [factories["start"](), bad, worse, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "h1", "error"),
             factories["edge"]("h1", "end-1")],
        )
        result = await engine_factory(wf).run({"user_input": "x"})
        assert result["status"] == "failed"
        assert result["failed_node"] == "h1"


class TestEvents:
    async def test_node_events_emitted_in_order(self, engine_factory,
                                                simple_workflow):
        events = []
        engine = engine_factory(simple_workflow)
        await engine.run({"user_input": "hi"}, on_event=events.append)
        kinds = [e["event"] for e in events]
        assert kinds[0] == "node_started"
        assert "node_finished" in kinds
        assert kinds[-1] == "workflow_finished"
        started = [e["node_id"] for e in events if e["event"] == "node_started"]
        assert started == ["start-1", "code-1", "end-1"]
