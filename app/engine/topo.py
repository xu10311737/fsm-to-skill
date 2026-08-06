"""拓扑排序、环检测与可达性分析。

约定：
- source_handle == "retry" 的边是异常处理回连边，不参与环检测与拓扑约束。
- 所有函数接收 workflow dict（含 nodes / edges 键）。
"""
from __future__ import annotations

import heapq

RETRY_HANDLE = "retry"


def _graph(wf: dict) -> tuple[list[str], dict[str, list[str]], dict[str, int]]:
    """构建邻接表与入度表（排除 retry 边）。"""
    node_ids = [n["id"] for n in wf.get("nodes", [])]
    known = set(node_ids)
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    indeg: dict[str, int] = {nid: 0 for nid in node_ids}
    for e in wf.get("edges", []):
        if e.get("source_handle") == RETRY_HANDLE:
            continue
        s, t = e["source"], e["target"]
        if s not in known or t not in known:
            continue
        adj[s].append(t)
        indeg[t] += 1
    return node_ids, adj, indeg


def topo_sort(wf: dict) -> list[str]:
    """Kahn 拓扑排序；并列时按节点声明顺序，保证结果稳定。

    存在环时抛 ValueError。
    """
    node_ids, adj, indeg = _graph(wf)
    pos = {nid: i for i, nid in enumerate(node_ids)}
    ready = [(pos[n], n) for n in node_ids if indeg[n] == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        _, nid = heapq.heappop(ready)
        order.append(nid)
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(ready, (pos[nxt], nxt))
    if len(order) != len(node_ids):
        raise ValueError("工作流存在环（cycle），无法进行拓扑排序")
    return order


def find_cycle(wf: dict) -> list[str] | None:
    """DFS 检测环，返回环上的节点 id 列表；无环返回 None。"""
    node_ids, adj, _ = _graph(wf)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in node_ids}

    for root in node_ids:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        path = [root]
        stack = [(root, iter(adj[root]))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if color[nxt] == GRAY:
                    return path[path.index(nxt):]
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(adj[nxt])))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                path.pop()
                stack.pop()
    return None


def unreachable_from_start(wf: dict) -> list[str]:
    """从 start 节点出发不可达的节点 id（按声明顺序）。"""
    node_ids = [n["id"] for n in wf.get("nodes", [])]
    starts = [n["id"] for n in wf.get("nodes", []) if n.get("type") == "start"]
    adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
    known = set(node_ids)
    for e in wf.get("edges", []):
        s, t = e["source"], e["target"]
        if s in known and t in known:
            adj[s].append(t)
    seen = set(starts)
    queue = list(starts)
    while queue:
        cur = queue.pop(0)
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return [nid for nid in node_ids if nid not in seen]
