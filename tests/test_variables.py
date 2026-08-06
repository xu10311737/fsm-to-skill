"""变量系统与模板渲染测试（PRD 第 9 章）。

契约：
- app.engine.variables: is_valid_var_name / check_type / types_compatible
- app.engine.context.VariableContext:
    define(name, type, value, owner) / get(name) / has(name)
    render(template) / snapshot()
"""
import pytest

from app.engine.variables import (  # TDD：尚不存在
    RESERVED_NAMES, check_type, is_valid_var_name, types_compatible,
)
from app.engine.context import VariableContext  # TDD：尚不存在


class TestVarNameRules:
    @pytest.mark.parametrize("name", [
        "user_input", "code1_output", "for-1-output", "a-b",
        "A", "_private", "camelCaseX9",
    ])
    def test_valid_names(self, name):
        assert is_valid_var_name(name) is True

    @pytest.mark.parametrize("name", [
        "1abc", "9", "a b", "a.b", "a/b", "", "a中b", "驼峰x9",
    ])
    def test_invalid_names(self, name):
        assert is_valid_var_name(name) is False

    def test_case_sensitive(self):
        """保留字匹配大小写敏感：result 保留，Result 可用。"""
        assert is_valid_var_name("Result")
        assert not is_valid_var_name("result")

    @pytest.mark.parametrize("name", [
        "index", "item", "result", "true", "false", "none",
        "and", "or", "not", "if", "else", "for", "in", "def", "return",
    ])
    def test_reserved_names_rejected(self, name):
        assert name in RESERVED_NAMES
        assert is_valid_var_name(name) is False


class TestTypeSystem:
    @pytest.mark.parametrize("value,type_,expected", [
        ("abc", "string", True),
        (123, "string", False),
        (1, "int", True),
        (True, "int", False),           # bool 不算 int
        (1.5, "float", True),
        (1, "float", True),             # int 可接受为 float
        ("1.5", "float", False),
        ([1, 2], "list", True),
        ((1, 2), "list", False),
        ({"a": 1}, "dict", True),
        ([], "list", True),
        ({}, "dict", True),
    ])
    def test_check_type(self, value, type_, expected):
        assert check_type(value, type_) is expected

    def test_same_type_compatible(self):
        assert types_compatible("string", "string")
        assert types_compatible("list", "list")

    def test_int_float_compatible(self):
        assert types_compatible("int", "float")
        assert types_compatible("float", "int")

    def test_incompatible(self):
        assert not types_compatible("string", "int")
        assert not types_compatible("list", "dict")
        assert not types_compatible("dict", "string")


class TestVariableContext:
    def test_define_and_get(self):
        ctx = VariableContext()
        ctx.define("user_input", "string", "你好", owner="start-1")
        assert ctx.get("user_input") == "你好"
        assert ctx.get_type("user_input") == "string"

    def test_get_missing_raises(self):
        ctx = VariableContext()
        with pytest.raises(KeyError):
            ctx.get("nope")

    def test_duplicate_define_rejected(self):
        """同一变量不能被重复写入。"""
        ctx = VariableContext()
        ctx.define("x", "string", "1", owner="n1")
        with pytest.raises(ValueError, match="重复|duplicate"):
            ctx.define("x", "string", "2", owner="n2")

    def test_type_mismatch_on_define(self):
        """节点输出值必须符合声明类型。"""
        ctx = VariableContext()
        with pytest.raises(TypeError):
            ctx.define("x", "int", "not-an-int", owner="n1")

    def test_owner_recorded(self):
        ctx = VariableContext()
        ctx.define("x", "string", "v", owner="n1")
        assert ctx.owner_of("x") == "n1"

    def test_snapshot_serializable(self):
        import json
        ctx = VariableContext()
        ctx.define("s", "string", "v", owner="n1")
        ctx.define("l", "list", [1, 2], owner="n2")
        snap = ctx.snapshot()
        json.dumps(snap, ensure_ascii=False)
        assert snap["s"]["value"] == "v"
        assert snap["l"]["type"] == "list"


class TestTemplateRender:
    def test_basic_render(self):
        ctx = VariableContext()
        ctx.define("user_input", "string", "世界", owner="s")
        assert ctx.render("你好 {{ user_input }}！") == "你好 世界！"

    def test_string_inserted_raw(self):
        ctx = VariableContext()
        ctx.define("s", "string", "a\"b", owner="n")
        assert ctx.render("{{ s }}") == 'a"b'

    def test_list_dict_inserted_as_utf8_json(self):
        ctx = VariableContext()
        ctx.define("l", "list", ["中", 2], owner="n")
        ctx.define("d", "dict", {"k": "值"}, owner="n")
        assert ctx.render("{{ l }}") == '["中", 2]'
        assert ctx.render("{{ d }}") == '{"k": "值"}'

    def test_int_float_render(self):
        ctx = VariableContext()
        ctx.define("i", "int", 42, owner="n")
        ctx.define("f", "float", 3.5, owner="n")
        assert ctx.render("{{ i }}/{{ f }}") == "42/3.5"

    def test_missing_variable_raises(self):
        """变量不存在时模板渲染失败（LLM 节点失败）。"""
        ctx = VariableContext()
        with pytest.raises(Exception, match="user_input|未定义|undefined"):
            ctx.render("{{ user_input }}")

    def test_syntax_error_raises(self):
        ctx = VariableContext()
        with pytest.raises(Exception):
            ctx.render("{{ 1 + }}")

    def test_multiple_vars(self):
        ctx = VariableContext()
        ctx.define("a", "string", "A", owner="n")
        ctx.define("b", "string", "B", owner="n")
        assert ctx.render("{{ a }}-{{ b }}-{{ a }}") == "A-B-A"

    def test_template_without_vars_unchanged(self):
        ctx = VariableContext()
        assert ctx.render("纯文本无变量") == "纯文本无变量"
