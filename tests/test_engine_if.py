"""IF/ELSE 节点引擎测试（PRD 4.4，8 个操作符 + 分支路由）。"""
import pytest


def build_if_workflow(factories, operator, variable_value, compare_value=None,
                      variable="user_input", var_type="string",
                      value_type="constant"):
    """start -> if -> (if: then_code -> end1) / (else: else_code -> end2)"""
    then_code = factories["code"](
        "then", inputs=[], outputs=[{"name": "route", "type": "string"}],
        code="def main():\n    return {\"result\": \"then\"}\n")
    else_code = factories["code"](
        "els", inputs=[], outputs=[{"name": "route2", "type": "string"}],
        code="def main():\n    return {\"result\": \"else\"}\n")
    cond = factories["if_"]("cond", variable=variable, operator=operator,
                            value=compare_value, value_type=value_type)
    wf = factories["workflow"](
        [factories["start"]("s", inputs=[{"name": variable, "type": var_type}]),
         cond, then_code, else_code,
         factories["end"]("e1"), factories["end"]("e2")],
        [factories["edge"]("s", "cond"),
         factories["edge"]("cond", "then", "if"),
         factories["edge"]("cond", "els", "else"),
         factories["edge"]("then", "e1"),
         factories["edge"]("els", "e2")],
    )
    return wf


class TestOperators:
    @pytest.mark.parametrize("op,val,cmp,expect_then", [
        ("包含", "hello world", "world", True),
        ("包含", "hello", "xyz", False),
        ("包含", [1, 2, 3], 2, True),
        ("包含", [1, 2, 3], 9, False),
        ("不包含", "hello", "xyz", True),
        ("不包含", "hello", "ell", False),
        ("开始是", "hello", "he", True),
        ("开始是", "hello", "lo", False),
        ("结束是", "hello", "lo", True),
        ("结束是", "hello", "he", False),
        ("是", "abc", "abc", True),
        ("是", "abc", "abd", False),
        ("不是", "abc", "abd", True),
        ("不是", "abc", "abc", False),
    ])
    async def test_binary_operators(self, engine_factory, factories,
                                    op, val, cmp, expect_then):
        var_type = "list" if isinstance(val, list) else "string"
        wf = build_if_workflow(factories, op, val, cmp, var_type=var_type)
        result = await engine_factory(wf).run({"user_input": val})
        assert result["status"] == "success"
        then_rec = result["node_records"]["then"]["status"]
        else_rec = result["node_records"]["els"]["status"]
        if expect_then:
            assert then_rec == "success" and else_rec == "skipped"
        else:
            assert then_rec == "skipped" and else_rec == "success"

    @pytest.mark.parametrize("val,op,expect_then", [
        ("", "为空", True),
        ("x", "为空", False),
        ([], "为空", True),
        ([1], "为空", False),
        ({}, "为空", True),
        (None, "为空", True),
        ("", "不为空", False),
        ("x", "不为空", True),
        ([1], "不为空", True),
        ({}, "不为空", False),
    ])
    async def test_empty_operators(self, engine_factory, factories,
                                   val, op, expect_then):
        var_type = "list" if isinstance(val, list) else \
                   "dict" if isinstance(val, dict) else "string"
        wf = build_if_workflow(factories, op, val, var_type=var_type)
        result = await engine_factory(wf).run({"user_input": val})
        then_rec = result["node_records"]["then"]["status"]
        else_rec = result["node_records"]["els"]["status"]
        if expect_then:
            assert then_rec == "success" and else_rec == "skipped"
        else:
            assert then_rec == "skipped" and else_rec == "success"

    async def test_compare_with_variable(self, engine_factory, factories):
        """比较值可引用另一个变量。"""
        then_code = factories["code"](
            "then", inputs=[], outputs=[{"name": "r", "type": "string"}],
            code="def main():\n    return {\"result\": \"eq\"}\n")
        else_code = factories["code"](
            "els", inputs=[], outputs=[{"name": "r2", "type": "string"}],
            code="def main():\n    return {\"result\": \"ne\"}\n")
        cond = factories["if_"]("cond", variable="a", operator="是",
                                value="b", value_type="variable")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "a", "type": "string"},
                                             {"name": "b", "type": "string"}]),
             cond, then_code, else_code,
             factories["end"]("e1"), factories["end"]("e2")],
            [factories["edge"]("s", "cond"),
             factories["edge"]("cond", "then", "if"),
             factories["edge"]("cond", "els", "else"),
             factories["edge"]("then", "e1"), factories["edge"]("els", "e2")],
        )
        result = await engine_factory(wf).run({"a": "same", "b": "same"})
        assert result["variables"]["r"]["value"] == "eq"
        result2 = await engine_factory(wf).run({"a": "x", "b": "y"})
        assert result2["variables"]["r2"]["value"] == "ne"


