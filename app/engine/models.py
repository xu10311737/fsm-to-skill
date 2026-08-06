"""工作流数据模型。

Workflow / Node / Edge 是不可变配置的轻量封装，
引擎运行态（变量上下文、节点状态）不放在这里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Node:
    id: str
    type: str  # start | code | llm | if | for | aggregate | end
    name: str
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Node":
        return cls(
            id=data["id"],
            type=data["type"],
            name=data.get("name") or data["id"],
            config=dict(data.get("config") or {}),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "name": self.name,
                "config": self.config}


@dataclass
class Edge:
    id: str
    source: str
    target: str
    source_handle: str = "out"

    @classmethod
    def from_dict(cls, data: dict) -> "Edge":
        return cls(
            id=data.get("id") or f"e-{data['source']}-{data['target']}",
            source=data["source"],
            target=data["target"],
            source_handle=data.get("source_handle", "out"),
        )

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "target": self.target,
                "source_handle": self.source_handle}


@dataclass
class Workflow:
    id: str
    name: str
    nodes: list[Node]
    edges: list[Edge]
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Workflow":
        return cls(
            id=data.get("id", "wf"),
            name=data.get("name", "未命名工作流"),
            description=data.get("description", ""),
            nodes=[Node.from_dict(n) for n in data.get("nodes", [])],
            edges=[Edge.from_dict(e) for e in data.get("edges", [])],
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    # ------------------------------------------------------------------
    # 查询辅助
    # ------------------------------------------------------------------
    def node_by_id(self, node_id: str) -> Optional[Node]:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def nodes_of_type(self, type_: str) -> list[Node]:
        return [n for n in self.nodes if n.type == type_]

    def out_edges(self, node_id: str, include_retry: bool = False) -> list[Edge]:
        return [e for e in self.edges
                if e.source == node_id
                and (include_retry or e.source_handle != "retry")]

    def in_edges(self, node_id: str, include_retry: bool = False) -> list[Edge]:
        return [e for e in self.edges
                if e.target == node_id
                and (include_retry or e.source_handle != "retry")]

    def start_node(self) -> Optional[Node]:
        starts = self.nodes_of_type("start")
        return starts[0] if starts else None
