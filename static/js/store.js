/* fsm to skill - 前端状态管理（轻量 pub/sub） */

export const NODE_TYPES = {
  start:     { label: "开始",   color: "#16a34a" },
  code:      { label: "Code",   color: "#1d4ed8" },
  llm:       { label: "Prompt", color: "#6d28d9" },
  if:        { label: "IF",     color: "#d97706" },
  for:       { label: "For",    color: "#0f766e" },
  aggregate: { label: "聚合",   color: "#be185d" },
  end:       { label: "结束",   color: "#475569" },
};

export const VAR_TYPES = ["string", "int", "float", "list", "dict"];

export const IF_OPERATORS =
  ["包含", "不包含", "开始是", "结束是", "是", "不是", "为空", "不为空"];

const RESERVED_VAR_NAMES = new Set([
  "index", "item", "len", "total", "true", "false", "none", "True", "False", "None",
  "and", "or", "not", "if", "elif", "else", "for", "while", "in",
  "def", "return", "class", "import", "from", "as", "with", "lambda",
  "is", "pass", "break", "continue", "raise", "try", "except",
]);

export const SYSTEM_VARIABLES = [
  { name: "task-id", type: "string", from: "system" },
];

const listeners = new Map();
let idCounters = {};

const DEFAULT_NODE_NAME_BASE = {
  start: "Start",
  code: "Code",
  llm: "Prompt",
  if: "IF",
  for: "For",
  aggregate: "Aggregate",
  end: "End",
};

export const state = {
  workflow: emptyWorkflow(),
  workflowId: null,          // 服务端保存后的 id
  savedPath: null,            // 本机保存路径；再次保存时直接覆盖
  savedSnapshot: "",         // 上次保存时的快照（脏检查）
  selectedNodeId: null,
  validation: { errors: [], warnings: [] },
  lastResult: null,          // 最近一次运行的完整结果
  nodeStatuses: {},          // node_id -> running/success/failed/skipped/waiting
  running: false,
  config: null,
};

export function emptyWorkflow() {
  return {
    name: "未命名工作流",
    description: "",
    nodes: [{
      id: "start-1",
      type: "start",
      name: "Start-1",
      position: { x: 80, y: 200 },
      config: defaultConfig("start"),
    }],
    edges: [],
  };
}

export function makeNode(type, pos) {
  idCounters[type] = (idCounters[type] || 0) + 1;
  let id = `${type}-${idCounters[type]}`;
  const exists = new Set(state.workflow.nodes.map((n) => n.id));
  while (exists.has(id)) {
    idCounters[type] += 1;
    id = `${type}-${idCounters[type]}`;
  }
  return {
    id,
    type,
    name: nextNodeName(type),
    position: { x: Math.round(pos.x), y: Math.round(pos.y) },
    config: withAgentInterface(id, type, defaultConfig(type)),
  };
}

export function defaultConfig(type) {
  switch (type) {
    case "start": return {
      inputs: [],
    };
    case "code": return {
      inputs: [{
        name: "arg-1",
        description: "参数描述",
        type: "string",
        required: true,
      }],
      code: DEFAULT_CODE,
      outputs: [{ name: "result", type: "string" }],
      timeout: 30, error_branch: false,
    };
    case "llm": return { prompt: "" };
    case "if": return {
      conditions: [{ variable: "", operator: "是", value: "", value_type: "constant" }],
      branch_mode: "multi",
    };
    case "for": return { list_source: "", body: { nodes: [], edges: [] } };
    case "aggregate": return {
      output_type: "string", inputs: [], input_mode: "explicit",
    };
    case "end": return {};
    default: return {};
  }
}

const DEFAULT_CODE =
`def main(params):
    return {"result": params["arg-1"]}
`;

function nextNodeName(type) {
  const base = DEFAULT_NODE_NAME_BASE[type] || NODE_TYPES[type]?.label || type;
  const used = new Set((state?.workflow?.nodes || [])
    .map((n) => (n.name || "").trim()).filter(Boolean));
  let i = idCounters[type] || 1;
  let name = `${base}-${i}`;
  while (used.has(name)) {
    i += 1;
    name = `${base}-${i}`;
  }
  return name;
}

