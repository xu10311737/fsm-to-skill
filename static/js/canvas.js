/* fsm to skill - DAG 画布（SVG 连线 + DOM 节点，无构建依赖） */
import { NODE_TYPES, nodeOutHandles, makeEdge, autoOutputName } from "./store.js";

const NODE_W = 176;
const FOR_NODE_W = 240;
const PORT_GAP = 22;

export class Canvas {
  /**
   * @param wrap  画布外层元素（含 .canvas-content 与 svg.canvas-edges）
   * @param graph {nodes, edges} 数据引用
   * @param opts  {readOnly, onSelect, onChange, onNodeDblClick, allowTypes}
   */
  constructor(wrap, graph, opts = {}) {
    this.wrap = wrap;
    this.content = wrap.querySelector(".canvas-content");
    this.svg = wrap.querySelector("svg.canvas-edges");
    this.graph = graph;
    this.opts = opts;
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.statuses = {};
    this.selectedId = null;
    this._tempPath = null;
    this._bind();
    this.render();
  }

  setGraph(graph) {
    this.graph = graph;
    this.render();
  }

  setStatuses(statuses) {
    this.statuses = statuses || {};
    for (const n of this.graph.nodes) {
      const el = this._nodeEl(n.id);
      if (!el) continue;
      el.classList.remove("st-running", "st-success", "st-failed", "st-skipped", "st-waiting");
      const st = this.statuses[n.id];
      if (st) el.classList.add(`st-${st}`);
      this._syncStatusBadge(el, st);
    }
    this._redrawEdges();
  }

  setSelected(id) {
    this.selectedId = id;
    for (const el of this.content.querySelectorAll(".wf-node")) {
      el.classList.toggle("selected", el.dataset.id === id);
    }
  }

  focusNode(id) {
    const n = this.graph.nodes.find((x) => x.id === id);
    if (!n) return;
    const rect = this.wrap.getBoundingClientRect();
    this.pan.x = rect.width / 2 - (n.position.x + NODE_W / 2) * this.zoom;
    this.pan.y = rect.height / 2 - (n.position.y + 40) * this.zoom;
    this._applyTransform();
    const el = this._nodeEl(id);
    if (el) {
      el.classList.add("flash");
      setTimeout(() => el.classList.remove("flash"), 900);
    }
    this.setSelected(id);
  }

  /* ---------------- 渲染 ---------------- */
  render() {
    this.content.querySelectorAll(".wf-node, .line-plus-btn")
      .forEach((el) => el.remove());
    this.svg.innerHTML = "";
    for (const n of this.graph.nodes) this._renderNode(n);
    for (const e of this.graph.edges) this._renderEdge(e);
    this._applyTransform();
  }

  _nodeEl(id) {
    return this.content.querySelector(`.wf-node[data-id="${CSS.escape(id)}"]`);
  }

