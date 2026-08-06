/* fsm to skill - 应用入口：工具栏 / 页签 / 校验 / 保存 / 模态框 */
import {
  state, on, changed, getNode, makeNode, serializeWorkflow, loadWorkflow,
  newWorkflow, markSaved, isDirty, NODE_TYPES, validateInitialVariableName,
  setNodeName,
} from "./store.js";
import { api } from "./api.js?v=20260730-agent-debug";
import { el, openModal, closeModal, toast } from "./ui.js";
import { Canvas } from "./canvas.js";
import { initPanels, renderPanel } from "./panels.js?v=20260804-loop-code-debug-2";
import { initRun, syncRunPage } from "./run.js?v=20260806-shell-fix-v3";
import { saveWorkflowToFile } from "./persistence.js";

let canvas = null;
let validateTimer = null;
let paletteDragSuppressClick = 0;
const WORKFLOW_DRAFT_KEY = "dag2skill.workflowDraft.v1";

document.addEventListener("DOMContentLoaded", () => {
  restoreWorkflowDraft();
  initPanels();
  initCanvas();
  initRun();
  initToolbar();
  initTabs();
  initResizablePanel();
  loadCachedConfig();
  renderPanel(null);
  on("change", (what) => {
    canvas.render();
    applyValidationToCanvas();
    if (what === "node-name") {
      renderPanel(state.selectedNodeId ? getNode(state.selectedNodeId) : null);
    }
    updateCanvasEmptyState();
    updateSaveState();
    persistWorkflowDraft();
    scheduleValidate();
  });
  on("saved", persistWorkflowDraft);
  updateCanvasEmptyState();
  updateSaveState();
  scheduleValidate();
});

/* ---------------- 画布空状态 ---------------- */
function updateCanvasEmptyState() {
  const el = document.getElementById("canvas-empty");
  if (!el) return;
  const count = Array.isArray(state.workflow?.nodes) ? state.workflow.nodes.length : 0;
  el.classList.toggle("hidden", count > 0);
}

/* ---------------- 画布 ---------------- */
function restoreWorkflowDraft() {
  let raw = "";
  try {
    raw = localStorage.getItem(WORKFLOW_DRAFT_KEY) || "";
  } catch {
    return;
  }
  if (!raw) return;
  try {
    const draft = JSON.parse(raw);
    const workflow = draft.workflow || draft;
    if (!workflow || !Array.isArray(workflow.nodes)) return;
    loadWorkflow(workflow, draft.workflowId ?? workflow.id ?? null,
      draft.savedPath ?? null);
    if (typeof draft.savedSnapshot === "string") {
      state.savedSnapshot = draft.savedSnapshot;
    }
  } catch {
    try { localStorage.removeItem(WORKFLOW_DRAFT_KEY); } catch { /* ignore */ }
  }
}

function persistWorkflowDraft() {
  try {
    localStorage.setItem(WORKFLOW_DRAFT_KEY, JSON.stringify({
      workflow: serializeWorkflow(),
      workflowId: state.workflowId,
      savedPath: state.savedPath,
      savedSnapshot: state.savedSnapshot,
    }));
  } catch {
    /* localStorage may be unavailable in restricted file contexts. */
  }
}