function withAgentInterface(id, type, config) {
  const cfg = { ...(config || {}) };
  if (type === "code") {
    cfg.agent_interface = {
      role: "agent_entry",
      entry_id: id,
      input_target: id,
      description: "Agent 输入进入此 Code 节点",
    };
  } else if (type === "llm") {
    cfg.agent_interface = {
      role: "agent_exit",
      exit_id: id,
      description: "Prompt 节点输出返回 Agent",
    };
  }
  return cfg;
}

function normalizeIfConfig(cfg) {
  if (!Array.isArray(cfg.conditions) || !cfg.conditions.length) {
    cfg.conditions = [{
      variable: cfg.variable || "",
      operator: cfg.operator || "是",
      value: cfg.value ?? "",
      value_type: cfg.value_type || "constant",
    }];
  }
  cfg.branch_mode = "multi";
  delete cfg.combinator;
}

function normalizeForBody(cfg) {
  if (!cfg.body || typeof cfg.body !== "object") {
    cfg.body = { nodes: [], edges: [] };
  }
  if (!Array.isArray(cfg.body.nodes)) cfg.body.nodes = [];
  if (!Array.isArray(cfg.body.edges)) cfg.body.edges = [];
  const byId = new Map(cfg.body.nodes.map((node) => [node.id, node]));
  for (const node of cfg.body.nodes) {
    node.config = node.config || {};
    if (node.type === "if") normalizeIfConfig(node.config);
    if (node.type === "aggregate" && !Array.isArray(node.config.inputs)) {
      node.config.inputs = [];
      node.config.input_mode = "legacy";
    }
  }
  for (const edge of cfg.body.edges) {
    if (edge.source_handle !== "if") continue;
    if (byId.get(edge.source)?.type === "if") edge.source_handle = "if-1";
  }
}

function normalizeConfig(node) {
  const rawConfig = node.config || {};
  node.config = { ...defaultConfig(node.type), ...rawConfig };
  if (node.type === "start") {
    normalizeStartInputs(node.config);
  }
  if (node.type === "code") {
    normalizeCodeInputs(node.config);
    syncCodeOutputsFromReturn(node.config);
  }
  if (node.type === "if") {
    normalizeIfConfig(node.config);
  }
  if (node.type === "for") {
    normalizeForBody(node.config);
  }
  if (node.type === "aggregate" && !Array.isArray(rawConfig.inputs)) {
    node.config.inputs = [];
    node.config.input_mode = "legacy";
  }
  node.config = withAgentInterface(node.id, node.type, node.config);
  return node;
}

export function normalizeStartInputs(config) {
  if (!Array.isArray(config.inputs)) config.inputs = [];
  config.inputs = config.inputs.map((raw, i) => {
    const spec = raw || {};
    return {
      name: spec.name || `arg-${i + 1}`,
      type: spec.type || "string",
      default: spec.default ?? spec.value ?? "",
    };
  });
  return config.inputs;
}

export function normalizeCodeInputs(config, { stripSource = false } = {}) {
  if (!Array.isArray(config.inputs)) config.inputs = [];
  config.inputs = config.inputs.map((raw, i) => {
    const spec = raw && typeof raw === "object" ? raw : {};
    const source = !stripSource && spec.source ? spec.source : null;
    const next = {
      name: spec.name || `arg-${i + 1}`,
      description: spec.description || "",
      type: spec.type || "string",
      required: spec.required !== false,
    };
    if (source) next.source = source;
    for (const key of Object.keys(spec)) delete spec[key];
    Object.assign(spec, next);
    return spec;
  });
  return config.inputs;
}

export function makeEdge(source, target, sourceHandle = "out") {
  return {
    id: `e-${source}-${sourceHandle}-${target}-${Date.now() % 100000}`,
    source, target, source_handle: sourceHandle,
  };
}

/* ---------------- pub/sub ---------------- */
export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, []);
  listeners.get(event).push(fn);
}
export function emit(event, payload) {
  (listeners.get(event) || []).forEach((fn) => fn(payload));
}
export function changed(what = "workflow") {
  emit("change", what);
}

/* ---------------- 工作流操作 ---------------- */
export function snapshot() {
  return JSON.stringify(serializeWorkflow());
}

export function serializeWorkflow() {
  const wf = state.workflow;
  const out = {
    name: wf.name,
    description: wf.description || "",
    nodes: wf.nodes,
    edges: wf.edges,
  };
  if (state.workflowId) out.id = state.workflowId;
  return out;
}

