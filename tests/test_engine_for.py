"""For 循环节点引擎测试（PRD 4.5）。

循环体契约：
- for node config: {"list_source": <全局list变量名>,
                    "collect": <循环体内局部变量名>,
                    "body": {"nodes": [...], "edges": [...]}}
- 局部只读变量 index(int, 从0) / item(当前元素)
- 每轮串行执行 body 子图，收集 collect 变量值
- 聚合结果写入 <for节点名>-output（list，只读）
"""


def for_workflow(factories, body_code, list_source="items",
                 collect="result", for_id="for-1", for_name="for-1"):
    body_code_node = factories["node"](
        "b1", "code", "b1",
        {"inputs": [
            {"name": "item", "type": "int", "source": "item"},
            {"name": "index", "type": "int", "source": "index"},
        ],
         "outputs": [{"name": "result", "type": "int"}],
         "code": body_code, "error_branch": False},
    )
    loop = factories["for_"](for_id, list_source=list_source,
                             body_nodes=[body_code_node], body_edges=[],
                             name=for_name)
    loop["config"]["collect"] = collect
    wf = factories["workflow"](
        [factories["start"]("s", inputs=[{"name": "items", "type": "list"}]),
         loop, factories["end"]()],
        [factories["edge"]("s", for_id), factories["edge"](for_id, "end-1")],
    )
    return wf