function initCanvas() {
  const wrap = document.getElementById("canvas-wrap");
  canvas = new Canvas(wrap, state.workflow, {
    onSelect: (id) => {
      state.selectedNodeId = id;
      renderPanel(id ? getNode(id) : null);
    },
    onChange: () => changed(),
    showLinePlus: true,
    onCreateNode: (type, pos) => {
      return createCanvasNode(type, pos, false);
    },
    canConnect: canConnect,
    onRenameNode: (node, name) => {
      const res = setNodeName(node, name);
      if (!res.ok) {
        toast(res.message, "error");
        return false;
      }
      return true;
    },
    onNodeDblClick: (id) => {
      const n = getNode(id);
      if (n && n.type === "for") {
        state.selectedNodeId = id;
        renderPanel(n);
      }
    },
  });
  // 组件面板
  const palette = document.getElementById("palette");
  palette.querySelectorAll(".palette-item").forEach((item) => {
    const type = item.dataset.type;
    if (type !== "start") installPalettePointerDrag(item, type, wrap);
    item.addEventListener("dragstart", (ev) => {
      if (type === "start") return;
      ev.dataTransfer.setData("application/x-node-type", type);
      ev.dataTransfer.effectAllowed = "copy";
    });
    item.addEventListener("click", () => {
      if (Date.now() < paletteDragSuppressClick) return;
      if (type === "start") {
        toast("Start 节点只能存在一个，当前工作流已包含入口节点", "info");
        return;
      }
      const rect = wrap.getBoundingClientRect();
      const pos = {
        x: (rect.width / 2 - canvas.pan.x) / canvas.zoom - 88 + Math.random() * 60,
        y: (rect.height / 2 - canvas.pan.y) / canvas.zoom - 30 + Math.random() * 60,
      };
      createCanvasNode(type, pos, true);
    });
  });
  document.getElementById("btn-canvas-autolayout")?.addEventListener("click",
    () => canvas.autoLayout());
  document.getElementById("btn-canvas-add-initial-var")?.addEventListener(
    "click", openInitialVariableDialog);
  wrap.addEventListener("dragover", (ev) => {
    if (ev.dataTransfer.types.includes("application/x-node-type")) {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "copy";
    }
  });
  wrap.addEventListener("drop", (ev) => {
    const type = ev.dataTransfer.getData("application/x-node-type");
    if (!type || type === "start") return;
    ev.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const pos = {
      x: (ev.clientX - rect.left - canvas.pan.x) / canvas.zoom - 88,
      y: (ev.clientY - rect.top - canvas.pan.y) / canvas.zoom - 28,
    };
    createCanvasNode(type, pos, true);
  });
}

function installPalettePointerDrag(item, type, wrap) {
  item.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const start = { x: ev.clientX, y: ev.clientY };
    let ghost = null;
    let moved = false;

    const setGhostPos = (x, y) => {
      if (!ghost) return;
      ghost.style.left = `${x}px`;
      ghost.style.top = `${y}px`;
    };
    const isOverCanvas = (x, y) => {
      const rect = wrap.getBoundingClientRect();
      return x >= rect.left && x <= rect.right &&
        y >= rect.top && y <= rect.bottom;
    };
    const beginDrag = () => {
      if (ghost) return;
      ghost = document.createElement("div");
      ghost.className = `palette-drag-ghost ${type}`;
      ghost.textContent = item.textContent.trim();
      document.body.append(ghost);
      document.body.classList.add("is-palette-dragging");
      moved = true;
    };
    const move = (e2) => {
      const delta = Math.abs(e2.clientX - start.x) +
        Math.abs(e2.clientY - start.y);
      if (delta > 6) beginDrag();
      if (!ghost) return;
      e2.preventDefault();
      setGhostPos(e2.clientX, e2.clientY);
      wrap.classList.toggle("drop-active", isOverCanvas(e2.clientX, e2.clientY));
    };
    const up = (e2) => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      wrap.classList.remove("drop-active");
      document.body.classList.remove("is-palette-dragging");
      if (ghost) ghost.remove();
      if (!moved || !isOverCanvas(e2.clientX, e2.clientY)) return;
      paletteDragSuppressClick = Date.now() + 350;
      const rect = wrap.getBoundingClientRect();
      const pos = {
        x: (e2.clientX - rect.left - canvas.pan.x) / canvas.zoom - 88,
        y: (e2.clientY - rect.top - canvas.pan.y) / canvas.zoom - 28,
      };
      createCanvasNode(type, pos, true);
    };

    window.addEventListener("pointermove", move, { passive: false });
    window.addEventListener("pointerup", up);
  });
}

