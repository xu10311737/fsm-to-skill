"""变量上下文：一次运行的全局变量表（支持 For 循环的局部作用域链）。

规则：
- 变量只写一次：同一作用域链上重复 define 抛 ValueError。
- define 时校验值与声明类型一致，不一致抛 TypeError。
- 模板渲染使用 Jinja2：string 原值插入；list/dict 以 UTF-8 JSON 插入；
  未定义变量渲染抛错（StrictUndefined）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import jinja2

from .variables import check_type, is_valid_var_name

_JINJA_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _finalize(value: Any) -> str:
    """Jinja 表达式结果的字符串化规则。"""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


_env = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    finalize=_finalize,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_template(template: str, vars_map: dict[str, Any]) -> str:
    """用给定变量表渲染模板。未定义变量/语法错误直接抛异常。

    引擎自动命名变量（如 ``for-1-output``）含连字符，不是合法 Jinja
    标识符；渲染前将其替换为下划线别名并注入别名映射。
    """
    text = template
    merged = dict(vars_map)
    for name in sorted(vars_map, key=len, reverse=True):
        if _JINJA_IDENT_RE.match(name):
            continue
        alias = re.sub(r"[^0-9A-Za-z_]", "_", name)
        if not alias or alias[0].isdigit():
            alias = "_" + alias
        if alias in merged:
            continue
        text = text.replace(name, alias)
        merged[alias] = vars_map[name]
    try:
        return _env.from_string(text).render(merged)
    except jinja2.UndefinedError as e:
        raise KeyError(f"模板引用了未定义的变量: {e}") from e


class VariableContext:
    """一次运行的变量表；parent 用于 For 循环局部作用域。"""

    def __init__(self, parent: Optional["VariableContext"] = None):
        self._vars: dict[str, dict[str, Any]] = {}
        self._parent = parent

    # ------------------------------------------------------------------
    # 定义与读取
    # ------------------------------------------------------------------
    def define(self, name: str, type_: str, value: Any, owner: str) -> None:
        if not is_valid_var_name(name):
            raise ValueError(f"非法变量名: {name!r}")
        if self.has(name):
            raise ValueError(f"变量重复定义（duplicate）: {name}")
        if not check_type(value, type_):
            raise TypeError(
                f"变量 {name} 声明类型为 {type_}，实际值为 {value!r}"
            )
        self._vars[name] = {"type": type_, "value": value, "owner": owner}

    def define_system(self, name: str, type_: str, value: Any,
                      owner: str, local_only: bool = False) -> None:
        """引擎内部变量定义：跳过变量名校验，保留唯一性与类型校验。

        用于自动命名变量（``<节点id>-output``、``index``/``item`` 等），
        这些名字可能含连字符或属于保留字，不面向用户声明。

        ``local_only=True`` 时仅检查当前作用域，允许遮蔽父作用域中的
        同名变量——嵌套 For 循环的 ``index``/``item``/``len``/``total``
        需要在子作用域中重新定义而不与外层冲突。
        """
        if (name in self._vars) if local_only else self.has(name):
            raise ValueError(f"变量重复定义（duplicate）: {name}")
        if not check_type(value, type_):
            raise TypeError(
                f"变量 {name} 声明类型为 {type_}，实际值为 {value!r}"
            )
        self._vars[name] = {"type": type_, "value": value, "owner": owner}

    def remove_owned(self, owner: str) -> None:
        """删除本作用域中归属于指定节点的全部变量（retry 重入前清理）。"""
        for key in [k for k, v in self._vars.items() if v["owner"] == owner]:
            del self._vars[key]

    def _lookup(self, name: str) -> dict[str, Any]:
        if name in self._vars:
            return self._vars[name]
        if self._parent is not None:
            return self._parent._lookup(name)
        raise KeyError(name)

    def get(self, name: str) -> Any:
        return self._lookup(name)["value"]

    def get_type(self, name: str) -> str:
        return self._lookup(name)["type"]

    def owner_of(self, name: str) -> str:
        return self._lookup(name)["owner"]

    def has(self, name: str) -> bool:
        if name in self._vars:
            return True
        return self._parent.has(name) if self._parent else False

    def names(self) -> list[str]:
        names = self._parent.names() if self._parent else []
        return names + list(self._vars.keys())

    # ------------------------------------------------------------------
    # 渲染与快照
    # ------------------------------------------------------------------
    def render(self, template: str) -> str:
        return render_template(template, self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        base = self._parent.as_dict() if self._parent else {}
        base.update({k: v["value"] for k, v in self._vars.items()})
        return base

    def snapshot(self) -> dict[str, dict[str, Any]]:
        base = self._parent.snapshot() if self._parent else {}
        base.update({k: dict(v) for k, v in self._vars.items()})
        return base
