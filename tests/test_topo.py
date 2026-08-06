"""拓扑排序与环检测测试（PRD 5.2 + 已确认决策）。

契约：app.engine.topo
- topo_sort(workflow_dict) -> list[str]  按依赖顺序返回节点 id
- find_cycle(workflow_dict) -> list[str] | None  返回环上的节点 id
- 异常捕获的重试边（source_handle == "retry"）不参与环检测
- 多输入已放开：聚合/普通节点均可有多条入边
"""
import pytest

from app.engine.topo import find_cycle, topo_sort  # TDD：尚不存在


class TestTopoSort:
    def test_linear_order(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["code"](), factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "end-1")],
        )
        order = topo_sort(wf)
        assert order.index("start-1") < order.index("code-1")
        assert order.index("code-1") < order.index("end-1")
        assert set(order) == {"start-1", "code-1", "end-1"}

    def test_diamond_order(self, factories):
        """A -> B, A -> C, B -> D, C -> D：D 必须最后。"""
        nodes = [factories["start"]("a"), factories["code"]("b"),
                 factories["code"]("c"), factories["end"]("d")]
        edges = [factories["edge"]("a", "b"), factories["edge"]("a", "c"),
                 factories["edge"]("b", "d"), factories["edge"]("c", "d")]
        order = topo_sort(factories["workflow"](nodes, edges))
        assert order[0] == "a"
        assert order[-1] == "d"
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_multiple_in_edges_allowed(self, factories):
        """已取消单输入约束：一个节点可有多条入边，拓扑仍正确。"""
        nodes = [factories["code"]("x", inputs=[], outputs=[]),
                 factories["code"]("y", inputs=[], outputs=[]),
                 factories["aggregate"]("agg")]
        edges = [factories["edge"]("x", "agg"), factories["edge"]("y", "agg")]
        order = topo_sort(factories["workflow"](nodes, edges))
        assert order.index("agg") > order.index("x")
        assert order.index("agg") > order.index("y")

    def test_if_branches_topological(self, factories):
        nodes = [factories["start"]("s"), factories["if_"]("cond"),
                 factories["code"]("t", inputs=[], outputs=[]),
                 factories["code"]("f", inputs=[], outputs=[]),
                 factories["end"]("e")]
        edges = [factories["edge"]("s", "cond"),
                 factories["edge"]("cond", "t", "if"),
                 factories["edge"]("cond", "f", "else"),
                 factories["edge"]("t", "e"), factories["edge"]("f", "e")]
        order = topo_sort(factories["workflow"](nodes, edges))
        assert order.index("cond") < order.index("t")
        assert order.index("cond") < order.index("f")
        assert order[-1] == "e"


class TestCycleDetection:
    def test_no_cycle(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["code"](), factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "end-1")],
        )
        assert find_cycle(wf) is None

    def test_self_loop_detected(self, factories):
        wf = factories["workflow"](
            [factories["code"]("a", inputs=[], outputs=[])],
            [factories["edge"]("a", "a")],
        )
        cycle = find_cycle(wf)
        assert cycle is not None and "a" in cycle

    def test_simple_cycle_detected(self, factories):
        nodes = [factories["code"](n, inputs=[], outputs=[])
                 for n in ("a", "b", "c")]
        edges = [factories["edge"]("a", "b"), factories["edge"]("b", "c"),
                 factories["edge"]("c", "a")]
        cycle = find_cycle(factories["workflow"](nodes, edges))
        assert cycle is not None
        assert {"a", "b", "c"} <= set(cycle)

    def test_topo_sort_raises_on_cycle(self, factories):
        nodes = [factories["code"](n, inputs=[], outputs=[])
                 for n in ("a", "b")]
        edges = [factories["edge"]("a", "b"), factories["edge"]("b", "a")]
        with pytest.raises(ValueError, match="环|cycle"):
            topo_sort(factories["workflow"](nodes, edges))

    def test_retry_edge_exempt_from_cycle(self, factories):
        """异常处理节点回连原 Code 节点（source_handle='retry'）是合法重试边。"""
        nodes = [factories["code"]("c1", error_branch=True),
                 factories["code"]("handler", inputs=[], outputs=[]),
                 factories["end"]()]
        edges = [
            factories["edge"]("c1", "handler", "error"),   # 错误捕获出边
            factories["edge"]("handler", "c1", "retry"),   # 回连重试边
            factories["edge"]("c1", "end-1"),
        ]
        wf = factories["workflow"](nodes, edges)
        assert find_cycle(wf) is None
        order = topo_sort(wf)  # 不应抛异常
        assert order.index("c1") < order.index("handler")

    def test_normal_back_edge_still_cycle(self, factories):
        """普通出边回连仍视为环（只有 retry 边豁免）。"""
        nodes = [factories["code"]("c1", inputs=[], outputs=[]),
                 factories["code"]("handler", inputs=[], outputs=[])]
        edges = [factories["edge"]("c1", "handler"),
                 factories["edge"]("handler", "c1", "out")]
        assert find_cycle(factories["workflow"](nodes, edges)) is not None


class TestReachability:
    def test_unreachable_node_detected(self, factories):
        from app.engine.topo import unreachable_from_start
        nodes = [factories["start"](), factories["code"]("used"),
                 factories["code"]("orphan", inputs=[], outputs=[]),
                 factories["end"]()]
        edges = [factories["edge"]("start-1", "used"),
                 factories["edge"]("used", "end-1")]
        wf = factories["workflow"](nodes, edges)
        assert "orphan" in unreachable_from_start(wf)

    def test_all_reachable(self, factories):
        from app.engine.topo import unreachable_from_start
        wf = factories["workflow"](
            [factories["start"](), factories["code"](), factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "end-1")],
        )
        assert unreachable_from_start(wf) == []