  _renderNode(n) {
    const meta = NODE_TYPES[n.type] || { label: n.type, color: "#888" };
    const el = document.createElement("div");
    el.className = `wf-node node-${n.type}`;
    el.dataset.id = n.id;
    el.style.left = `${n.position.x}px`;
    el.style.top = `${n.position.y}px`;
    el.style.setProperty("--node-color", meta.color || "#64748b");
    if (n.type === "for") el.style.width = `${FOR_NODE_W}px`;
    if (this.selectedId === n.id) el.classList.add("selected");
    const st = this.statuses[n.id];
    if (st) el.classList.add(`st-${st}`);

    const head = document.createElement("div");
    head.className = "node-head";
    const title = document.createElement("div");
    title.className = "node-title";
    const dot = document.createElement("span");
    dot.className = "node-dot";
    const nameEl = document.createElement("span");
    nameEl.className = "node-name";
    nameEl.textContent = n.name || n.id;
    nameEl.title = n.id;
    title.append(dot, nameEl);
    head.append(title);
    el.append(head);

    const typeEl = document.createElement("div");
    typeEl.className = "node-type";
    typeEl.append(this._nodeBody(n, meta));
    el.append(typeEl);

    if (st) this._syncStatusBadge(el, st);

    if (n.type !== "start") {
      const inPort = document.createElement("div");
      inPort.className = "node-port port-in";
      inPort.dataset.node = n.id;
      inPort.title = "输入";
      el.append(inPort);
    }
    const handles = nodeOutHandles(n);
    handles.forEach((h, i) => {
      const port = document.createElement("div");
      port.className = "node-port port-out";
      port.dataset.node = n.id;
      port.dataset.handle = h;
      port.style.top = `${30 + i * PORT_GAP}px`;
      port.title = h;
      el.append(port);
      const lab = document.createElement("span");
      lab.className = "port-label";
      lab.style.top = `${26 + i * PORT_GAP}px`;
      lab.textContent = handleLabel(h);
      el.append(lab);
    });

    el.addEventListener("mousedown", (ev) => this._nodeMouseDown(ev, n));
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      this.setSelected(n.id);
      if (this.opts.onSelect) this.opts.onSelect(n.id);
    });
    el.addEventListener("dblclick", (ev) => {
      ev.stopPropagation();
      if (this.opts.onNodeDblClick) this.opts.onNodeDblClick(n.id);
    });
    el.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (this.opts.readOnly) return;
      this.setSelected(n.id);
      if (this.opts.onSelect) this.opts.onSelect(n.id);
      showMenu(ev.clientX, ev.clientY, [
        {
          label: "重命名",
          action: () => {
            const name = prompt("节点名称：", n.name || "");
            if (name !== null) {
              if (this.opts.onRenameNode) {
                if (!this.opts.onRenameNode(n, name)) return;
              } else {
                n.name = name.trim() || n.name;
              }
              this.render();
              this._change();
            }
          },
        },
        {
          label: "删除节点",
          danger: true,
          action: () => {
            this.graph.nodes = this.graph.nodes.filter((x) => x.id !== n.id);
            this.graph.edges = this.graph.edges.filter(
              (e) => e.source !== n.id && e.target !== n.id);
            this.render();
            this._change();
            if (this.opts.onSelect) this.opts.onSelect(null);
          },
        },
      ]);
    });
    this.content.append(el);
  }

  _renderEdge(e) {
    const src = this.graph.nodes.find((n) => n.id === e.source);
    const tgt = this.graph.nodes.find((n) => n.id === e.target);
    if (!src || !tgt) return;
    const { x1, y1, x2, y2 } = this._edgePoints(src, tgt, e.source_handle);
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", edgePath(x1, y1, x2, y2));
    path.setAttribute("class", this._edgeClass(e));
    const hit = document.createElementNS("http://www.w3.org/2000/svg", "path");
    hit.setAttribute("d", edgePath(x1, y1, x2, y2));
    hit.setAttribute("class", "edge-hit");
    hit.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (this.opts.readOnly) return;
      const items = [
        {
          label: "删除连线", danger: true,
          action: () => {
            this.graph.edges = this.graph.edges.filter((x) => x.id !== e.id);
            this.render();
            this._change();
          },
        },
      ];
      if (!this.opts.readOnly) {
        items.unshift({
          label: "在此插入节点",
          submenu: insertableTypes(this.opts.allowTypes, e.source_handle).map((t) => ({
            label: NODE_TYPES[t].label,
            action: () => this._insertNodeOnEdge(e, t),
          })),
        });
      }
      showMenu(ev.clientX, ev.clientY, items);
    });
    g.append(path, hit);
    if (e.source_handle && e.source_handle !== "out") {
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("class", "edge-label");
      t.setAttribute("x", String((x1 + x2) / 2));
      t.setAttribute("y", String((y1 + y2) / 2 - 6));
      t.textContent = e.source_handle;
      g.append(t);
    }
    this.svg.append(g);
    if (!this.opts.readOnly && this.opts.showLinePlus) {
      this._renderLinePlus(e, x1, y1, x2, y2);
    }
  }

  _edgePoints(src, tgt, handle) {
    const handles = nodeOutHandles(src);
    const idx = Math.max(0, handles.indexOf(handle));
    const sourcePoint = this._portPoint(
      src.id, `.port-out[data-handle="${CSS.escape(handle || "out")}"]`);
    const targetPoint = this._portPoint(tgt.id, ".port-in");
    return {
      x1: sourcePoint?.x ?? src.position.x + nodeWidth(src),
      y1: sourcePoint?.y ?? src.position.y + 30 + idx * PORT_GAP + 8,
      x2: targetPoint?.x ?? tgt.position.x,
      y2: targetPoint?.y ?? tgt.position.y + 54,
    };
  }

  _portPoint(nodeId, selector) {
    const port = this._nodeEl(nodeId)?.querySelector(selector);
    if (!port) return null;
    const rect = port.getBoundingClientRect();
    const wrapRect = this.wrap.getBoundingClientRect();
    return {
      x: (rect.left + rect.width / 2 - wrapRect.left - this.pan.x) / this.zoom,
      y: (rect.top + rect.height / 2 - wrapRect.top - this.pan.y) / this.zoom,
    };
  }

  _edgeClass(e) {
    const cls = ["edge-path"];
    if (e.source_handle === "retry") cls.push("edge-retry");
    const srcStatus = this.statuses[e.source];
    const tgtStatus = this.statuses[e.target];
    if (srcStatus === "running" || tgtStatus === "running" ||
        srcStatus === "waiting" || tgtStatus === "waiting") {
      cls.push("edge-active");
    } else if (srcStatus === "success" && tgtStatus === "success") {
      cls.push("edge-success");
    } else if (srcStatus === "failed" || tgtStatus === "failed") {
      cls.push("edge-failed");
    }
    return cls.join(" ");
  }

  _renderLinePlus(edge, x1, y1, x2, y2) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "line-plus-btn";
    btn.textContent = "+";
    btn.title = "在连线中插入节点";
    btn.style.left = `${(x1 + x2) / 2}px`;
    btn.style.top = `${(y1 + y2) / 2}px`;
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const items = insertableTypes(this.opts.allowTypes, edge.source_handle)
        .map((t) => ({
          label: NODE_TYPES[t].label,
          action: () => this._insertNodeOnEdge(edge, t),
        }));
      items.push({
        label: "删除连线",
        danger: true,
        action: () => this._deleteEdge(edge.id),
      });
      showMenu(ev.clientX, ev.clientY, items);
    });
    btn.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showMenu(ev.clientX, ev.clientY, [{
        label: "删除连线",
        danger: true,
        action: () => this._deleteEdge(edge.id),
      }]);
    });
    this.content.append(btn);
  }

  _deleteEdge(edgeId) {
    this.graph.edges = this.graph.edges.filter((x) => x.id !== edgeId);
    this.render();
    this._change();
  }

  _nodeBody(n, meta) {
    const frag = document.createDocumentFragment();
    const id = document.createElement("div");
    id.className = "node-id";
    id.textContent = `${meta.label} · ${n.id}`;
    id.title = n.id;
    if (n.type !== "start" && n.type !== "end") frag.append(id);

    const io = document.createElement("div");
    io.className = "node-io";
    const inputs = inputVars(n);
    const outputs = outputVars(n);
    if (n.type === "start") {
      const text = document.createElement("div");
      text.className = "node-io-label node-desc";
      text.textContent = "运行入口，初始变量由画布工具维护";
      io.append(text);
    } else if (n.type === "if") {
      const cond = document.createElement("div");
      cond.className = "node-cond";
      cond.textContent = ifSummary(n);
      io.append(section("条件", [cond], true));
    } else if (n.type === "for") {
      io.append(section("输入", varRows(inputs)));
      io.append(section("输出", varRows(outputs)));
      io.append(loopPreview(n));
    } else if (n.type === "end") {
      const text = document.createElement("div");
      text.className = "node-io-label node-desc";
      text.textContent = "流程命中后结束";
      io.append(text);
    } else {
      if (inputs.length) io.append(section("输入", varRows(inputs)));
      if (outputs.length) io.append(section("输出", varRows(outputs)));
      if (!inputs.length && !outputs.length) {
        const empty = document.createElement("div");
        empty.className = "node-io-label";
        empty.textContent = n.type === "start" ? "运行时上下文入口" : "未配置变量";
        io.append(empty);
      }
    }
    frag.append(io);
    return frag;
  }

  _syncStatusBadge(el, status) {
    let badge = el.querySelector(".status-badge");
    if (!status || status === "pending") {
      if (badge) badge.remove();
      return;
    }
    if (!badge) {
      badge = document.createElement("span");
      badge.className = "status-badge";
      el.append(badge);
    }
    badge.textContent = {
      running: "↻",
      success: "✓",
      failed: "!",
      skipped: "·",
      waiting: "等待",
    }[status] || "";
  }

  _insertNodeOnEdge(edge, type) {
    if (edge.source_handle === "error" && type !== "llm") return;
    const src = this.graph.nodes.find((n) => n.id === edge.source);
    const tgt = this.graph.nodes.find((n) => n.id === edge.target);
    if (!src || !tgt) return;
    const pos = {
      x: (src.position.x + tgt.position.x) / 2,
      y: (src.position.y + tgt.position.y) / 2 + 40,
    };
    if (this.opts.onCreateNode) {
      const node = this.opts.onCreateNode(type, pos);
      if (!node) return;
      this.graph.edges = this.graph.edges.filter((x) => x.id !== edge.id);
      this.graph.edges.push(makeEdge(src.id, node.id, edge.source_handle));
      this.graph.edges.push(makeEdge(node.id, tgt.id,
        node.type === "if" ? "if-1" : "out"));
      this.render();
      this._change();
    }
  }

  /* ---------------- 交互 ---------------- */
  _bind() {
    this.wrap.addEventListener("mousedown", (ev) => {
      if (ev.target !== this.wrap && ev.target !== this.content &&
          ev.target !== this.svg) return;
      if (ev.button !== 0) return;
      const start = { x: ev.clientX - this.pan.x, y: ev.clientY - this.pan.y };
      const move = (e2) => {
        this.pan.x = e2.clientX - start.x;
        this.pan.y = e2.clientY - start.y;
        this._applyTransform();
      };
      const up = () => {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
    this.wrap.addEventListener("click", (ev) => {
      if (ev.target === this.wrap || ev.target === this.content ||
          ev.target === this.svg) {
        this.setSelected(null);
        if (this.opts.onSelect) this.opts.onSelect(null);
      }
    });
    this.wrap.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const rect = this.wrap.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      const old = this.zoom;
      this.zoom = Math.min(2, Math.max(0.3,
        this.zoom * (ev.deltaY < 0 ? 1.1 : 0.9)));
      const k = this.zoom / old;
      this.pan.x = mx - (mx - this.pan.x) * k;
      this.pan.y = my - (my - this.pan.y) * k;
      this._applyTransform();
    }, { passive: false });

    // 连线拖拽
    this.content.addEventListener("mousedown", (ev) => {
      const port = ev.target.closest?.(".port-out");
      if (!port || this.opts.readOnly) return;
      ev.stopPropagation();
      ev.preventDefault();
      this._startConnect(port, ev);
    });
  }

  _nodeMouseDown(ev, n) {
    if (this.opts.readOnly) return;
    if (ev.button !== 0) return;
    if (ev.target.closest(".node-port")) return;
    ev.stopPropagation();
    const start = {
      x: ev.clientX, y: ev.clientY,
      nx: n.position.x, ny: n.position.y,
    };
    let moved = false;
    const move = (e2) => {
      const dx = (e2.clientX - start.x) / this.zoom;
      const dy = (e2.clientY - start.y) / this.zoom;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      n.position.x = Math.round(start.nx + dx);
      n.position.y = Math.round(start.ny + dy);
      const el = this._nodeEl(n.id);
      if (el) {
        el.style.left = `${n.position.x}px`;
        el.style.top = `${n.position.y}px`;
      }
      this._redrawEdges();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      if (moved) this._change();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  _startConnect(port, ev) {
    const srcId = port.dataset.node;
    const handle = port.dataset.handle || "out";
    const src = this.graph.nodes.find((n) => n.id === srcId);
    if (!src) return;
    const rect = this.wrap.getBoundingClientRect();
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", "edge-path edge-temp");
    this.svg.append(path);
    const startPt = this._edgePoints(src, src, handle);
    let moved = false;
    const move = (e2) => {
      if (Math.abs(e2.clientX - ev.clientX) + Math.abs(e2.clientY - ev.clientY) > 4) {
        moved = true;
      }
      const x2 = (e2.clientX - rect.left - this.pan.x) / this.zoom;
      const y2 = (e2.clientY - rect.top - this.pan.y) / this.zoom;
      path.setAttribute("d", edgePath(startPt.x1, startPt.y1, x2, y2));
    };
    const up = (e2) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      path.remove();
      if (!moved) {
        this._showAddMenu(src, e2.clientX, e2.clientY, handle);
        return;
      }
      const stack = document.elementsFromPoint(e2.clientX, e2.clientY);
      const targetPort = stack
        .map((el) => el.closest?.(".port-in"))
        .find(Boolean);
      const nodeEl = targetPort?.closest?.(".wf-node");
      if (!nodeEl) return;
      this._connectNodes(srcId, nodeEl.dataset.id, handle);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  _connectNodes(srcId, targetId, handle) {
    if (!targetId || targetId === srcId) return false;
    const target = this.graph.nodes.find((n) => n.id === targetId);
    if (!target || target.type === "start") return false;
    if (this.graph.edges.some((e) =>
      e.source === srcId && e.target === targetId &&
      e.source_handle === handle)) return false;
    if (this.opts.canConnect) {
      const allowed = this.opts.canConnect(srcId, targetId, handle);
      if (!allowed) return false;
    }
    this.graph.edges.push(makeEdge(srcId, targetId, handle));
    this.render();
    this._change();
    return true;
  }

  _showAddMenu(node, x, y, handle = "out") {
    showMenu(x, y, insertableTypes(this.opts.allowTypes, handle).map((t) => ({
      label: NODE_TYPES[t].label,
      action: () => {
        if (!this.opts.onCreateNode) return;
        const pos = { x: node.position.x + nodeWidth(node) + 90, y: node.position.y };
        const created = this.opts.onCreateNode(t, pos);
        if (!created) return;
        const edgeHandle = handle || (node.type === "if" ? "if-1" : "out");
        if (this.opts.canConnect && !this.opts.canConnect(node.id, created.id, edgeHandle)) {
          this.graph.nodes = this.graph.nodes.filter((n) => n.id !== created.id);
          return;
        }
        this.graph.edges.push(makeEdge(node.id, created.id, edgeHandle));
        this.render();
        this._change();
        this.setSelected(created.id);
        if (this.opts.onSelect) this.opts.onSelect(created.id);
      },
    })));
  }

  /* ---------------- 自动布局 ---------------- */
  autoLayout() {
    const nodes = this.graph.nodes;
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const depth = new Map();
    const starts = nodes.filter((n) =>
      !this.graph.edges.some((e) => e.target === n.id));
    const queue = [...starts.map((n) => n.id)];
    for (const s of starts) depth.set(s.id, 0);
    while (queue.length) {
      const id = queue.shift();
      for (const e of this.graph.edges.filter((x) => x.source === id)) {
        const d = (depth.get(id) || 0) + 1;
        if ((depth.get(e.target) ?? -1) < d) {
          depth.set(e.target, d);
          queue.push(e.target);
        }
      }
    }
    const layers = new Map();
    for (const n of nodes) {
      const d = depth.get(n.id) ?? 0;
      if (!layers.has(d)) layers.set(d, []);
      layers.get(d).push(n);
    }
    for (const [d, arr] of layers) {
      arr.forEach((n, i) => {
        n.position.x = 80 + d * 300;
        n.position.y = 80 + i * 150;
      });
    }
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.render();
    this._change();
  }

  _redrawEdges() {
    this.svg.querySelectorAll("g").forEach((g) => g.remove());
    this.content.querySelectorAll(".line-plus-btn").forEach((el) => el.remove());
    for (const e of this.graph.edges) this._renderEdge(e);
  }

  _applyTransform() {
    const transform = `translate(${this.pan.x}px, ${this.pan.y}px) scale(${this.zoom})`;
    this.content.style.transform = transform;
    this.svg.style.transform = transform;
    this.svg.style.transformOrigin = "0 0";
  }

  _change() {
    if (this.opts.onChange) this.opts.onChange();
  }
}

function edgePath(x1, y1, x2, y2) {
  const dx = Math.max(60, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function insertableTypes(allowTypes, handle = "out") {
  const all = ["code", "llm", "if", "for", "aggregate", "end"];
  const filtered = allowTypes ? all.filter((t) => allowTypes.includes(t)) : all;
  return handle === "error" ? filtered.filter((t) => t === "llm") : filtered;
}

function nodeWidth(node) {
  return node.type === "for" ? FOR_NODE_W : NODE_W;
}

function handleLabel(handle) {
  if (!handle || handle === "out") return "";
  if (handle === "if") return "IF 1";
  if (/^if-\d+$/.test(handle)) return `IF ${handle.slice(3)}`;
  if (handle === "else") return "ELSE";
  if (handle === "error") return "ERROR";
  if (handle === "retry") return "RETRY";
  return handle;
}

function inputVars(node) {
  const cfg = node.config || {};
  if (node.type === "start") {
    return (cfg.inputs || []).map((v) => ({
      name: v.name || "arg-1",
      type: v.type || "string",
    }));
  }
  if (node.type === "code") {
    return (cfg.inputs || []).map((v) => ({
      name: v.source || v.name || "未配置",
      type: v.type || "",
    }));
  }
  if (node.type === "if") {
    const vars = [];
    const conditions = conditionList(cfg);
    for (const c of conditions) {
      if (c.variable) vars.push({ name: c.variable, type: "" });
      if (c.value_type === "variable" && c.value) vars.push({ name: c.value, type: "" });
    }
    return vars;
  }
  if (node.type === "for") {
    return cfg.list_source ? [{ name: cfg.list_source, type: "list" }] : [];
  }
  return [];
}

function outputVars(node) {
  const cfg = node.config || {};
  if (node.type === "code") {
    return (cfg.outputs || []).map((v) => ({
      name: v.name || "result",
      type: v.type || "string",
    }));
  }
  if (node.type === "llm") {
    return [];
  }
  if (node.type === "for") {
    return [{ name: autoOutputName(node), type: "list" }];
  }
  if (node.type === "aggregate") {
    return [{ name: autoOutputName(node), type: cfg.output_type || "string" }];
  }
  return [];
}

function section(label, children, raw = false) {
  const wrap = document.createElement("div");
  wrap.className = "node-io-section";
  const title = document.createElement("div");
  title.className = "node-io-label";
  title.textContent = label;
  wrap.append(title);
  if (raw) {
    children.forEach((child) => wrap.append(child));
  } else {
    children.forEach((child) => wrap.append(child));
  }
  return wrap;
}

function varRows(vars) {
  if (!vars.length) {
    const empty = document.createElement("div");
    empty.className = "node-var-row";
    empty.append(textSpan("未配置", "node-var-name"));
    return [empty];
  }
  return vars.slice(0, 3).map((v) => {
    const row = document.createElement("div");
    row.className = "node-var-row";
    row.append(textSpan(v.name, "node-var-name"));
    if (v.type) row.append(textSpan(v.type, "type-chip"));
    return row;
  });
}

function textSpan(text, className) {
  const span = document.createElement("span");
  span.className = className;
  span.textContent = text || "";
  span.title = text || "";
  return span;
}

function ifSummary(node) {
  const cfg = node.config || {};
  const conditions = conditionList(cfg);
  if (!conditions.length || !conditions.some((c) => c.variable)) return "未配置条件";
  return conditions.map((c, index) => {
    if (!c.variable) return "未配置";
    if (c.operator === "为空" || c.operator === "不为空") {
      return `${c.variable} ${c.operator}`;
    }
    const value = c.value_type === "variable"
      ? (c.value || "比较变量")
      : JSON.stringify(c.value ?? "");
    return `IF ${index + 1}: ${c.variable} ${c.operator || "是"} ${value}`;
  }).join("\n");
}

function conditionList(cfg) {
  if (Array.isArray(cfg.conditions) && cfg.conditions.length) return cfg.conditions;
  return [{
    variable: cfg.variable || "",
    operator: cfg.operator || "是",
    value: cfg.value ?? "",
    value_type: cfg.value_type || "constant",
  }];
}

function loopPreview(node) {
  const wrap = document.createElement("div");
  wrap.className = "loop-preview";
  const inner = document.createElement("div");
  inner.className = "loop-preview-inner";
  const body = node.config?.body || {};
  const nodes = body.nodes || [];
  if (!nodes.length) {
    inner.append(textSpan("循环体未配置", "node-io-label"));
  } else {
    nodes.slice(0, 3).forEach((n) => {
      const mini = document.createElement("div");
      mini.className = "loop-mini-node";
      mini.textContent = n.name || NODE_TYPES[n.type]?.label || n.id;
      mini.title = n.id;
      inner.append(mini);
    });
  }
  wrap.append(inner);
  return wrap;
}

/* ---------------- 通用右键菜单 ---------------- */
export function showMenu(x, y, items) {
  const menu = document.getElementById("ctx-menu");
  menu.innerHTML = "";
  menu.classList.remove("hidden");
  const build = (list, container) => {
    for (const item of list) {
      const div = document.createElement("div");
      div.className = "ctx-item" + (item.danger ? " danger" : "") +
        (item.submenu ? " has-submenu" : "");
      div.textContent = item.label;
      if (item.submenu) {
        const sub = document.createElement("div");
        sub.className = "ctx-submenu";
        build(item.submenu, sub);
        div.append(sub);
      } else {
        div.addEventListener("click", () => {
          hideMenu();
          item.action();
        });
      }
      container.append(div);
    }
  };
  build(items, menu);
  menu.style.left = `${Math.min(x, window.innerWidth - 200)}px`;
  menu.style.top = `${Math.min(y, window.innerHeight - items.length * 32 - 16)}px`;
}

export function hideMenu() {
  document.getElementById("ctx-menu").classList.add("hidden");
}

document.addEventListener("click", (ev) => {
  if (!ev.target.closest("#ctx-menu")) hideMenu();
});
