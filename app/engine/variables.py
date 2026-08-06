"""变量命名规则与类型系统（PRD 第 9 章）。

- 变量名：ASCII 标识符（字母/下划线开头，字母数字下划线），大小写敏感。
- 保留字不可用作变量名（For 循环局部变量、Code 返回键、模板/脚本关键字）。
- 五种类型：string / int / float / list / dict；int 与 float 互相兼容。
"""
from __future__ import annotations

import re
from typing import Any

TYPES = ("string", "int", "float", "list", "dict")

RESERVED_NAMES = frozenset({
    # 引擎内建
    "index", "item", "len", "total", "result",
    # 字面量
    "true", "false", "none", "True", "False", "None",
    # 模板/脚本关键字
    "and", "or", "not", "if", "elif", "else", "for", "while", "in",
    "def", "return", "class", "import", "from", "as", "with", "lambda",
    "is", "not", "pass", "break", "continue", "raise", "try", "except",
})

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def is_valid_var_name(name: str) -> bool:
    """合法变量名：ASCII 标识符且非保留字。"""
    if not name or not _NAME_RE.match(name):
        return False
    return name not in RESERVED_NAMES


def check_type(value: Any, type_: str) -> bool:
    """值是否符合声明类型。bool 既不是 int 也不是 float。

    None 兼容任何类型（可空语义：Start 输入/IF 判空场景允许空值进入上下文）。
    """
    if value is None:
        return True
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_ == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "list":
        return isinstance(value, list)
    if type_ == "dict":
        return isinstance(value, dict)
    return False


def types_compatible(declared: str, actual: str) -> bool:
    """声明类型与实际类型是否兼容（int <-> float 双向兼容）。"""
    if declared == actual:
        return True
    return {declared, actual} == {"int", "float"}