class TestForLoop:
    async def test_basic_aggregation(self, engine_factory, factories):
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": item * 2}\n",
        )
        result = await engine_factory(wf).run({"items": [1, 2, 3]})
        assert result["status"] == "success"
        assert result["variables"]["for-1-output"]["value"] == [2, 4, 6]
        assert result["variables"]["for-1-output"]["type"] == "list"

    async def test_output_uses_node_name(self, engine_factory, factories):
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": item}\n",
            for_id="loop-node",
            for_name="For 1",
        )
        result = await engine_factory(wf).run({"items": [1]})
        assert result["variables"]["For-1-output"]["value"] == [1]
        assert "loop-node-output" not in result["variables"]

    async def test_order_preserved_serial(self, engine_factory, factories):
        """严格串行、按输入顺序聚合。"""
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": item + index}\n",
        )
        result = await engine_factory(wf).run({"items": [10, 20, 30]})
        assert result["variables"]["for-1-output"]["value"] == [10, 21, 32]

    async def test_empty_list_skips_body(self, engine_factory, factories,
                                         inline_code):
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": item}\n",
        )
        result = await engine_factory(wf).run({"items": []})
        assert result["status"] == "success"
        assert result["variables"]["for-1-output"]["value"] == []
        assert inline_code.calls == []  # 循环体未执行
        # 后续节点仍执行
        assert result["node_records"]["end-1"]["status"] == "success"

    async def test_iteration_failure_aborts(self, engine_factory, factories):
        """任一轮未处理异常 -> 循环立即失败，不继续后续轮次。"""
        wf = for_workflow(
            factories,
            "def main(item, index):\n"
            "    if item == 2:\n"
            "        raise ValueError(\"boom\")\n"
            "    return {\"result\": item}\n",
        )
        result = await engine_factory(wf).run({"items": [1, 2, 3]})
        assert result["status"] == "failed"
        assert result["failed_node"] == "for-1"
        assert "for-1-output" not in result["variables"]

    async def test_body_code_error_branch_reaches_body_prompt(
            self, engine_factory, factories):
        failed = factories["node"](
            "body-code", "code", "Body-Code",
            {"inputs": [{"name": "item", "type": "int", "source": "item"}],
             "outputs": [{"name": "result", "type": "int"}],
             "code": ("def main(item):\n"
                      "    raise RuntimeError('body failure')\n"),
             "error_branch": True},
        )
        error_prompt = factories["llm"](
            "body-error-prompt", "{{ Body-Code-error-message }}")
        loop = factories["for_"](
            "for-1", list_source="items", body_nodes=[failed, error_prompt],
            body_edges=[factories["edge"](
                "body-code", "body-error-prompt", "error")],
        )
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[
                {"name": "items", "type": "list"},
            ]), loop, factories["end"]()],
            [factories["edge"]("start-1", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )

        result = await engine_factory(wf).run({"items": [1]})

        assert result["status"] == "waiting"
        assert result["waiting_node"] == "body-error-prompt"
        assert "body failure" in result["waiting_prompt"]

    async def test_index_item_readonly_locals(self, engine_factory, factories):
        """body 内可读取 index/item；body 输出不污染全局变量。"""
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": index}\n",
        )
        result = await engine_factory(wf).run({"items": ["a", "b"]})
        assert result["variables"]["for-1-output"]["value"] == [0, 1]
        # 局部 result / item / index 不写入全局
        assert "result" not in result["variables"]
        assert "item" not in result["variables"]
        assert "index" not in result["variables"]

    async def test_body_reads_global_variable(self, engine_factory, factories):
        """循环体可读取外部全局变量。"""
        body_code = (
            "def main(item, index, base):\n"
            "    return {\"result\": item + base}\n"
        )
        body_node = factories["node"](
            "b1", "code", "b1",
            {"inputs": [
                {"name": "item", "type": "int", "source": "item"},
                {"name": "index", "type": "int", "source": "index"},
                {"name": "base", "type": "int", "source": "base"},
            ],
             "outputs": [{"name": "result", "type": "int"}],
             "code": body_code, "error_branch": False},
        )
        loop = factories["for_"]("for-1", list_source="items",
                                 body_nodes=[body_node])
        loop["config"]["collect"] = "result"
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[
                {"name": "items", "type": "list"},
                {"name": "base", "type": "int"}]),
             loop, factories["end"]()],
            [factories["edge"]("s", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )
        result = await engine_factory(wf).run({"items": [1, 2], "base": 100})
        assert result["variables"]["for-1-output"]["value"] == [101, 102]

    async def test_body_cannot_shadow_global(self, engine_factory, factories):
        """循环体内不能创建与全局同名变量。"""
        body_node = factories["node"](
            "b1", "code", "b1",
            {"inputs": [{"name": "item", "type": "int", "source": "item"}],
             # 输出名与全局变量 items 冲突
             "outputs": [{"name": "items", "type": "int"}],
             "code": "def main(item):\n    return {\"result\": 1}\n",
             "error_branch": False},
        )
        loop = factories["for_"]("for-1", list_source="items",
                                 body_nodes=[body_node])
        loop["config"]["collect"] = "items"
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "items", "type": "list"}]),
             loop, factories["end"]()],
            [factories["edge"]("s", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )
        result = await engine_factory(wf).run({"items": [1]})
        assert result["status"] == "failed"

    async def test_output_readonly_cannot_rewrite(self, engine_factory,
                                                  factories):
        """下游节点不能再写入 <for>-output 同名变量。"""
        wf = for_workflow(
            factories,
            "def main(item, index):\n    return {\"result\": item}\n",
        )
        # 追加一个 code 节点试图输出同名变量
        thief = factories["code"](
            "thief", inputs=[], outputs=[{"name": "for-1-output", "type": "list"}],
            code="def main():\n    return {\"result\": []}\n")
        wf["nodes"].append(thief)
        wf["edges"] = [e for e in wf["edges"] if e["target"] != "end-1"]
        wf["edges"].append(factories["edge"]("for-1", "thief"))
        wf["edges"].append(factories["edge"]("thief", "end-1"))
        result = await engine_factory(wf).run({"items": [1]})
        assert result["status"] == "failed"
        assert result["failed_node"] == "thief"

    async def test_list_source_not_list_fails(self, engine_factory, factories):
        loop = factories["for_"]("for-1", list_source="s", body_nodes=[])
        loop["config"]["collect"] = "result"
        wf = factories["workflow"](
            [factories["start"]("st", inputs=[{"name": "s", "type": "string"}]),
             loop, factories["end"]()],
            [factories["edge"]("st", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )
        result = await engine_factory(wf).run({"s": "not-a-list"})
        assert result["status"] == "failed"
        assert result["failed_node"] == "for-1"

    async def test_body_multi_node_chain(self, engine_factory, factories):
        """循环体内多个节点串联：b1 -> b2，collect 取 b2 输出。"""
        b1 = factories["node"](
            "b1", "code", "b1",
            {"inputs": [{"name": "item", "type": "int", "source": "item"}],
             "outputs": [{"name": "mid", "type": "int"}],
             "code": "def main(item):\n    return {\"result\": item + 1}\n",
             "error_branch": False},
        )
        b2 = factories["node"](
            "b2", "code", "b2",
            {"inputs": [{"name": "mid", "type": "int", "source": "mid"}],
             "outputs": [{"name": "final", "type": "int"}],
             "code": "def main(mid):\n    return {\"result\": mid * 10}\n",
             "error_branch": False},
        )
        loop = factories["for_"]("for-1", list_source="items",
                                 body_nodes=[b1, b2],
                                 body_edges=[factories["edge"]("b1", "b2")])
        loop["config"]["collect"] = "final"
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "items", "type": "list"}]),
             loop, factories["end"]()],
            [factories["edge"]("s", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )
        result = await engine_factory(wf).run({"items": [1, 2]})
        assert result["variables"]["for-1-output"]["value"] == [20, 30]