class TestIfRouting:
    async def test_multi_condition_routes_first_matching_branch(self,
                                                                engine_factory,
                                                                factories):
        first = factories["code"](
            "first", inputs=[], outputs=[{"name": "first_result", "type": "string"}],
            code="def main():\n    return {\"first_result\": \"first\"}\n")
        second = factories["code"](
            "second", inputs=[], outputs=[{"name": "second_result", "type": "string"}],
            code="def main():\n    return {\"second_result\": \"second\"}\n")
        fallback = factories["code"](
            "fallback", inputs=[], outputs=[{"name": "fallback_result", "type": "string"}],
            code="def main():\n    return {\"fallback_result\": \"fallback\"}\n")
        cond = factories["node"]("cond", "if", "cond", {
            "branch_mode": "multi",
            "conditions": [
                {"variable": "value", "operator": "是", "value": "one", "value_type": "constant"},
                {"variable": "value", "operator": "是", "value": "two", "value_type": "constant"},
            ],
        })
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "value", "type": "string"}]),
             cond, first, second, fallback, factories["end"]("e1"),
             factories["end"]("e2"), factories["end"]("e3")],
            [factories["edge"]("s", "cond"),
             factories["edge"]("cond", "first", "if-1"),
             factories["edge"]("cond", "second", "if-2"),
             factories["edge"]("cond", "fallback", "else"),
             factories["edge"]("first", "e1"), factories["edge"]("second", "e2"),
             factories["edge"]("fallback", "e3")],
        )
        result = await engine_factory(wf).run({"value": "two"})
        assert result["status"] == "success"
        assert result["node_records"]["first"]["status"] == "skipped"
        assert result["node_records"]["second"]["status"] == "success"
        assert result["node_records"]["fallback"]["status"] == "skipped"

    async def test_reaches_correct_end(self, engine_factory, factories):
        """到达任一 End 即成功；未走分支的 End 不触发。"""
        wf = build_if_workflow(factories, "是", "go", "go")
        result = await engine_factory(wf).run({"user_input": "go"})
        assert result["status"] == "success"
        assert result["end_node"] == "e1"

    async def test_if_variable_missing_fails(self, engine_factory, factories):
        then_code = factories["code"](
            "then", inputs=[], outputs=[{"name": "r", "type": "string"}],
            code="def main():\n    return {\"result\": \"x\"}\n")
        else_code = factories["code"](
            "els", inputs=[], outputs=[{"name": "r2", "type": "string"}],
            code="def main():\n    return {\"result\": \"y\"}\n")
        cond = factories["if_"]("cond", variable="ghost", operator="为空")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[]), cond, then_code, else_code,
             factories["end"]("e1"), factories["end"]("e2")],
            [factories["edge"]("s", "cond"),
             factories["edge"]("cond", "then", "if"),
             factories["edge"]("cond", "els", "else"),
             factories["edge"]("then", "e1"), factories["edge"]("els", "e2")],
        )
        result = await engine_factory(wf).run({})
        assert result["status"] == "failed"
        assert result["failed_node"] == "cond"
