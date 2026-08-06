"""针对测试工程师发现的 bug 的回归测试。

Bug 1: bool 类型自相矛盾 —— `_infer_type(True)` 推断为 "int"，但
        `check_type(True, "int")` 返回 False，导致 Code 节点输出 bool
        或 For 遍历含 bool 的 list 时引擎必然失败。
Bug 2: 嵌套 For 循环不可用 —— 循环局部变量 index/item/len/total 用
        `define_system` 定义，其 `has()` 沿父作用域链查找，内层循环定义
        index 时与外层 index 冲突，抛"变量重复定义"。
"""
import pytest


@pytest.mark.asyncio
async def test_code_output_bool_is_normalized(factories, engine_factory):
    """Code 节点返回 bool 值（未声明类型）应能正常执行并被归一化为 int。"""
    code = factories["code"](
        "c1", inputs=[{"name": "num", "type": "int", "source": "num"}],
        outputs=[],
        code=("def main(num):\n"
              "    return {\"is_big\": num > 5, \"tag\": 'T' + str(num)}\n"))
    wf = factories["workflow"](
        [factories["start"]("s", inputs=[{"name": "num", "type": "int"}]),
         code, factories["end"]()],
        [factories["edge"]("s", "c1"), factories["edge"]("c1", "end-1")])
    result = await engine_factory(wf).run({"num": 10})
    assert result["status"] == "success"
    assert result["variables"]["is_big"]["type"] == "int"
    assert result["variables"]["is_big"]["value"] == 1
    assert result["variables"]["tag"]["value"] == "T10"


@pytest.mark.asyncio
async def test_for_loop_traverses_bool_list(factories, engine_factory):
    """For 循环遍历含 bool 的 list 应能正常执行（item 归一化为 int）。"""
    body_code = factories["code"](
        "bc", inputs=[{"name": "item", "type": "int", "source": "item"}],
        outputs=[{"name": "out", "type": "string"}],
        code="def main(item):\n    return {\"out\": str(item)}\n")
    wf = factories["workflow"](
        [factories["start"]("s", inputs=[{"name": "items", "type": "list"}]),
         factories["for_"]("f", list_source="items",
                           body_nodes=[body_code], body_edges=[]),
         factories["end"]()],
        [factories["edge"]("s", "f"), factories["edge"]("f", "end-1")])
    result = await engine_factory(wf).run({"items": [True, False, True]})
    assert result["status"] == "success"
    assert result["variables"]["f-output"]["value"] == ["1", "0", "1"]


@pytest.mark.asyncio
async def test_nested_for_loop_works(factories, engine_factory):
    """嵌套 For 循环应能正常工作（内层 index/item 遮蔽外层）。"""
    inner_code = factories["code"](
        "b2c", inputs=[{"name": "item", "type": "int", "source": "item"}],
        outputs=[{"name": "out", "type": "int"}],
        code="def main(item):\n    return {\"out\": item * 2}\n")
    inner_for = factories["for_"]("inner", list_source="item",
                                  body_nodes=[inner_code], body_edges=[])
    outer_code = factories["code"](
        "b1c",
        inputs=[{"name": "inner", "type": "list", "source": "inner-output"},
                {"name": "item", "type": "string", "source": "item"}],
        outputs=[{"name": "out", "type": "string"}],
        code=("def main(inner, item):\n"
              "    return {\"out\": str(item) + ':' + str(sum(inner))}\n"))
    outer_for = factories["for_"]("outer", list_source="matrix",
                                  body_nodes=[inner_for, outer_code],
                                  body_edges=[factories["edge"](
                                      "inner", "b1c")])
    wf = factories["workflow"](
        [factories["start"]("s",
                            inputs=[{"name": "matrix", "type": "list"}]),
         outer_for, factories["end"]()],
        [factories["edge"]("s", "outer"),
         factories["edge"]("outer", "end-1")])
    result = await engine_factory(wf).run({"matrix": [[1, 2], [3, 4]]})
    assert result["status"] == "success"
    assert result["variables"]["outer-output"]["value"] == ["[1, 2]:6",
                                                            "[3, 4]:14"]


@pytest.mark.asyncio
async def test_complex_workflow_runs(factories, engine_factory):
    """集成：多层嵌套 For + IF 分支 + Code 汇总的复杂工作流可跑通。"""
    double = factories["code"](
        "double", inputs=[{"name": "item", "type": "int", "source": "item"}],
        outputs=[{"name": "out", "type": "int"}],
        code="def main(item):\n    return {\"out\": item * 2}\n")
    inner_for = factories["for_"]("inner", list_source="item",
                                  body_nodes=[double], body_edges=[])
    calc = factories["code"](
        "calc",
        inputs=[{"name": "row", "type": "list", "source": "inner-output"},
                {"name": "threshold", "type": "int", "source": "threshold"}],
        outputs=[{"name": "row-sum", "type": "int"},
                 {"name": "decision", "type": "string"}],
        code=("def main(row, threshold):\n"
              "    total = sum(row)\n"
              "    return {\"row-sum\": total, \"decision\": "
              "'big' if total > threshold else 'small'}\n"))
    if_node = factories["node"]("big", "if", "big", {
        "branch_mode": "multi",
        "conditions": [{"variable": "decision", "operator": "是",
                        "value": "small", "value_type": "constant"}],
    })
    yes_code = factories["code"](
        "yes", inputs=[{"name": "s", "type": "string", "source": "decision"}],
        outputs=[{"name": "yes-out", "type": "string"}],
        code="def main(s):\n    return {\"yes-out\": 'BIG:' + s}\n")
    no_code = factories["code"](
        "no", inputs=[{"name": "s", "type": "string", "source": "decision"}],
        outputs=[{"name": "no-out", "type": "string"}],
        code="def main(s):\n    return {\"no-out\": 'small:' + s}\n")
    fmt = factories["code"](
        "fmt",
        inputs=[{"name": "index", "type": "int", "source": "index"},
                {"name": "row", "type": "list", "source": "inner-output"},
                {"name": "decision", "type": "string", "source": "decision"}],
        outputs=[{"name": "fmt-out", "type": "string"}],
        code=("def main(index, row, decision):\n"
              "    return {\"fmt-out\": "
              "f\"i={index} sum={sum(row)} {decision}\"}\n"))
    outer_for = factories["for_"](
        "outer", list_source="matrix",
        body_nodes=[inner_for, calc, if_node, yes_code, no_code, fmt],
        body_edges=[factories["edge"]("inner", "calc"),
                    factories["edge"]("calc", "big"),
                    factories["edge"]("big", "yes", "if-1"),
                    factories["edge"]("big", "no", "else"),
                    factories["edge"]("calc", "fmt")])
    wf = factories["workflow"](
        [factories["start"]("s",
                            inputs=[{"name": "matrix", "type": "list"},
                                    {"name": "threshold", "type": "int"}]),
         outer_for, factories["end"]()],
        [factories["edge"]("s", "outer"),
         factories["edge"]("outer", "end-1")])
    result = await engine_factory(wf).run(
        {"matrix": [[1, 2], [3, 4], [5, 6]], "threshold": 8})
    assert result["status"] == "success"
    assert result["variables"]["outer-output"]["value"] == [
        "i=0 sum=6 small", "i=1 sum=14 big", "i=2 sum=22 big"]