function createCanvasNode(type, pos, select) {
  const node = makeNode(type, freeNodePosition(type, pos));
  state.workflow.nodes.push(node);
  changed();
  if (select) {
    canvas.setSelected(node.id);
    state.selectedNodeId = node.id;
    renderPanel(node);
  }
  return node;
}

function freeNodePosition(type, pos) {
  const width = type === "for" ? 240 : 176;
  const height = type === "for" ? 160 : 120;
  const gap = 28;
  const base = {
    x: Math.round(pos.x),
    y: Math.round(pos.y),
  };
  const occupied = state.workflow.nodes.map((node) => ({
    x: node.position?.x || 0,
    y: node.position?.y || 0,
    w: node.type === "for" ? 240 : 176,
    h: node.type === "for" ? 160 : 120,
  }));
  const overlaps = (candidate) => occupied.some((rect) =>
    candidate.x < rect.x + rect.w + gap &&
    candidate.x + width + gap > rect.x &&
    candidate.y < rect.y + rect.h + gap &&
    candidate.y + height + gap > rect.y);
  if (!overlaps(base)) return base;
  for (let ring = 1; ring <= 7; ring += 1) {
    const stepX = width + gap;
    const stepY = height + gap;
    const offsets = [
      [ring * stepX, 0],
      [ring * stepX, ring * stepY],
      [0, ring * stepY],
      [-ring * stepX, ring * stepY],
      [ring * stepX, -ring * stepY],
    ];
    for (const [dx, dy] of offsets) {
      const candidate = {
        x: Math.max(20, base.x + dx),
        y: Math.max(20, base.y + dy),
      };
      if (!overlaps(candidate)) return candidate;
    }
  }
  return base;
}

function canConnect(sourceId, targetId, handle) {
  const source = getNode(sourceId);
  const target = getNode(targetId);
  if (!source || !target) return false;
  if (source.type === "if" &&
      (handle === "else" || String(handle || "").startsWith("if"))) {
    const exists = state.workflow.edges.some((e) =>
      e.source === sourceId && e.source_handle === handle && e.target !== targetId);
    if (exists) {
      toast(`${handle === "else" ? "ELSE" : handle.toUpperCase()} 出口只能连接一个节点`, "error");
      return false;
    }
  }
  if (handle === "error") {
    if (source.type !== "code" || !source.config?.error_branch) {
      toast("只有启用错误分支的 Code 节点才能连接 error 出边", "error");
      return false;
    }
    if (target.type !== "llm") {
      toast("Code error 出边只能连接 Prompt 节点", "error");
      return false;
    }
    const exists = state.workflow.edges.some((e) =>
      e.source === sourceId && e.source_handle === "error" && e.target !== targetId);
    if (exists) {
      toast("Code error 出边只能连接一个 Prompt 节点", "error");
      return false;
    }
  }
  return true;
}

/* ---------------- 工具栏 ---------------- */
function initToolbar() {
  const nameInput = document.getElementById("wf-name");
  nameInput.value = state.workflow.name;
  nameInput.addEventListener("input", () => {
    state.workflow.name = nameInput.value;
    changed();
  });
  on("loaded", () => {
    nameInput.value = state.workflow.name;
    canvas.setGraph(state.workflow);
    renderPanel(null);
    updateSaveState();
    persistWorkflowDraft();
    scheduleValidate();
  });

  document.getElementById("btn-new").addEventListener("click", () => {
    if (isDirty() && !confirm("当前工作流有未保存修改，确定新建？")) return;
    newWorkflow();
  });
  document.getElementById("btn-validate").addEventListener("click",
    showValidationReport);
  document.getElementById("canvas-error-pill")?.addEventListener("click",
    showValidationReport);
  document.getElementById("btn-run-top")?.addEventListener("click", () => {
    document.querySelector('.tab[data-tab="run"]').click();
  });
  document.getElementById("btn-save").addEventListener("click", saveWorkflow);
  document.getElementById("btn-open").addEventListener("click", openWorkflowFromFile);
  document.getElementById("btn-export-json")?.addEventListener("click", exportJSON);
  document.getElementById("btn-import")?.addEventListener("click",
    () => document.getElementById("file-import")?.click());
  document.getElementById("file-import")?.addEventListener("change", importJSON);
  document.getElementById("btn-export-skill")
    .addEventListener("click", openExportSkill);
  document.getElementById("btn-config").addEventListener("click", openConfig);
  document.getElementById("btn-docs").addEventListener("click", openDocs);
  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal").addEventListener("mousedown", (ev) => {
    if (ev.target.id === "modal") closeModal();
  });
}