export function loadWorkflow(data, workflowId = null, savedPath = null) {
  idCounters = {};
  const nodes = (data.nodes || []).map((n) => ({
    id: n.id,
    type: n.type,
    name: loadedNodeName(n),
    position: {
      x: n.position?.x ?? 100,
      y: n.position?.y ?? 100,
    },
    config: n.config || {},
  })).map(normalizeConfig);
  for (const n of nodes) {
    const m = /^([a-z]+)-(\d+)$/.exec(n.id);
    if (m) {
      idCounters[m[1]] = Math.max(idCounters[m[1]] || 0, parseInt(m[2], 10));
    }
  }
  state.workflow = {
    name: data.name || "未命名工作流",
    description: data.description || "",
    nodes,
    edges: (data.edges || []).map((e) => {
      const source = nodes.find((node) => node.id === e.source);
      const handle = source?.type === "if" && e.source_handle === "if"
        ? "if-1" : (e.source_handle || "out");
      return {
        id: e.id || makeEdge(e.source, e.target, handle).id,
        source: e.source,
        target: e.target,
        source_handle: handle,
      };
    }),
  };
  state.workflowId = workflowId ?? data.id ?? null;
  state.savedPath = savedPath;
  state.selectedNodeId = null;
  state.lastResult = null;
  state.nodeStatuses = {};
  state.validation = { errors: [], warnings: [] };
  state.savedSnapshot = snapshot();
  changed();
  emit("loaded");
}

export function newWorkflow() {
  loadWorkflow(emptyWorkflow(), null);
}

export function isDirty() {
  return snapshot() !== state.savedSnapshot;
}

export function markSaved(workflowId, savedPath = undefined) {
  if (workflowId) state.workflowId = workflowId;
  if (savedPath !== undefined) state.savedPath = savedPath;
  state.savedSnapshot = snapshot();
  emit("saved");
}

function loadedNodeName(node) {
  const named = normalizeNodeName(node.name || "");
  if (named) return named;
  const m = /^([a-z]+)-(\d+)$/.exec(node.id || "");
  if (m) {
    const base = DEFAULT_NODE_NAME_BASE[m[1]] || m[1];
    return `${base}-${m[2]}`;
  }
  return normalizeNodeName(
    (NODE_TYPES[node.type] || {}).label || node.type || node.id || "node");
}

export function getNode(id) {
  return state.workflow.nodes.find((n) => n.id === id) || null;
}

export function removeNode(id) {
  const wf = state.workflow;
  wf.nodes = wf.nodes.filter((n) => n.id !== id);
  wf.edges = wf.edges.filter((e) => e.source !== id && e.target !== id);
  if (state.selectedNodeId === id) state.selectedNodeId = null;
  changed();
}

export function isNodeNameTaken(name, excludeId = null) {
  const value = (name || "").trim();
  if (!value) return false;
  return state.workflow.nodes.some((n) =>
    n.id !== excludeId && (n.name || "").trim() === value);
}

export function setNodeName(node, name) {
  return setGraphNodeName(state.workflow, node, name);
}

export function setGraphNodeName(graph, node, name, { relatedConfigs = [] } = {}) {
  const value = normalizeNodeName(name);
  if (!value) return { ok: false, message: "节点名称不能为空" };
  const taken = (graph?.nodes || []).some((n) =>
    n.id !== node.id && (n.name || "").trim() === value);
  if (taken) {
    return { ok: false, message: `节点名称「${value}」已存在` };
  }
  const previousName = node.name || node.id;
  const variableRenames = derivedVariableRenames(node, value);
  node.name = value;
  renameGraphVariableReferences(graph, variableRenames);
  for (const config of relatedConfigs) {
    renameConfigVariableReferences(config, variableRenames);
  }
  renameGeneratedErrorPrompt(graph, node, previousName, value);
  changed("node-name");
  return { ok: true, variableRenames };
}

export function normalizeNodeName(name) {
  return String(name || "").trim().replace(/\s+/g, "-");
}

