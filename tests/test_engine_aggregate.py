"""变量聚合节点引擎测试（PRD 4.7 + 取消单输入约束决策）。

输入变量由连线解析；必须同类型；输出自动命名 <agg节点名>-output。
聚合结果：string -> 按上游顺序拼接 list？——按 PRD「将多个同类型变量聚合为一个」，
聚合输出类型与输入一致：
- string: 依连线顺序拼接为单个 string？不可行（语义不明）。
合理实现（plan 已确定）：聚合为同类型元素的 list 不合法（类型须与输入一致），
故 string 聚合为换行拼接，int/float 聚合为求和，list 聚合为顺次连接，
dict 聚合为合并（键冲突后者覆盖）。以下测试锁定该语义。
"""
import pytest


def agg_workflow(factories, output_type, sources, agg_id="agg-1",
                 agg_name=None):
    """start(声明各源变量) -> 两个 code 产出 -> agg -> end。

    简化：Start 直接声明全部输入变量，agg 直接连线 Start 多次不现实，
    所以用两个 code 节点产出变量后聚合。
    """
    producers = []
    edges = []
    for i, (name, value_repr, type_) in enumerate(sources):
        nid = f"p{i}"
        producers.append(factories["code"](
            nid, inputs=[], outputs=[{"name": name, "type": type_}],
            code=f"def main():\n    return {{\"result\": {value_repr}}}\n"))
        edges.append(factories["edge"](nid, agg_id))
    agg = factories["aggregate"](agg_id, output_type=output_type,
                                 name=agg_name)
    nodes = producers + [agg, factories["end"]()]
    edges.append(factories["edge"](agg_id, "end-1"))
    return factories["workflow"](nodes, edges)


class TestAggregate:
    async def test_explicit_inputs_only_aggregate_selected_variables(
            self, engine_factory, factories):
        wf = agg_workflow(factories, "int", [
            ("a", "1", "int"), ("b", "20", "int")])
        agg = next(node for node in wf["nodes"] if node["type"] == "aggregate")
        agg["config"]["inputs"] = [{"source": "a", "type": "int"}]
        result = await engine_factory(wf).run({})
        assert result["status"] == "success"
        assert result["variables"]["agg-1-output"]["value"] == 1
    async def test_string_concat(self, engine_factory, factories):
        wf = agg_workflow(factories, "string", [
            ("s1", "\"甲\"", "string"), ("s2", "\"乙\"", "string")])
        result = await engine_factory(wf).run({})
        assert result["status"] == "success"
        out = result["variables"]["agg-1-output"]
        assert out["type"] == "string"
        assert "甲" in out["value"] and "乙" in out["value"]

    async def test_int_sum(self, engine_factory, factories):
        wf = agg_workflow(factories, "int", [
            ("a", "1", "int"), ("b", "2", "int"), ("c", "3", "int")])
        result = await engine_factory(wf).run({})
        assert result["variables"]["agg-1-output"]["value"] == 6

    async def test_float_sum(self, engine_factory, factories):
        wf = agg_workflow(factories, "float", [
            ("a", "1.5", "float"), ("b", "2.5", "float")])
        result = await engine_factory(wf).run({})
        assert result["variables"]["agg-1-output"]["value"] == pytest.approx(4.0)

    async def test_list_concat(self, engine_factory, factories):
        wf = agg_workflow(factories, "list", [
            ("l1", "[1, 2]", "list"), ("l2", "[3]", "list")])
        result = await engine_factory(wf).run({})
        assert result["variables"]["agg-1-output"]["value"] == [1, 2, 3]

    async def test_dict_merge(self, engine_factory, factories):
        wf = agg_workflow(factories, "dict", [
            ("d1", "{\"a\": 1}", "dict"), ("d2", "{\"b\": 2}", "dict")])
        result = await engine_factory(wf).run({})
        assert result["variables"]["agg-1-output"]["value"] == {"a": 1, "b": 2}

    async def test_output_autonamed_readonly(self, engine_factory, factories):
        wf = agg_workflow(factories, "int", [("a", "1", "int")],
                          agg_id="my_agg")
        result = await engine_factory(wf).run({})
        assert result["variables"]["my_agg-output"]["owner"] == "my_agg"

    async def test_output_uses_node_name(self, engine_factory, factories):
        wf = agg_workflow(
            factories, "int", [("a", "1", "int")],
            agg_id="agg-node", agg_name="Aggregate 1")
        result = await engine_factory(wf).run({})
        assert result["variables"]["Aggregate-1-output"]["value"] == 1
        assert "agg-node-output" not in result["variables"]

    async def test_waits_for_all_upstreams(self, engine_factory, factories):
        """多输入：所有上游完成后才执行聚合。"""
        wf = agg_workflow(factories, "int", [
            ("a", "1", "int"), ("b", "2", "int")])
        events = []
        engine = engine_factory(wf)
        result = await engine.run({}, on_event=events.append)
        assert result["status"] == "success"
        finished = [e["node_id"] for e in events
                    if e["event"] == "node_finished"]
        agg_pos = finished.index("agg-1")
        assert finished.index("p0") < agg_pos
        assert finished.index("p1") < agg_pos

    async def test_skipped_upstream_not_aggregated(self, engine_factory,
                                                   factories):
        """IF 分支未走的上游处于 skipped，不参与聚合（聚合仍可成功）。"""
        then_code = factories["code"](
            "then", inputs=[], outputs=[{"name": "v", "type": "int"}],
            code="def main():\n    return {\"result\": 5}\n")
        else_code = factories["code"](
            "els", inputs=[], outputs=[{"name": "w", "type": "int"}],
            code="def main():\n    return {\"result\": 9}\n")
        cond = factories["if_"]("cond", variable="x", operator="是", value="1")
        agg = factories["aggregate"]("agg", output_type="int")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "x", "type": "string"}]),
             cond, then_code, else_code, agg, factories["end"]()],
            [factories["edge"]("s", "cond"),
             factories["edge"]("cond", "then", "if"),
             factories["edge"]("cond", "els", "else"),
             factories["edge"]("then", "agg"),
             factories["edge"]("els", "agg"),
             factories["edge"]("agg", "end-1")],
        )
        result = await engine_factory(wf).run({"x": "1"})
        assert result["status"] == "success"
        # 只聚合了 then 分支的 5
        assert result["variables"]["agg-output"]["value"] == 5
