"""校验器测试（PRD 6.2 十五项检查 + 已确认决策）。

契约：app.validator.validator.validate_workflow(wf_dict) -> report
report: {"errors": [issue], "warnings": [issue]}
issue: {"code": str, "node_id": str | None, "message": str}
注意：决策已取消「单输入」约束，第 13 项仅保留无环路。
"""
from app.validator.validator import validate_workflow  # TDD：尚不存在


from app.validator.validator import _static_return_keys


def err_codes(report):
    return {i["code"] for i in report["errors"]}


def test_static_return_keys_only_reads_main_returns():
    code = '''
def helper():
    return {"helper": 1}

def main(params):
    def nested():
        return {"nested": 2}
    if params:
        return {"accepted": True}
    return {"rejected": False}
'''

    assert _static_return_keys(code) == ["accepted", "rejected"]


class TestStartEndRules:
    def test_valid_workflow_passes(self, simple_workflow):
        report = validate_workflow(simple_workflow)
        assert report["errors"] == []

    def test_missing_start(self, factories):
        wf = factories["workflow"](
            [factories["code"](), factories["end"]()],
            [factories["edge"]("code-1", "end-1")])
        assert "NO_START" in err_codes(validate_workflow(wf))

    def test_multiple_starts(self, factories):
        wf = factories["workflow"](
            [factories["start"]("s1"), factories["start"]("s2"),
             factories["end"]()],
            [factories["edge"]("s1", "end-1"), factories["edge"]("s2", "end-1")])
        assert "MULTIPLE_STARTS" in err_codes(validate_workflow(wf))

    def test_missing_end(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["code"]()],
            [factories["edge"]("start-1", "code-1")])
        assert "NO_END" in err_codes(validate_workflow(wf))

    def test_start_with_in_edge(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["code"](), factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "start-1", "retry"),
             factories["edge"]("code-1", "end-1")])
        assert "START_HAS_INPUT" in err_codes(validate_workflow(wf))

    def test_end_with_out_edge(self, factories):
        extra = factories["code"]("extra", inputs=[], outputs=[
            {"name": "v9", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        wf = factories["workflow"](
            [factories["start"](), factories["end"](), extra],
            [factories["edge"]("start-1", "end-1"),
             factories["edge"]("end-1", "extra")])
        assert "END_HAS_OUTPUT" in err_codes(validate_workflow(wf))


class TestNodeConfigRules:
    def test_empty_llm_prompt(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt="  "),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        assert "EMPTY_PROMPT" in err_codes(validate_workflow(wf))

    def test_empty_code(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["code"]("c1", code="  \n"),
             factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "EMPTY_CODE" in err_codes(validate_workflow(wf))

    def test_code_syntax_error(self, factories):
        wf = factories["workflow"](
            [factories["start"](),
             factories["code"]("c1", code="def main(:\n"),
             factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "CODE_SYNTAX" in err_codes(validate_workflow(wf))

    def test_code_missing_main(self, factories):
        wf = factories["workflow"](
            [factories["start"](),
             factories["code"]("c1", code="def other():\n    pass\n"),
             factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "NO_MAIN_FUNC" in err_codes(validate_workflow(wf))

    def test_code_main_params_match_inputs(self, factories):
        """main 参数与输入变量一一对应（名称与个数）。"""
        bad = factories["code"](
            "c1",
            inputs=[{"name": "a", "type": "string", "source": "user_input"},
                    {"name": "b", "type": "string", "source": "user_input"}],
            code="def main(a):\n    return {\"result\": a}\n")
        wf = factories["workflow"](
            [factories["start"](), bad, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "MAIN_PARAMS_MISMATCH" in err_codes(validate_workflow(wf))

    def test_code_params_dict_style_is_valid(self, factories):
        node = factories["code"](
            "c1",
            inputs=[{"name": "arg-1", "type": "string", "source": "user_input"}],
            code="def main(params):\n    return {\"result\": params[\"arg-1\"]}\n")
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        codes = err_codes(validate_workflow(wf))
        assert "MAIN_PARAMS_MISMATCH" not in codes

    def test_code_arg_schema_without_source_is_valid(self, factories):
        """新 Code 参数 schema 只声明 Agent 入参，不要求填写 DAG source。"""
        node = factories["code"](
            "c1",
            inputs=[{
                "name": "arg1",
                "description": "输入参数",
                "type": "string",
                "required": True,
            }],
            code="def main(arg1):\n    return {\"result\": arg1}\n")
        wf = factories["workflow"](
            [factories["start"](), node, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        codes = err_codes(validate_workflow(wf))
        assert "UNDEFINED_VARIABLE" not in codes
        assert "MAIN_PARAMS_MISMATCH" not in codes


class TestVariableRules:
    def test_undefined_variable_reference(self, factories):
        bad = factories["code"](
            "c1",
            inputs=[{"name": "a", "type": "string", "source": "ghost"}],
            code="def main(a):\n    return {\"result\": a}\n")
        wf = factories["workflow"](
            [factories["start"](), bad, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "UNDEFINED_VARIABLE" in err_codes(validate_workflow(wf))

    def test_llm_template_undefined_variable(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", "{{ ghost }}"),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        assert "UNDEFINED_VARIABLE" in err_codes(validate_workflow(wf))

    def test_user_message_is_not_system_variable(self, factories):
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[]),
             factories["llm"]("l1", "{{ user-message }}"),
             factories["end"]()],
            [factories["edge"]("s", "l1"),
             factories["edge"]("l1", "end-1")])
        assert "UNDEFINED_VARIABLE" in err_codes(validate_workflow(wf))

    def test_duplicate_variable_name(self, factories):
        c1 = factories["code"]("c1", inputs=[], outputs=[
            {"name": "dup", "type": "string"}],
            code="def main():\n    return {\"result\": \"1\"}\n")
        c2 = factories["code"]("c2", inputs=[], outputs=[
            {"name": "dup", "type": "string"}],
            code="def main():\n    return {\"result\": \"2\"}\n")
        wf = factories["workflow"](
            [factories["start"](), c1, c2, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "c2"),
             factories["edge"]("c2", "end-1")])
        assert "DUPLICATE_VARIABLE" in err_codes(validate_workflow(wf))

    def test_invalid_variable_name(self, factories):
        bad = factories["code"]("c1", inputs=[], outputs=[
            {"name": "1bad-name", "type": "string"}],
            code="def main():\n    return {\"result\": \"x\"}\n")
        wf = factories["workflow"](
            [factories["start"](), bad, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "INVALID_VAR_NAME" in err_codes(validate_workflow(wf))

    def test_reserved_variable_name(self, factories):
        bad = factories["code"]("c1", inputs=[], outputs=[
            {"name": "index", "type": "int"}],
            code="def main():\n    return {\"result\": 1}\n")
        wf = factories["workflow"](
            [factories["start"](), bad, factories["end"]()],
            [factories["edge"]("start-1", "c1"),
             factories["edge"]("c1", "end-1")])
        assert "INVALID_VAR_NAME" in err_codes(validate_workflow(wf))

    def test_type_incompatible_reference(self, factories):
        """引用类型与声明类型不兼容。"""
        producer = factories["code"]("p", inputs=[], outputs=[
            {"name": "num", "type": "int"}],
            code="def main():\n    return {\"result\": 1}\n")
        consumer = factories["code"](
            "c", inputs=[{"name": "s", "type": "string", "source": "num"}],
            outputs=[{"name": "o", "type": "string"}],
            code="def main(s):\n    return {\"result\": s}\n")
        wf = factories["workflow"](
            [factories["start"](), producer, consumer, factories["end"]()],
            [factories["edge"]("start-1", "p"),
             factories["edge"]("p", "c"),
             factories["edge"]("c", "end-1")])
        assert "TYPE_MISMATCH" in err_codes(validate_workflow(wf))

    def test_int_to_float_compatible(self, factories):
        producer = factories["code"]("p", inputs=[], outputs=[
            {"name": "num", "type": "int"}],
            code="def main():\n    return {\"result\": 1}\n")
        consumer = factories["code"](
            "c", inputs=[{"name": "f", "type": "float", "source": "num"}],
            outputs=[{"name": "o", "type": "float"}],
            code="def main(f):\n    return {\"result\": f}\n")
        wf = factories["workflow"](
            [factories["start"](), producer, consumer, factories["end"]()],
            [factories["edge"]("start-1", "p"),
             factories["edge"]("p", "c"),
             factories["edge"]("c", "end-1")])
        assert "TYPE_MISMATCH" not in err_codes(validate_workflow(wf))

    def test_if_join_cannot_reference_single_branch_variable(self, factories):
        """IF 汇合后不允许直接引用只在单侧分支产生的变量。"""
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
             factories["edge"]("join", "end-1")])
        issues = validate_workflow(wf)["errors"]
        assert any(i["code"] == "UNDEFINED_VARIABLE" and
                   i["node_id"] == "join" and "a_val" in i["message"]
                   for i in issues)


class TestStructureRules:
    def test_duplicate_node_id(self, factories):
        c1 = factories["code"]("dup", inputs=[], outputs=[
            {"name": "v1", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        c2 = factories["code"]("dup", inputs=[], outputs=[
            {"name": "v2", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        wf = factories["workflow"](
            [factories["start"](), c1, c2, factories["end"]()],
            [factories["edge"]("start-1", "dup"),
             factories["edge"]("dup", "end-1")])
        assert "DUPLICATE_NODE_ID" in err_codes(validate_workflow(wf))

    def test_cycle_detected(self, factories):
        c1 = factories["code"]("a", inputs=[], outputs=[
            {"name": "v1", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        c2 = factories["code"]("b", inputs=[], outputs=[
            {"name": "v2", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        wf = factories["workflow"](
            [factories["start"](), c1, c2, factories["end"]()],
            [factories["edge"]("start-1", "a"),
             factories["edge"]("a", "b"), factories["edge"]("b", "a"),
             factories["edge"]("b", "end-1")])
        assert "CYCLE" in err_codes(validate_workflow(wf))

    def test_retry_edge_not_cycle(self, factories):
        c1 = factories["code"]("a", error_branch=True)
        h = factories["code"]("h", inputs=[
            {"name": "e", "type": "string", "source": "a-error-message"}],
            outputs=[{"name": "hv", "type": "string"}],
            code="def main(e):\n    return {\"result\": e}\n")
        wf = factories["workflow"](
            [factories["start"](), c1, h, factories["end"]()],
            [factories["edge"]("start-1", "a"),
             factories["edge"]("a", "h", "error"),
             factories["edge"]("h", "a", "retry"),
             factories["edge"]("a", "end-1")])
        assert "CYCLE" not in err_codes(validate_workflow(wf))

    def test_unreachable_node(self, factories):
        orphan = factories["code"]("orphan", inputs=[], outputs=[
            {"name": "ov", "type": "string"}],
            code="def main():\n    return {\"result\": \"\"}\n")
        wf = factories["workflow"](
            [factories["start"](), factories["code"](), orphan,
             factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "end-1")])
        assert "UNREACHABLE_NODE" in err_codes(validate_workflow(wf))

    def test_if_branches_must_connect(self, factories):
        """IF 与 ELSE 都必须连接下游；未连接 -> Error，阻止运行导出。"""
        wf = factories["workflow"](
            [factories["start"](), factories["if_"]("cond"),
             factories["code"]("t", inputs=[], outputs=[
                 {"name": "tv", "type": "string"}],
                 code="def main():\n    return {\"result\": \"\"}\n"),
             factories["end"]()],
            [factories["edge"]("start-1", "cond"),
             factories["edge"]("cond", "t", "if"),
             factories["edge"]("t", "end-1")])  # else 未连接
        assert "IF_BRANCH_UNCONNECTED" in err_codes(validate_workflow(wf))

    def test_multi_if_branch_cannot_connect_multiple_targets(self, factories):
        cond = factories["node"]("cond", "if", "cond", {
            "branch_mode": "multi",
            "conditions": [{"variable": "value", "operator": "是", "value": "x"}],
        })
        then_a = factories["code"]("then-a", inputs=[], outputs=[
            {"name": "a", "type": "string"}], code="def main():\n    return {\"a\": \"a\"}\n")
        then_b = factories["code"]("then-b", inputs=[], outputs=[
            {"name": "b", "type": "string"}], code="def main():\n    return {\"b\": \"b\"}\n")
        else_node = factories["code"]("els", inputs=[], outputs=[
            {"name": "e", "type": "string"}], code="def main():\n    return {\"e\": \"e\"}\n")
        wf = factories["workflow"](
            [factories["start"]("s", inputs=[{"name": "value", "type": "string"}]),
             cond, then_a, then_b, else_node, factories["end"]()],
            [factories["edge"]("s", "cond"),
             factories["edge"]("cond", "then-a", "if-1"),
             factories["edge"]("cond", "then-b", "if-1"),
             factories["edge"]("cond", "els", "else"),
             factories["edge"]("then-a", "end-1")])
        assert "IF_BRANCH_MULTIPLE_TARGETS" in err_codes(validate_workflow(wf))

    def test_aggregate_input_must_be_direct_and_same_type(self, factories):
        producer = factories["code"]("p", inputs=[], outputs=[
            {"name": "total", "type": "int"}], code="def main():\n    return {\"total\": 1}\n")
        agg = factories["aggregate"]("agg", output_type="string")
        agg["config"]["inputs"] = [{"source": "total", "type": "string"}]
        wf = factories["workflow"](
            [factories["start"](), producer, agg, factories["end"]()],
            [factories["edge"]("start-1", "p"), factories["edge"]("p", "agg"),
             factories["edge"]("agg", "end-1")])
        assert "TYPE_MISMATCH" in err_codes(validate_workflow(wf))

    def test_multiple_in_edges_no_error(self, factories):
        """决策：取消单输入约束，多入边不再是错误。"""
        c1 = factories["code"]("a", inputs=[], outputs=[
            {"name": "v1", "type": "int"}],
            code="def main():\n    return {\"result\": 1}\n")
        c2 = factories["code"]("b", inputs=[], outputs=[
            {"name": "v2", "type": "int"}],
            code="def main():\n    return {\"result\": 2}\n")
        agg = factories["aggregate"]("agg", output_type="int")
        wf = factories["workflow"](
            [factories["start"](), c1, c2, agg, factories["end"]()],
            [factories["edge"]("start-1", "a"), factories["edge"]("start-1", "b"),
             factories["edge"]("a", "agg"), factories["edge"]("b", "agg"),
             factories["edge"]("agg", "end-1")])
        report = validate_workflow(wf)
        assert "MULTIPLE_INPUTS" not in err_codes(report)
        assert report["errors"] == []


class TestForRules:
    def test_for_input_must_be_list(self, factories):
        loop = factories["for_"]("f1", list_source="s", body_nodes=[])
        loop["config"]["collect"] = "result"
        wf = factories["workflow"](
            [factories["start"]("st", inputs=[{"name": "s", "type": "string"}]),
             loop, factories["end"]()],
            [factories["edge"]("st", "f1"), factories["edge"]("f1", "end-1")])
        assert "FOR_INPUT_NOT_LIST" in err_codes(validate_workflow(wf))

    def test_for_collect_autoinferred(self, factories):
        body = factories["node"]("b1", "code", "b1", {
            "inputs": [{"name": "item", "type": "int", "source": "item"}],
            "outputs": [{"name": "result", "type": "int"}],
            "code": "def main(item):\n    return {\"result\": item}\n",
            "error_branch": False})
        loop = factories["for_"]("f1", list_source="items", body_nodes=[body])
        # 不配置 collect：由循环体末端输出自动推导
        wf = factories["workflow"](
            [factories["start"]("st", inputs=[{"name": "items", "type": "list"}]),
             loop, factories["end"]()],
            [factories["edge"]("st", "f1"), factories["edge"]("f1", "end-1")])
        assert "FOR_NO_OUTPUT" not in err_codes(validate_workflow(wf))

    def test_nested_for_rejected(self, factories):
        inner = factories["for_"]("inner", list_source="item", body_nodes=[])
        inner["config"]["collect"] = "result"
        outer = factories["for_"]("outer", list_source="items",
                                  body_nodes=[inner])
        outer["config"]["collect"] = "inner-output"
        wf = factories["workflow"](
            [factories["start"]("st", inputs=[{"name": "items", "type": "list"}]),
             outer, factories["end"]()],
            [factories["edge"]("st", "outer"), factories["edge"]("outer", "end-1")])
        assert "NESTED_FOR" in err_codes(validate_workflow(wf))


class TestReportStructure:
    def test_issue_has_node_id(self, factories):
        wf = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt=""),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        report = validate_workflow(wf)
        issue = next(i for i in report["errors"] if i["code"] == "EMPTY_PROMPT")
        assert issue["node_id"] == "l1"
        assert issue["message"]

    def test_warnings_separate_from_errors(self, simple_workflow):
        report = validate_workflow(simple_workflow)
        assert isinstance(report["warnings"], list)
        assert isinstance(report["errors"], list)