/* ---------------- 页签 ---------------- */
function initTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) =>
        t.classList.toggle("active", t === tab));
      const page = tab.dataset.tab;
      document.getElementById("page-edit").classList.toggle("hidden", page !== "edit");
      document.getElementById("page-run").classList.toggle("hidden", page !== "run");
      if (page === "run") syncRunPage();
    });
  });
}

/* ---------------- 校验 ---------------- */
function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(runValidate, 700);
}

async function runValidate() {
  const badge = document.getElementById("btn-validate");
  const label = badge.querySelector(".tb-label") || badge;
  try {
    const rep = await api.validateWorkflow(serializeWorkflow());
    state.validation = rep;
    const errs = (rep.errors || []).length;
    const warns = (rep.warnings || []).length;
    badge.classList.toggle("has-error", errs > 0);
    badge.classList.toggle("has-warn", !errs && warns > 0);
    label.textContent = errs ? `${errs} 错误` : (warns ? `${warns} 警告` : "校验通过");
    applyValidationToCanvas();
    syncCanvasErrorPill(errs, warns);
  } catch {
    label.textContent = "校验";
  }
}

function applyValidationToCanvas() {
  const errors = state.validation?.errors || [];
  const errorNodeIds = new Set(errors.map((e) => e.node_id).filter(Boolean));
  document.querySelectorAll("#canvas-wrap .wf-node").forEach((el) => {
    el.classList.toggle("has-error", errorNodeIds.has(el.dataset.id));
  });
}

function syncCanvasErrorPill(errs, warns) {
  const pill = document.getElementById("canvas-error-pill");
  if (!pill) return;
  pill.classList.toggle("hidden", !errs && !warns);
  pill.textContent = errs ? `检查项(${errs})` : `警告(${warns})`;
}

function showValidationReport() {
  const { errors = [], warnings = [] } = state.validation || {};
  const body = el("div", { class: "val-report" });
  if (!errors.length && !warnings.length) {
    body.append(el("div", { class: "placeholder", text: "✓ 未发现结构或配置问题" }));
  }
  const addItems = (items, kind) => {
    for (const it of items) {
      const row = el("div", { class: `val-item ${kind}` },
        el("span", { class: "val-code mono", text: it.code }),
        el("span", { class: "val-msg", text: it.message }),
        it.node_id ? el("button", {
          class: "btn small", text: "定位",
          onclick: () => {
            closeModal();
            document.querySelector('.tab[data-tab="edit"]').click();
            canvas.focusNode(it.node_id);
            state.selectedNodeId = it.node_id;
            renderPanel(getNode(it.node_id));
          },
        }) : null);
      body.append(row);
    }
  };
  addItems(errors, "error");
  addItems(warnings, "warn");
  openModal({ title: "校验报告", body });
}

