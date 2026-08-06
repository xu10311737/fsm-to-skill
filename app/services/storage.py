"""Workflow YAML 本地存储（PRD 5.1）。

每个工作流一个 YAML 文件，文件名由工作流 id 派生；
list 时静默跳过损坏文件。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z_.-]+")


def _filename(wf: dict[str, Any]) -> str:
    raw = str(wf.get("id") or "workflow")
    safe = _SAFE_NAME_RE.sub("_", raw).strip("._") or "workflow"
    return f"{safe}.yaml"


def save_workflow(directory: str | Path, workflow: dict[str, Any]) -> Path:
    """保存工作流到 directory，返回文件路径。"""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    path = d / _filename(workflow)
    path.write_text(
        yaml.safe_dump(workflow, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return path


def load_workflow(path: str | Path) -> dict[str, Any]:
    """加载工作流；文件不存在抛 FileNotFoundError，损坏抛 ValueError。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"工作流文件不存在: {p}")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"工作流文件损坏（{p}）: {e}") from e
    if not isinstance(data, dict) or "nodes" not in data:
        raise ValueError(f"工作流文件损坏（{p}）：缺少 nodes 结构")
    return data


def list_workflows(directory: str | Path) -> list[dict[str, Any]]:
    """返回 [{"id", "name", "path"}]，跳过损坏文件。"""
    d = Path(directory)
    if not d.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in (".yaml", ".yml") or not p.is_file():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        items.append({"id": data["id"],
                      "name": data.get("name", data["id"]),
                      "path": str(p)})
    return items


def delete_workflow(path: str | Path) -> None:
    """删除工作流文件。"""
    Path(path).unlink(missing_ok=True)