export function nodeVariableBase(node) {
  const raw = normalizeNodeName(node?.name || node?.id || "node");
  let value = raw
    .replace(/[^0-9A-Za-z_-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (!/^[A-Za-z_]/.test(value)) {
    value = String(node?.id || "node")
      .replace(/[^0-9A-Za-z_-]/g, "-")
      .replace(/-+/g, "-")
      .replace(/^-+|-+$/g, "");
  }
  if (!/^[A-Za-z_]/.test(value)) value = `node-${value || "output"}`;
  return value;
}

export function autoOutputName(node) {
  return `${nodeVariableBase(node)}-output`;
}

function derivedVariableRenames(node, nextName) {
  const renames = new Map();
  const previous = { ...node };
  const next = { ...node, name: nextName };
  if (node.type === "for" || node.type === "aggregate") {
    addVariableRename(renames, autoOutputName(previous), autoOutputName(next));
  } else if (node.type === "code" && node.config?.error_branch) {
    const oldBase = nodeVariableBase(previous);
    const newBase = nodeVariableBase(next);
    addVariableRename(renames, `${oldBase}-error-type`, `${newBase}-error-type`);
    addVariableRename(renames, `${oldBase}-error-message`, `${newBase}-error-message`);
  }
  return renames;
}

function addVariableRename(renames, previous, next) {
  if (previous && next && previous !== next) renames.set(previous, next);
}

function renameGraphVariableReferences(graph, renames) {
  if (!renames.size) return;
  for (const item of graph?.nodes || []) {
    renameConfigVariableReferences(item.config || {}, renames);
  }
}

function renameConfigVariableReferences(config, renames) {
  if (!config || !renames.size) return;
  renameExactReference(config, "list_source", renames);
  renameExactReference(config, "collect", renames);
  renameExactReference(config, "variable", renames);
  if (config.value_type === "variable") {
    renameExactReference(config, "value", renames);
  }
  for (const spec of config.inputs || []) {
    renameExactReference(spec, "source", renames);
  }
  for (const condition of config.conditions || []) {
    renameExactReference(condition, "variable", renames);
    if (condition.value_type === "variable") {
      renameExactReference(condition, "value", renames);
    }
  }
  if (typeof config.prompt === "string") {
    config.prompt = renameTemplateReferences(config.prompt, renames);
  }
  if (config.body && typeof config.body === "object") {
    renameGraphVariableReferences(config.body, renames);
  }
}

function renameExactReference(owner, key, renames) {
  if (!owner || typeof owner[key] !== "string") return;
  const next = renames.get(owner[key]);
  if (next) owner[key] = next;
}

function renameTemplateReferences(template, renames) {
  return template.replace(/{{([^}]*)}}/g, (full, inner) => {
    const next = renames.get(inner.trim());
    if (!next) return full;
    const leading = inner.match(/^\s*/)?.[0] || "";
    const trailing = inner.match(/\s*$/)?.[0] || "";
    return `{{${leading}${next}${trailing}}}`;
  });
}

function renameGeneratedErrorPrompt(graph, node, previousName, nextName) {
  if (node.type !== "code" || !node.config?.error_branch) return;
  const previousLine = `Code 节点「${previousName}」执行失败。`;
  const nextLine = `Code 节点「${nextName}」执行失败。`;
  for (const edge of graph?.edges || []) {
    if (edge.source !== node.id || edge.source_handle !== "error") continue;
    const target = (graph.nodes || []).find((item) => item.id === edge.target);
    const prompt = target?.config?.prompt;
    if (target?.type === "llm" && typeof prompt === "string" &&
        prompt.startsWith(previousLine)) {
      target.config.prompt = nextLine + prompt.slice(previousLine.length);
    }
  }
}

export function addEdge(edge) {
  const dup = state.workflow.edges.some((e) =>
    e.source === edge.source && e.target === edge.target &&
    e.source_handle === edge.source_handle);
  if (dup || edge.source === edge.target) return false;
  state.workflow.edges.push(edge);
  changed();
  return true;
}

export function removeEdge(edgeId) {
  state.workflow.edges = state.workflow.edges.filter((e) => e.id !== edgeId);
  changed();
}

/** 节点可用的输出 handle 列表 */
export function nodeOutHandles(node) {
  switch (node.type) {
    case "code": {
      const handles = ["out"];
      if (node.config.error_branch) handles.push("error");
      return handles;
    }
    case "if": {
      const count = Math.max(1, (node.config?.conditions || []).length);
      return Array.from({ length: count }, (_, index) => `if-${index + 1}`)
        .concat("else");
    }
    case "start": case "llm": case "for": case "aggregate":
      return ["out"];
    default: return [];
  }
}

export function safeNodePrefix(nodeId) {
  return String(nodeId || "").replace(/[^0-9A-Za-z-]/g, "-");
}

export function isValidVarName(name) {
  return /^[A-Za-z_][A-Za-z0-9_-]*$/.test(name || "") &&
    !RESERVED_VAR_NAMES.has(name);
}

export function automaticVariableNames() {
  const names = new Set(SYSTEM_VARIABLES.map((v) => v.name));
  for (const n of state.workflow.nodes) {
    const cfg = n.config || {};
    if (n.type === "code") {
      for (const out of cfg.outputs || []) {
        if (out.name) names.add(out.name);
      }
      if (cfg.error_branch) {
        const prefix = nodeVariableBase(n);
        names.add(`${prefix}-error-type`);
        names.add(`${prefix}-error-message`);
      }
    } else if (n.type === "for" || n.type === "aggregate") {
      names.add(autoOutputName(n));
    }
  }
  return names;
}

export function initialVariableNames(excludeName = "") {
  const start = state.workflow.nodes.find((n) => n.type === "start");
  return new Set(((start?.config?.inputs) || [])
    .map((s) => s.name)
    .filter((name) => name && name !== excludeName));
}

export function validateInitialVariableName(name) {
  const value = (name || "").trim();
  if (!value) return { ok: false, message: "变量名不能为空" };
  if (!isValidVarName(value)) {
    return { ok: false, message: "变量名须为 ASCII 标识符，且不能使用系统保留字" };
  }
  if (initialVariableNames().has(value)) {
    return { ok: false, message: `初始变量「${value}」已存在` };
  }
  if (automaticVariableNames().has(value)) {
    return { ok: false, message: `变量「${value}」会与节点自动产出的变量冲突` };
  }
  return { ok: true, name: value };
}

export function parseReturnDictKeys(code) {
  const keys = [];
  const seen = new Set();
  const lines = String(code || "").split(/\r?\n/);
  const mainIndex = lines.findIndex((line) =>
    /^[ \t]*(?:async\s+)?def\s+main\s*\(/.test(line));
  if (mainIndex < 0) return keys;
  const mainIndent = indentationOf(lines[mainIndex]);
  const nestedDefinitions = [];

  for (let i = mainIndex + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.trim() || line.trimStart().startsWith("#")) continue;
    const indent = indentationOf(line);
    if (indent <= mainIndent) break;
    while (nestedDefinitions.length &&
           indent <= nestedDefinitions[nestedDefinitions.length - 1]) {
      nestedDefinitions.pop();
    }
    if (/^[ \t]*(?:async\s+)?(?:def|class)\s+\w+\b/.test(line)) {
      nestedDefinitions.push(indent);
      continue;
    }
    if (nestedDefinitions.length || !/^[ \t]*return\s*\{/.test(line)) continue;
    const body = lines.slice(i).join("\n").match(/^[ \t]*return\s*\{([\s\S]*?)\}/)?.[1];
    if (body == null) continue;
    const keyRe = /["']([A-Za-z_][A-Za-z0-9_-]*)["']\s*:/g;
    let km;
    while ((km = keyRe.exec(body))) {
      if (!seen.has(km[1])) {
        keys.push(km[1]);
        seen.add(km[1]);
      }
    }
  }
  return keys;
}

function indentationOf(line) {
  return (line.match(/^[ \t]*/) || [""])[0].length;
}

export function toPythonParamName(name) {
  let value = String(name || "").replace(/[^0-9A-Za-z_]/g, "_");
  if (/^[0-9]/.test(value)) value = `_${value}`;
  return value;
}

export function migrateDefaultCodeTemplate(config) {
  if (!config) return false;
  const inputs = Array.isArray(config.inputs) ? config.inputs : [];
  if (inputs.length !== 1) return false;
  const name = inputs[0]?.name || "arg-1";
  const pyName = toPythonParamName(name);
  const code = String(config.code || "");
  const compact = code.replace(/\s+/g, " ").trim();
  const oldDefault = `def main(${pyName}): return {"result": ${pyName}}`;
  if (compact !== oldDefault) return false;
  config.code = `def main(params):\n    return {"result": params["${name}"]}\n`;
  return true;
}

export function syncCodeOutputsFromReturn(config) {
  const keys = parseReturnDictKeys(config.code || "");
  if (!keys.length) return false;
  const prev = new Map((config.outputs || []).map((o) => [o.name, o]));
  config.outputs = keys.map((name) => ({
    name,
    type: prev.get(name)?.type || "string",
  }));
  return true;
}

function producedVariablesForNode(n) {
  const vars = [];
  const cfg = n.config || {};
  if (n.type === "start") {
    for (const s of cfg.inputs || []) {
      if (s.name) vars.push({ name: s.name, type: s.type || "string", from: n.id });
    }
  } else if (n.type === "code") {
    for (const s of cfg.outputs || []) {
      if (s.name) vars.push({ name: s.name, type: s.type || "string", from: n.id });
    }
    if (cfg.error_branch) {
      const prefix = nodeVariableBase(n);
      vars.push({ name: `${prefix}-error-type`, type: "string", from: n.id });
      vars.push({ name: `${prefix}-error-message`, type: "string", from: n.id });
    }
  } else if (n.type === "for" || n.type === "aggregate") {
    const t = n.type === "aggregate"
      ? (cfg.output_type || "string")
      : "list";
    vars.push({ name: autoOutputName(n), type: t, from: n.id });
  }
  return vars;
}

function upstreamNodeIds(targetNodeId) {
  if (!targetNodeId) return null;
  const reverse = new Map();
  for (const edge of state.workflow.edges || []) {
    if (edge.source_handle === "retry") continue;
    if (!reverse.has(edge.target)) reverse.set(edge.target, []);
    reverse.get(edge.target).push(edge.source);
  }
  const seen = new Set();
  const queue = [...(reverse.get(targetNodeId) || [])];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    queue.push(...(reverse.get(id) || []));
  }
  return seen;
}

function outMap(edges = state.workflow.edges || []) {
  const map = new Map();
  for (const edge of edges) {
    if (edge.source_handle === "retry") continue;
    if (!map.has(edge.source)) map.set(edge.source, []);
    map.get(edge.source).push(edge);
  }
  return map;
}

function canReach(fromId, targetId, map, seen = new Set()) {
  if (!fromId || seen.has(fromId)) return false;
  if (fromId === targetId) return true;
  seen.add(fromId);
  for (const edge of map.get(fromId) || []) {
    if (canReach(edge.target, targetId, map, seen)) return true;
  }
  return false;
}

function isBranchGuaranteed(ownerId, targetNodeId) {
  if (!targetNodeId || !ownerId || ownerId === "system") return true;
  const map = outMap();
  for (const ifNode of state.workflow.nodes.filter((n) => n.type === "if")) {
    const branches = (map.get(ifNode.id) || [])
      .filter((edge) => edge.source_handle === "else" ||
        String(edge.source_handle || "").startsWith("if"));
    const branchesToTarget = branches.filter((edge) =>
      canReach(edge.target, targetNodeId, map));
    if (branchesToTarget.length < 2) continue;
    const ownerBranches = branchesToTarget.filter((edge) =>
      canReach(edge.target, ownerId, map));
    if (ownerBranches.length > 0 &&
        ownerBranches.length < branchesToTarget.length) {
      return false;
    }
  }
  return true;
}

function pushUnique(vars, item, seen) {
  if (!item?.name || seen.has(item.name)) return;
  vars.push(item);
  seen.add(item.name);
}

/** 计算某节点可见变量：系统变量 + DAG 上游传导变量。 */
export function availableVariables(excludeNodeId = null, extraVars = []) {
  const vars = [];
  const seen = new Set();
  for (const v of SYSTEM_VARIABLES) pushUnique(vars, v, seen);
  const upstream = upstreamNodeIds(excludeNodeId);
  for (const n of state.workflow.nodes) {
    if (n.id === excludeNodeId) continue;
    if (upstream && !upstream.has(n.id)) continue;
    if (!isBranchGuaranteed(n.id, excludeNodeId)) continue;
    for (const v of producedVariablesForNode(n)) pushUnique(vars, v, seen);
  }
  for (const v of extraVars) pushUnique(vars, v, seen);
  return vars;
}
