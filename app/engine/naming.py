"""Shared naming helpers for DAG variables and Python call boundaries."""
from __future__ import annotations

import keyword
import re

_PY_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def python_param_name(name: str) -> str:
    """Map an external DAG/argparse name to a valid Python keyword name.

    External workflow JSON keeps hyphenated names such as ``task-id`` and
    ``arg-1``. Legacy code paths may still need Python-safe keyword names,
    so this helper preserves the old boundary mapping for compatibility.
    """
    value = re.sub(r"[^0-9A-Za-z_]", "_", str(name or ""))
    if value and value[0].isdigit():
        value = "_" + value
    return value


def is_valid_python_param_name(name: str) -> bool:
    mapped = python_param_name(name)
    return bool(mapped and _PY_IDENT_RE.fullmatch(mapped)
                and not keyword.iskeyword(mapped))


def python_arg_map(args: dict) -> dict:
    """Return args keyed by Python-safe names; reject ambiguous collisions."""
    out = {}
    owners: dict[str, str] = {}
    for raw_name, value in (args or {}).items():
        py_name = python_param_name(str(raw_name))
        if py_name in owners and owners[py_name] != raw_name:
            raise ValueError(
                f"参数名 {owners[py_name]!r} 与 {raw_name!r} 映射到同一个 "
                f"Python 形参 {py_name!r}")
        owners[py_name] = str(raw_name)
        out[py_name] = value
    return out