/* ---------------- 保存 / 打开 ---------------- */
async function saveWorkflow() {
  const btn = document.getElementById("btn-save");
  btn.disabled = true;
  try {
    await saveWorkflowToFile({ forceDialog: !state.savedPath });
  } catch (err) {
    toast(`保存失败：${err.data?.detail?.message || err.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

function updateSaveState() {
  const label = document.querySelector("#btn-save .tb-label") ||
    document.getElementById("btn-save");
  label.textContent = "保存";
}

async function saveWorkflowNative() {
  return null;
}

async function openWorkflowFromFile() {
  try {
    const resp = await api.openWorkflowFile();
    if (resp?.cancelled) return;
    if (!resp?.workflow) {
      toast("未读取到有效工作流", "error");
      return;
    }
    loadWorkflow(resp.workflow, resp.workflow.id || null, resp.path || null);
    toast(`已打开：${resp.path || resp.workflow.name || "工作流"}`, "success");
  } catch (err) {
    toast(`打开失败：${err.data?.detail?.message || err.message}`, "error");
  }
}

function fmtTime(t) {
  if (!t) return "-";
  try { return new Date(t).toLocaleString(); } catch { return t; }
}

function safeFilename(name) {
  return String(name).replace(/[\\/:*?"<>|]+/g, "_").slice(0, 80) || "workflow";
}

/* ---------------- 导入 / 导出 JSON ---------------- */
function exportJSON() {
  const blob = new Blob([JSON.stringify(serializeWorkflow(), null, 2)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.workflow.name || "workflow"}.json`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function importJSON(ev) {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  importWorkflowFile(file);
}

function importWorkflowFile(file) {
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const data = JSON.parse(reader.result);
      if (!Array.isArray(data.nodes)) throw new Error("缺少 nodes 字段");
      loadWorkflow(data, null);
      toast("导入成功（尚未保存）", "success");
    } catch (err) {
      toast(`导入失败：${err.message}`, "error");
    }
  };
  reader.readAsText(file);
}

function openInitialVariableDialog() {
  const start = state.workflow.nodes.find((n) => n.type === "start");
  if (!start) {
    toast("工作流缺少 Start 节点", "error");
    return;
  }
  const name = el("input", {
    class: "input mono", placeholder: "如 arg-1",
    value: nextInitialVarName(start),
  });
  const type = el("select", { class: "input" });
  for (const t of ["string", "int", "float", "list", "dict"]) {
    type.append(el("option", { value: t, text: t }));
  }
  const defaultValue = el("input", {
    class: "input mono", placeholder: "运行页默认填入，可选",
  });
  const msg = el("div", { class: "field-hint" });
  openModal({
    title: "新增初始变量",
    body: el("div", {},
      fieldRow("变量名", name),
      fieldRow("类型", type),
      fieldRow("默认赋值", defaultValue),
      msg),
    footer: [
      { label: "取消", action: (close) => close() },
      {
        label: "添加", kind: "primary",
        action: (close) => {
          const valid = validateInitialVariableName(name.value);
          if (!valid.ok) {
            msg.textContent = valid.message;
            msg.classList.add("error-text");
            return;
          }
          start.config.inputs = start.config.inputs || [];
          start.config.inputs.push({
            name: valid.name,
            type: type.value,
            default: defaultValue.value,
          });
          changed();
          toast(`已新增初始变量：${valid.name}`, "success");
          close();
        },
      },
    ],
  });
}

function nextInitialVarName(start) {
  const used = new Set((start.config.inputs || []).map((v) => v.name));
  let i = 1;
  while (used.has(`arg-${i}`)) i += 1;
  return `arg-${i}`;
}

/* ---------------- 导出 SKILL ---------------- */
function openExportSkill() {
  api.exportSkill(serializeWorkflow(), "", false)
    .then((resp) => {
      if (resp?.cancelled) return;
      toast(`SKILL 导出成功：${resp.path || "已完成"}`, "success");
    })
    .catch((err) => {
      toast(`导出失败：${err.data?.detail?.message || err.message}`, "error");
    });
}

/* ---------------- 配置 ---------------- */
async function openConfig() {
  let cfg;
  try {
    cfg = await api.getConfig();
    state.config = cfg;
  } catch (err) {
    toast(`读取配置失败：${err.message}`, "error");
    return;
  }
  cfg.providers = cfg.providers || {};
  const providerKeys = ["openai", "compatible", "anthropic"];
  const body = el("div", { class: "config-form" });
  const providerInputs = {};
  const testResult = el("div", { class: "export-result" });
  const section = (title, ...children) => el("section", { class: "config-section" },
    el("div", { class: "section-title", text: title }),
    ...children);
  const kv = (label, control, hint) => fieldRow(label, control);
  const modelGrid = el("div", { class: "config-grid" });
  for (const key of providerKeys) {
    const p = cfg.providers[key] || {};
    const base = el("input", {
      class: "input mono", placeholder: "base_url", value: p.base_url || "",
    });
    const keyIn = el("input", {
      class: "input mono", placeholder: "api_key", value: p.api_key || "",
      type: "password",
    });
    providerInputs[key] = { base, key: keyIn };
    modelGrid.append(el("div", { class: "config-provider" },
      el("div", { class: "section-subtitle", text: key }),
      el("div", { class: "kv-row" }, base),
      el("div", { class: "kv-row" }, keyIn)));
  }
  const defProv = el("select", { class: "input" });
  for (const k of providerKeys) defProv.append(el("option", { value: k, text: k }));
  defProv.value = providerKeys.includes(cfg.default_provider)
    ? cfg.default_provider : "openai";
  const defModel = el("input", {
    class: "input mono", value: cfg.default_model || "", placeholder: "默认模型",
  });
  const stream = el("input", { type: "checkbox", checked: cfg.stream !== false });
  modelGrid.append(
    fieldRow("默认服务商", defProv),
    fieldRow("默认模型", defModel),
    el("label", { class: "switch" }, stream, el("span", { text: " 流式输出" })),
  );

  const timeoutGrid = el("div", { class: "config-grid" });
  const timeout = el("input", {
    class: "input", type: "number", value: String(cfg.timeout_seconds ?? 60),
  });
  const retries = el("input", {
    class: "input", type: "number", value: String(cfg.max_retries ?? 2),
  });
  const idleTimeout = el("input", {
    class: "input", type: "number", value: String(cfg.idle_timeout ?? 600),
  });
  const maxTaskRuntime = el("input", {
    class: "input", type: "number", value: String(cfg.max_task_runtime ?? 3600),
  });
  timeoutGrid.append(
    fieldRow("请求超时（秒）", timeout),
    fieldRow("最大重试", retries),
    fieldRow("idle_timeout（秒）", idleTimeout),
    fieldRow("max_task_runtime（秒）", maxTaskRuntime),
  );

  const shellCfg = cfg.shell_tool || {};
  const shellEnable = el("input", {
    type: "checkbox", checked: !!shellCfg.enabled,
  });
  const shellMode = el("select", { class: "input" });
  for (const s of ["auto", "powershell", "pwsh", "bash", "sh"]) {
    shellMode.append(el("option", { value: s, text: s }));
  }
  shellMode.value = ["auto", "powershell", "pwsh", "bash", "sh"].includes(shellCfg.shell)
    ? shellCfg.shell
    : "auto";
  const shellTimeout = el("input", {
    class: "input", type: "number", value: String(shellCfg.timeout_seconds ?? 60),
  });
  const shellCalls = el("input", {
    class: "input", type: "number", value: String(shellCfg.max_calls ?? 100),
  });
  const envGrid = el("div", { class: "config-grid" });
  const pyPath = el("input", {
    class: "input mono", value: cfg.python_path || "",
    placeholder: "留空则使用当前 Python",
  });
  envGrid.append(
    fieldRow("Python 路径", pyPath),
    fieldRow("Shell 模式", shellMode),
    fieldRow("Shell 超时（秒）", shellTimeout),
    fieldRow("Shell 最大调用", shellCalls),
    el("label", { class: "switch" }, shellEnable, el("span", { text: " 启用 shell tool" })),
  );

  body.append(
    section("模型配置", modelGrid),
    section("超时配置", timeoutGrid),
    section("环境配置", envGrid),
    el("div", { class: "field-hint",
      text: "api_key 显示为 ****** 时表示保留已保存的值，不会覆盖。" }),
    testResult,
  );

  const buildPayload = () => {
    const payload = {
      providers: {},
      default_provider: defProv.value,
      default_model: defModel.value.trim(),
      timeout_seconds: parseInt(timeout.value, 10) || 60,
      max_retries: parseInt(retries.value, 10) || 0,
      stream: stream.checked,
      idle_timeout: parseInt(idleTimeout.value, 10) || 600,
      max_task_runtime: parseInt(maxTaskRuntime.value, 10) || 3600,
      python_path: pyPath.value.trim(),
      shell_tool: {
        enabled: shellEnable.checked,
        shell: shellMode.value,
        timeout_seconds: parseInt(shellTimeout.value, 10) || 60,
        max_calls: parseInt(shellCalls.value, 10) || 100,
      },
    };
    for (const [k, ins] of Object.entries(providerInputs)) {
      payload.providers[k] = {
        base_url: ins.base.value.trim(),
        api_key: ins.key.value.trim(),
      };
    }
    return payload;
  };

  openModal({
    title: "系统配置", body, wide: true,
    footer: [
      { label: "取消", action: (close) => close() },
      {
        label: "测试模型接口",
        action: async () => {
          testResult.innerHTML = "测试中…";
          try {
            const resp = await api.testConfig(buildPayload());
            testResult.innerHTML = "";
            testResult.append(el("div", { class: "debug-ok",
              text: `✓ 模型接口可用：${resp.provider} / ${resp.model}` }));
          } catch (err) {
            testResult.innerHTML = "";
            testResult.append(el("div", { class: "debug-error",
              text: err.data?.detail?.message || err.data?.detail || err.message }));
          }
        },
      },
      {
        label: "保存", kind: "primary",
        action: async (close) => {
          const payload = buildPayload();
          try {
            await api.saveConfig(payload);
            state.config = payload;
            toast("配置已保存", "success");
            close();
          } catch (err) {
            toast(`保存失败：${err.data?.detail?.message || err.message}`, "error");
          }
        },
      },
    ],
  });
}

async function loadCachedConfig() {
  try {
    state.config = await api.getConfig();
  } catch {
    state.config = null;
  }
}

function initResizablePanel() {
  const panel = document.getElementById("config-panel");
  const resizer = document.getElementById("config-resizer");
  const runPanel = document.getElementById("run-side");
  const runResizer = document.getElementById("run-resizer");
  if (panel && resizer) {
    resizer.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = panel.getBoundingClientRect().width;
      resizer.setPointerCapture(ev.pointerId);
      const move = (e2) => {
        const next = Math.max(320, Math.min(760, startW + (startX - e2.clientX)));
        panel.style.flexBasis = `${next}px`;
        panel.style.width = `${next}px`;
      };
      const up = () => {
        resizer.removeEventListener("pointermove", move);
        resizer.removeEventListener("pointerup", up);
      };
      resizer.addEventListener("pointermove", move);
      resizer.addEventListener("pointerup", up);
    });
  }
  if (runPanel && runResizer) {
    runResizer.addEventListener("pointerdown", (ev) => {
      ev.preventDefault();
      const startX = ev.clientX;
      const startW = runPanel.getBoundingClientRect().width;
      runResizer.setPointerCapture(ev.pointerId);
      const move = (e2) => {
        const max = Math.max(520, Math.min(1120, window.innerWidth - 360));
        const next = Math.max(420, Math.min(max, startW + (startX - e2.clientX)));
        runPanel.style.flexBasis = `${next}px`;
        runPanel.style.width = `${next}px`;
      };
      const up = () => {
        runResizer.removeEventListener("pointermove", move);
        runResizer.removeEventListener("pointerup", up);
      };
      runResizer.addEventListener("pointermove", move);
      runResizer.addEventListener("pointerup", up);
    });
  }
}

function fieldRow(label, control) {
  return el("div", { class: "field" },
    el("label", { class: "field-label", text: label }), control);
}

/* ---------------- 文档 ---------------- */
function openDocs() {
  openModal({
    title: "使用文档",
    wide: true,
    body: el("iframe", { class: "docs-frame", src: "/docs.html" }),
  });
}
