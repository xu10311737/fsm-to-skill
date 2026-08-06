/* fsm to skill - 节点配置面板 */
import {
  NODE_TYPES, VAR_TYPES, IF_OPERATORS, availableVariables, changed,
  serializeWorkflow, makeEdge, state, makeNode, removeNode, setNodeName,
  syncCodeOutputsFromReturn, migrateDefaultCodeTemplate, parseReturnDictKeys,
  normalizeCodeInputs, autoOutputName, nodeVariableBase, normalizeNodeName,
  setGraphNodeName,
} from "./store.js";
import { api } from "./api.js?v=20260730-agent-debug";
import { el, openModal, toast, attachVarHelper, parseTypedValue, typedInput } from "./ui.js";
import { Canvas } from "./canvas.js";

let panelRoot = null;
let headRoot = null;

export function initPanels() {
  panelRoot = document.getElementById("config-body");
  headRoot = document.getElementById("config-head");
}

export function renderPanel(node) {
  if (!panelRoot) initPanels();
  panelRoot.innerHTML = "";
  headRoot.innerHTML = "";
  if (!node) {
    headRoot.append(el("span", { class: "panel-title", text: "节点配置" }));
    panelRoot.append(el("div", {
      class: "placeholder",
      text: "在画布中选择一个节点进行配置。\n\n拖动画布空白处平移，滚轮缩放；从节点右侧端口拖出连线；点击节点 + 按钮追加后续节点。",
    }));
    return;
  }
  const meta = NODE_TYPES[node.type] || { label: node.type };
  headRoot.append(el("div", { class: "panel-head-main" },
    el("span", { class: "panel-title", text: `${meta.label} 节点` }),
    el("span", { class: "panel-sub", text: node.id })));
  if (node.type !== "start") {
    headRoot.append(el("button", {
      class: "btn small danger", text: "删除节点",
      onclick: () => {
        if (!confirm(`确定删除「${node.name || node.id}」？`)) return;
        removeNode(node.id);
        renderPanel(null);
      },
    }));
  }

  if (node.type !== "start" && node.type !== "end") {
    panelRoot.append(field("节点名称", el("input", {
      class: "input", value: node.name || "",
      onchange: (ev) => {
        const res = setNodeName(node, ev.target.value);
        if (!res.ok) {
          toast(res.message, "error");
          ev.target.value = node.name || "";
        } else {
          ev.target.value = node.name || "";
        }
      },
    }), "节点名称必须唯一。"));
  }

  const builders = {
    start: buildStart, code: buildCode, llm: buildLLM,
    if: buildIf, for: buildFor, aggregate: buildAggregate, end: buildEnd,
  };
  (builders[node.type] || (() => {}))(node);
}

function field(label, control, hint) {
  return el("div", { class: "field" },
    el("label", { class: "field-label", text: label }),
    control,
    hint ? el("div", { class: "field-hint", text: hint }) : null);
}

function varDatalist(excludeId, extra = [], filterFn = null) {
  const dl = el("datalist", { id: `dl-${Math.random().toString(36).slice(2)}` });
  for (const v of availableVariables(excludeId, extra).filter((x) =>
    !filterFn || filterFn(x))) {
    dl.append(el("option", { value: v.name, text: `${v.type} (${v.from})` }));
  }
  panelRoot.append(dl);
  return dl.id;
}

function codeEditor(value, onInput, rows = 12) {
  const wrap = el("div", { class: "code-editor-wrap" });
  const gutter = el("div", { class: "code-gutter" });
  const ta = el("textarea", {
    class: "code-textarea mono", rows: String(rows), spellcheck: "false",
    autocomplete: "off", autocapitalize: "off",
  });
  ta.value = value || "";
  const syncLines = () => {
    const count = Math.max(1, ta.value.split("\n").length);
    gutter.textContent = Array.from({ length: count }, (_, i) => i + 1).join("\n");
  };
  ta.addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab") return;
    ev.preventDefault();
    const start = ta.selectionStart;
    const end = ta.selectionEnd;
    ta.value = ta.value.slice(0, start) + "    " + ta.value.slice(end);
    ta.selectionStart = ta.selectionEnd = start + 4;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  });
  ta.addEventListener("input", () => {
    syncLines();
    onInput(ta.value);
  });
  syncLines();
  wrap.append(gutter, ta);
  return { wrap, textarea: ta, sync: syncLines };
}

function typeSelect(spec, onChange) {
  const sel = el("select", { class: "input" });
  for (const t of VAR_TYPES) sel.append(el("option", { value: t, text: t }));
  sel.value = spec.type || "string";
  sel.addEventListener("change", () => {
    spec.type = sel.value;
    onChange();
  });
  return sel;
}

function paramNameCell(spec, onChange) {
  const input = el("input", {
    class: "input mono", placeholder: "arg-1", value: spec.name || "",
    oninput: (ev) => {
      spec.name = ev.target.value.trim();
      onChange();
    },
  });
  return el("div", { class: "param-name-cell" }, input);
}

function variableBindingCell(spec, getVars, onChange) {
  const datalist = el("datalist", {
    id: `dl-source-${Math.random().toString(36).slice(2)}`,
  });
  for (const variable of getVars()) {
    datalist.append(el("option", {
      value: `{{ ${variable.name} }}`,
      text: `${variable.type} (${variable.from})`,
    }));
  }
  const input = el("input", {
    class: "input mono", list: datalist.id,
    placeholder: "变量填入（可选）",
    value: formatVariableBinding(spec.source),
    oninput: (ev) => {
      const source = parseVariableBinding(ev.target.value);
      if (source) spec.source = source;
      else delete spec.source;
      onChange();
    },
    onchange: (ev) => {
      ev.target.value = formatVariableBinding(spec.source);
    },
  });
  attachVarHelper(input, getVars);
  return el("div", { class: "param-source-cell" }, input, datalist);
}

function parseVariableBinding(value) {
  const raw = String(value || "").trim();
  const match = /^{{\s*([^{}]+?)\s*}}$/.exec(raw);
  return (match ? match[1] : raw).trim();
}

function formatVariableBinding(source) {
  return source ? `{{ ${source} }}` : "";
}

/* ---------------- Start ---------------- */
function buildStart(node) {
  const list = el("div", { class: "kv-list" });
  const renderInputs = () => {
    const inputs = node.config?.inputs || [];
    list.innerHTML = "";
    list.append(el("div", { class: "var-row compact" },
      el("span", { class: "var-name mono", text: "task-id" }),
      el("span", { class: "var-type", text: "string" }),
      el("span", { class: "var-val", text: "系统变量" })));
    inputs.forEach((spec) => {
      list.append(el("div", { class: "var-row compact" },
        el("span", { class: "var-name mono", text: spec.name || "-" }),
        el("span", { class: "var-type", text: spec.type || "string" }),
        el("span", { class: "var-val", text: spec.default || "" }),
        el("button", {
          class: "btn icon danger",
          title: "删除变量",
          text: "✕",
          onclick: () => {
            node.config.inputs = (node.config.inputs || [])
              .filter((item) => item.name !== spec.name);
            renderInputs();
            changed();
          },
        })));
    });
    if (!inputs.length) {
      list.append(el("div", { class: "field-hint",
        text: "除 task-id 外，当前没有用户新增初始变量。" }));
    }
  };
  renderInputs();
  panelRoot.append(
    el("div", { class: "placeholder",
      text: "开始节点是运行入口，不在此处配置。初始变量请使用左侧「新增初始变量」按钮维护。" }),
    field("当前初始变量", list));
}

/* ---------------- Code ---------------- */
function buildCode(node) {
  const cfg = node.config;
  normalizeCodeInputs(cfg);
  if (migrateDefaultCodeTemplate(cfg)) changed();
  syncCodeOutputsFromReturn(cfg);

  // Agent/argparse 参数 schema
  const inWrap = el("div", { class: "kv-list" });
  let editor = null;
  const renderInputs = () => {
    inWrap.innerHTML = "";
    cfg.inputs.forEach((spec, i) => {
      if (!spec.type) spec.type = "string";
      if (spec.required == null) spec.required = true;
      inWrap.append(el("div", { class: "kv-row param-row" },
        paramNameCell(spec, () => {
          renderOutputs();
          changed();
        }),
        variableBindingCell(spec, () => availableVariables(node.id), () => changed()),
        el("input", {
          class: "input", placeholder: "参数描述", value: spec.description || "",
          oninput: (ev) => { spec.description = ev.target.value; changed(); },
        }),
        typeSelect(spec, () => changed()),
        el("label", { class: "switch param-required" },
          el("input", {
            type: "checkbox", checked: spec.required !== false,
            onchange: (ev) => { spec.required = ev.target.checked; changed(); },
          }),
          el("span", { text: "必填" })),
        el("button", {
          class: "btn icon danger", text: "✕", title: "删除输入变量",
          onclick: () => {
            cfg.inputs.splice(i, 1);
            renderInputs();
            renderOutputs();
            changed();
          },
        })));
    });
  };
  renderInputs();
  panelRoot.append(
    field("输入参数（argparse schema）", inWrap,
      "选择变量后会以 Jinja 语法显示，并由工作流自动填入；该字段不会出现在 Agent 入参中。"),
    el("button", {
    class: "btn", text: "+ 添加输入",
      onclick: () => {
        cfg.inputs.push({
          name: nextArgName(cfg),
          description: "参数描述",
          type: "string",
          required: true,
        });
        renderInputs();
        renderOutputs();
        changed();
      },
    }));

  // 代码编辑
  const outputWrap = el("div", { class: "kv-list" });
  const renderOutputs = () => {
    outputWrap.innerHTML = "";
    const keys = parseReturnDictKeys(cfg.code || "");
    if (!keys.length) {
      outputWrap.append(el("div", { class: "field-hint",
        text: "尚未解析到静态 return 字典，例如 return {\"result\": value}" }));
      return;
    }
    syncCodeOutputsFromReturn(cfg);
    for (const out of cfg.outputs || []) {
      outputWrap.append(el("div", { class: "kv-row output-row" },
        el("span", { class: "output-name mono", text: out.name }),
        typeSelect(out, () => changed())));
    }
  };
  editor = codeEditor(cfg.code || "", (value) => {
    cfg.code = value;
    renderOutputs();
    changed();
  }, 14);
  panelRoot.append(field("Python 代码", editor.wrap,
    "定义 main(params)；通过 params[\"arg-1\"] 读取输入，return 字典的 key 会自动成为输出变量名。"));

  // 输出
  renderOutputs();
  panelRoot.append(field("输出变量（自动解析）", outputWrap));

  // 超时 + 错误分支
  panelRoot.append(el("div", { class: "field-row" },
    field("超时（秒）", el("input", {
      class: "input", type: "number", min: "1", value: String(cfg.timeout ?? 30),
      oninput: (ev) => {
        cfg.timeout = Math.max(1, parseInt(ev.target.value, 10) || 30);
        changed();
      },
    })),
    field("错误分支", el("label", { class: "switch" },
      el("input", {
        type: "checkbox", checked: !!cfg.error_branch,
        onchange: (ev) => {
          cfg.error_branch = ev.target.checked;
          if (cfg.error_branch) ensureCodeErrorPrompt(node);
          else state.workflow.edges = state.workflow.edges.filter((edge) =>
            !(edge.source === node.id && edge.source_handle === "error"));
          changed();
        },
      }),
      el("span", { text: " 启用 error 出边" })))));

  buildDebugSection(node);
}

function nextArgName(cfg) {
  const used = new Set((cfg.inputs || []).map((s) => s.name));
  let i = 1;
  while (used.has(`arg-${i}`)) i += 1;
  return `arg-${i}`;
}

function ensureCodeErrorPrompt(node, graph = state.workflow) {
  const nodes = graph.nodes || [];
  const edges = graph.edges || [];
  const existing = edges.find((edge) =>
    edge.source === node.id && edge.source_handle === "error" &&
    nodes.find((n) => n.id === edge.target && n.type === "llm"));
  if (existing) return;
  graph.edges = edges.filter((edge) =>
    !(edge.source === node.id && edge.source_handle === "error"));
  const prompt = makeNode("llm", {
    x: (node.position?.x || 100) + 280,
    y: (node.position?.y || 100) + 90,
  });
  if (graph !== state.workflow) {
    const used = new Set(nodes.map((item) => item.id));
    const base = `${node.id}-error-prompt`;
    let id = base;
    let index = 1;
    while (used.has(id)) id = `${base}-${index++}`;
    prompt.id = id;
  }
  prompt.name = uniquePromptName("Error-Prompt", nodes);
  const prefix = nodeVariableBase(node);
  prompt.config.prompt =
    `Code 节点「${node.name || node.id}」执行失败。\n` +
    `错误类型：{{ ${prefix}-error-type }}\n` +
    `错误信息：{{ ${prefix}-error-message }}\n\n` +
    "请基于错误信息给 Agent 一个可执行的下一步说明。";
  graph.nodes.push(prompt);
  graph.edges.push(makeEdge(node.id, prompt.id, "error"));
}

function uniquePromptName(base, nodes = state.workflow.nodes) {
  const used = new Set(nodes.map((n) => n.name));
  let i = 1;
  let name = `${normalizeNodeName(base)}-${i}`;
  while (used.has(name)) {
    i += 1;
    name = `${normalizeNodeName(base)}-${i}`;
  }
  return name;
}

function buildDebugSection(node, mount = panelRoot, {
  inlineInputs = false,
} = {}) {
  const cfg = node.config;
  normalizeCodeInputs(cfg);
  const box = el("div", { class: "debug-box" });
  box.append(el("div", { class: "section-title", text: "单节点调试" }));
  const resultWrap = el("div", { class: "debug-result" });
  const specs = (cfg.inputs || []).filter((s) => s.name);
  let inlineForm = null;

  const runDebug = async (inputs) => {
    resultWrap.innerHTML = "";
    resultWrap.append(el("div", { class: "muted", text: "调试运行中…" }));
    try {
      const resp = await api.debugNode(serializeWorkflow(), node.id, inputs);
      renderDebugResult(resultWrap, resp);
      return resp;
    } catch (err) {
      resultWrap.innerHTML = "";
      const msg = err.data?.detail;
      const detail = typeof msg === "string" ? msg : (msg?.message || err.message);
      resultWrap.append(el("pre", { class: "debug-error", text: detail }));
      throw err;
    }
  };

  const runWithInputsModal = () => {
    const formWrap = el("div", { class: "debug-form" });
    const modalClose = openModal({
      title: `单节点调试 - ${node.name || node.id}`,
      wide: true,
      body: formWrap,
      footer: [
        { label: "取消", action: (close) => close() },
      ],
    });
    renderDebugForm(formWrap, specs, async (inputs) => {
      try {
        const resp = await runDebug(inputs);
        if (resp) modalClose();
      } catch {
        /* 结果已在面板中显示 */
      }
    });
  };

  const runWithInlineInputs = () => {
    if (inlineForm) return;
    inlineForm = el("div", { class: "debug-form inline-debug-form" });
    box.append(inlineForm);
    renderDebugForm(inlineForm, specs, async (inputs) => {
      try {
        await runDebug(inputs);
      } catch {
        /* 结果已在面板中显示 */
      }
    });
  };

  box.append(el("button", {
    class: "btn primary", text: "单节点调试",
    onclick: async () => {
      if (specs.length) {
        if (inlineInputs) runWithInlineInputs();
        else runWithInputsModal();
        return;
      }
      try {
        await runDebug({});
      } catch (err) {
        const detail = err.data?.detail;
        const text = typeof detail === "string"
          ? detail
          : (detail?.message || err.message || "");
        if (/首次调试必须提供入参|调试必须提供入参|缺少必填调试参数|缺少入参/.test(text)) {
          if (inlineInputs) runWithInlineInputs();
          else runWithInputsModal();
          return;
        }
        toast(text, "error");
      }
    },
  }));
  box.append(resultWrap);
  mount.append(box);
}

function renderDebugForm(wrap, specs, onRun) {
  wrap.innerHTML = "";
  const controls = {};
  for (const spec of specs) {
    const input = typedInput(spec.type || "string");
    input.placeholder = spec.description || `参数 ${spec.name}`;
    controls[spec.name] = { input, spec };
    wrap.append(field(
      `${spec.name}（${spec.type || "string"}${spec.required === false ? "，可选" : "，必填"}）`,
      input,
      spec.description || ""));
  }
  wrap.append(el("button", {
    class: "btn primary", text: "使用以上输入运行",
    onclick: () => {
      const inputs = {};
      try {
        for (const [k, item] of Object.entries(controls)) {
          const raw = item.input.value;
          const spec = item.spec;
          if (raw === "" && spec.required === false) continue;
          if (raw === "" && spec.required !== false) {
            throw new Error(`请输入必填参数 ${k}`);
          }
          inputs[k] = parseTypedValue(spec.type || "string", raw);
        }
        onRun(inputs);
      } catch (err) {
        toast(err.message, "error");
      }
    },
  }));
}

function renderDebugResult(wrap, resp) {
  wrap.innerHTML = "";
  if (resp.ok) {
    const head = resp.duration_ms != null
      ? `✓ 成功（${Math.round(resp.duration_ms)} ms）` : "✓ 成功";
    wrap.append(el("div", { class: "debug-ok", text: head }));
    if (resp.result !== undefined) {
      wrap.append(el("div", { class: "field-label", text: "result" }),
        el("pre", { class: "debug-pre", text: JSON.stringify(resp.result, null, 2) }));
    }
    if (resp.prompt) {
      wrap.append(el("div", { class: "field-label", text: "实际 Prompt" }),
        el("pre", { class: "debug-pre", text: resp.prompt }));
    }
    if (resp.content !== undefined) {
      wrap.append(el("div", { class: "field-label", text: "模型输出" }),
        el("pre", { class: "debug-pre", text: resp.content }));
    }
    if (resp.thinking) {
      wrap.append(el("div", { class: "field-label", text: "思考过程" }),
        el("pre", { class: "debug-pre", text: resp.thinking }));
    }
    if (resp.usage) {
      wrap.append(el("div", { class: "field-label", text: "Token 用量" }),
        el("pre", { class: "debug-pre", text: JSON.stringify(resp.usage) }));
    }
    if (resp.stdout) {
      wrap.append(el("div", { class: "field-label", text: "stdout" }),
        el("pre", { class: "debug-pre", text: resp.stdout }));
    }
  } else {
    wrap.append(
      el("div", { class: "debug-error", text: `✕ ${resp.error_type || "Error"}` }),
      el("pre", { class: "debug-pre", text: resp.error_message || "" }));
    if (resp.stderr) {
      wrap.append(el("div", { class: "field-label", text: "stderr" }),
        el("pre", { class: "debug-pre", text: resp.stderr }));
    }
  }
}

/* ---------------- LLM ---------------- */
function buildLLM(node) {
  const cfg = node.config;
  const ta = el("textarea", {
    class: "input mono prompt-editor", rows: "8", spellcheck: "false",
    placeholder: "输入提示词；键入 / 可插入变量，如 {{ Code-1-output }}",
  });
  ta.value = cfg.prompt || "";
  ta.addEventListener("input", () => { cfg.prompt = ta.value; changed(); });
  attachVarHelper(ta, () => availableVariables(node.id));
  panelRoot.append(
    field("提示词（支持 Jinja2 模板）", ta),
    field("自动追加内容", el("pre", {
      class: "readonly-preview mono",
      text: promptAppendPreview(node, state.workflow),
    }),
    "动态生成，运行时自动拼到 Prompt 后；Code error 分支不会追加。"));
}

function promptAppendPreview(node, graph, opts = {}) {
  const parts = [];
  if (opts.loop) {
    parts.push([
      "-------------",
      "循环上下文:",
      "",
      "当前轮次 {index+1}/{len}",
      "当前 item: {item}",
      "-------------",
    ].join("\n"));
  }
  const incomingError = (graph.edges || []).some((edge) =>
    edge.target === node.id && edge.source_handle === "error");
  if (incomingError) {
    parts.push("Code error 分支 Prompt 不追加下一步命令。");
    return parts.join("\n\n");
  }
  const entries = nextCodeEntries(graph, node.id);
  if (!entries.length) {
    parts.push("当前后续路径没有 Code 节点。");
    return parts.join("\n\n");
  }
  const first = entries[0];
  parts.push(formatNextCodePreview(entries));
  return parts.join("\n\n");
}

function formatNextCodePreview(entries) {
  const first = entries[0];
  const stepParam = first.input_schema.length
    ? "--step-param <下文中实际节点入参>"
    : "--step-param {}";
  return [
    "-------------",
    "## 下一个step待执行命令:",
    "",
    [
      "python {skill路径中的main.py文件绝对路径} \\",
      "--task_id {task_id} \\",
      `--step-id ${first.step_id} \\`,
      stepParam,
    ].join("\n"),
    "-------------",
    "**step-param 入参说明**:",
    "",
    formatCodeSchemaMd(entries),
    "-------------",
  ].join("\n");
}

function nextCodeEntries(graph, promptId) {
  const nodes = graph.nodes || [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out = new Map();
  for (const edge of graph.edges || []) {
    if (edge.source_handle === "error" || edge.source_handle === "retry") continue;
    if (!out.has(edge.source)) out.set(edge.source, []);
    out.get(edge.source).push(edge);
  }
  const entries = [];
  const seen = new Set();
  const queue = [...(out.get(promptId) || []).map((edge) => edge.target)];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const n = byId.get(id);
    if (!n) continue;
    if (n.type === "code") {
      entries.push({
        step_id: n.id,
        node_name: n.name || n.id,
        input_schema: (n.config?.inputs || []).filter((spec) => !spec.source)
          .map((spec, i) => ({
          name: spec.name || `arg-${i + 1}`,
          description: spec.description || "",
          type: spec.type || "string",
          required: spec.required !== false,
          })),
      });
      continue;
    }
    if (n.type === "llm" || n.type === "end") continue;
    queue.push(...(out.get(id) || []).map((edge) => edge.target));
  }
  return entries;
}

function formatCodeSchemaMd(entries) {
  const lines = [];
  for (const entry of entries) {
    lines.push(`### 节点${entry.node_name}`, "");
    lines.push("| 参数 | 类型 | 必填 | 描述 |");
    lines.push("| --- | --- | --- | --- |");
    for (const spec of entry.input_schema) {
      lines.push(`| ${spec.name} | ${spec.type} | ${spec.required ? "是" : "否"} | ${spec.description || ""} |`);
    }
    lines.push("");
  }
  return lines.join("\n").trim();
}

/* ---------------- IF ---------------- */
function buildIf(node) {
  const cfg = node.config;
  const dlId = varDatalist(node.id);
  ensureIfConditions(cfg);
  const wrap = el("div", { class: "condition-list" });

  const render = () => {
    wrap.innerHTML = "";
    cfg.conditions.forEach((cond, i) => {
      wrap.append(conditionRow(cond, i, dlId, () => {
        removeIfCondition(node, i, state.workflow);
        render();
        changed();
      }));
    });
  };
  render();
  panelRoot.append(
    field("IF 条件", wrap),
    el("button", {
      class: "btn", text: "+ 添加条件",
      onclick: () => {
        cfg.conditions.push(emptyIfCondition());
        cfg.branch_mode = "multi";
        render(); changed();
      },
    }),
    el("div", { class: "field-hint",
      text: "条件按顺序匹配：每条条件各有一个 IF 出口，均不满足时走 ELSE；每个出口只能连接一个节点。" }));
}

function emptyIfCondition() {
  return { variable: "", operator: "是", value: "", value_type: "constant" };
}

function ensureIfConditions(cfg, fallbackVariable = "") {
  if (!Array.isArray(cfg.conditions) || !cfg.conditions.length) {
    cfg.conditions = [{
      variable: cfg.variable || fallbackVariable,
      operator: cfg.operator || "是",
      value: cfg.value ?? "",
      value_type: cfg.value_type || "constant",
    }];
  }
  cfg.branch_mode = "multi";
  delete cfg.combinator;
}

function removeIfCondition(node, index, graph) {
  const cfg = node.config;
  ensureIfConditions(cfg);
  const handle = `if-${index + 1}`;
  if (cfg.conditions.length === 1) {
    cfg.conditions = [emptyIfCondition()];
    graph.edges = graph.edges.filter((edge) =>
      !(edge.source === node.id && edge.source_handle === handle));
    return;
  }
  cfg.conditions.splice(index, 1);
  graph.edges = graph.edges
    .filter((edge) => !(edge.source === node.id && edge.source_handle === handle))
    .map((edge) => {
      if (edge.source !== node.id) return edge;
      const match = /^if-(\d+)$/.exec(edge.source_handle || "");
      if (match && Number(match[1]) > index + 1) {
        edge.source_handle = `if-${Number(match[1]) - 1}`;
      }
      return edge;
    });
}

function conditionRow(cond, index, dlId, onDelete) {
  const row = el("div", { class: "condition-row" });
  const title = el("div", { class: "condition-title" },
    el("span", { text: `IF 分支 ${index + 1}` }),
    el("button", { class: "btn icon danger", text: "✕", onclick: onDelete }));
  const varInput = el("input", {
    class: "input mono", list: dlId, placeholder: "变量",
    value: cond.variable || "",
    oninput: (ev) => { cond.variable = ev.target.value.trim(); changed(); },
  });
  const opSel = el("select", { class: "input" });
  for (const op of IF_OPERATORS) opSel.append(el("option", { value: op, text: op }));
  opSel.value = cond.operator || "是";
  const valueWrap = el("div", {});
  const renderValue = () => {
    valueWrap.innerHTML = "";
    if (opSel.value === "为空" || opSel.value === "不为空") return;
    const typeSel = el("select", { class: "input compact-select" },
      el("option", { value: "constant", text: "常量" }),
      el("option", { value: "variable", text: "变量" }));
    typeSel.value = cond.value_type || "constant";
    const valInput = el("input", {
      class: "input mono",
      placeholder: typeSel.value === "variable" ? "比较变量" : "比较值",
      value: cond.value == null ? "" : String(cond.value),
    });
    if (typeSel.value === "variable") valInput.setAttribute("list", dlId);
    typeSel.addEventListener("change", () => {
      cond.value_type = typeSel.value;
      cond.value = "";
      renderValue();
      changed();
    });
    valInput.addEventListener("input", () => {
      cond.value = valInput.value;
      changed();
    });
    valueWrap.append(el("div", { class: "kv-row" }, typeSel, valInput));
  };
  opSel.addEventListener("change", () => {
    cond.operator = opSel.value;
    renderValue();
    changed();
  });
  renderValue();
  row.append(title, varInput, opSel, valueWrap);
  return row;
}

/* ---------------- For ---------------- */
function buildFor(node) {
  const cfg = node.config;
  const dlId = varDatalist(node.id, [], (v) => v.type === "list");
  if (!cfg.body || typeof cfg.body !== "object") cfg.body = { nodes: [], edges: [] };
  if (!Array.isArray(cfg.body.nodes)) cfg.body.nodes = [];
  if (!Array.isArray(cfg.body.edges)) cfg.body.edges = [];

  panelRoot.append(
    field("列表来源（list 变量）", el("input", {
      class: "input mono", list: dlId, value: cfg.list_source || "",
      oninput: (ev) => { cfg.list_source = ev.target.value.trim(); changed(); },
    })),
    el("div", { class: "field-hint",
      text: `收集变量自动推导：${inferForCollect(cfg) || "循环体末端输出"}` }),
    el("div", { class: "field-hint",
      text: "循环体内可使用 index（int）与 item（当前元素）；输出固定为当前 For 节点的 list。" }),
    el("button", {
      class: "btn primary", text: "编辑循环体",
      onclick: () => openForBodyEditor(node),
    }),
    el("div", { class: "field-hint",
      text: `输出变量：${autoOutputName(node)}（list）` }));
}

function openForBodyEditor(forNode) {
  const cfg = forNode.config;
  if (!cfg.body || typeof cfg.body !== "object") cfg.body = { nodes: [], edges: [] };
  if (!Array.isArray(cfg.body.nodes)) cfg.body.nodes = [];
  if (!Array.isArray(cfg.body.edges)) cfg.body.edges = [];
  const backup = JSON.stringify(cfg.body);
  const layout = el("div", { class: "body-editor-layout" });
  const wrap = el("div", { class: "body-canvas-wrap" });
  const bodySvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  bodySvg.setAttribute("class", "canvas-edges");
  wrap.append(
    bodySvg,
    el("div", { class: "canvas-content" }));
  const bodyResizer = el("div", { class: "body-panel-resizer", title: "拖动调整循环体配置宽度" });
  const bodyPanel = el("div", { class: "body-node-panel placeholder",
    text: "选择循环体节点以配置" });
  layout.append(wrap, bodyResizer, bodyPanel);

  const bodyGraph = cfg.body;
  let counter = 0;
  const genId = (type) => {
    counter += 1;
    let id = `${forNode.id}_${type}${counter}`;
    const all = new Set(bodyGraph.nodes.map((n) => n.id));
    while (all.has(id)) { counter += 1; id = `${forNode.id}_${type}${counter}`; }
    return id;
  };

  let canvas;
  const renderEmptyBodyPanel = () => {
    bodyPanel.classList.add("placeholder");
    bodyPanel.innerHTML = "选择循环体节点以配置";
  };
  const renderSelectedBodyNode = (node) => {
    bodyPanel.classList.remove("placeholder");
    bodyPanel.innerHTML = "";
    renderBodyNodePanel(node, bodyPanel, forNode, {
      refresh: () => {
        canvas.render();
        canvas.setSelected(null);
        renderEmptyBodyPanel();
      },
      rename: (target, name) =>
        setBodyNodeName(bodyGraph, target, name, forNode.config),
      refreshBranchPorts: (target) => {
        canvas.render();
        canvas.setSelected(target.id);
        renderSelectedBodyNode(target);
      },
    });
  };
  const spawnAtCenter = (type) => {
    if (!canvas) return;
    const rect = wrap.getBoundingClientRect();
    const pos = {
      x: (rect.width / 2 - canvas.pan.x) / canvas.zoom - 88,
      y: (rect.height / 2 - canvas.pan.y) / canvas.zoom - 36,
    };
    const created = createBodyNode(type, pos);
    canvas.render();
    canvas.setSelected(created.id);
    renderSelectedBodyNode(created);
    changed();
  };
  const toolbar = el("div", { class: "body-toolbar" },
    el("button", { class: "btn small", text: "+ Code",
      onclick: () => spawnAtCenter("code") }),
    el("button", { class: "btn small", text: "+ Prompt",
      onclick: () => spawnAtCenter("llm") }),
    el("button", { class: "btn small", text: "+ IF",
      onclick: () => spawnAtCenter("if") }),
    el("button", { class: "btn small", text: "+ 聚合",
      onclick: () => spawnAtCenter("aggregate") }),
    el("button", { class: "btn small", text: "自动布局",
      onclick: () => canvas && canvas.autoLayout() }));
  const close = openModal({
    title: `编辑循环体 - ${forNode.name || forNode.id}`,
    wide: true,
    className: "body-editor-modal",
    body: el("div", {},
      el("div", { class: "hint-bar",
        text: "右键画布添加节点：Code / LLM / IF / 聚合（不支持嵌套 For）。循环体节点可使用 index、item 变量。" }),
      toolbar,
      layout),
    footer: [
      { label: "自动布局", action: () => canvas.autoLayout() },
      { label: "取消", action: (closeFn) => {
        cfg.body = JSON.parse(backup);
        changed();
        renderPanel(forNode);
        closeFn();
      } },
      { label: "完成", kind: "primary", action: (closeFn) => closeFn() },
    ],
  });

  wrap.addEventListener("contextmenu", (ev) => {
    if (ev.target !== wrap &&
        !ev.target.classList.contains("canvas-content") &&
        ev.target.tagName !== "svg") return;
    ev.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const pos = {
      x: (ev.clientX - rect.left - canvas.pan.x) / canvas.zoom,
      y: (ev.clientY - rect.top - canvas.pan.y) / canvas.zoom,
    };
    import("./canvas.js").then(({ showMenu }) => {
      showMenu(ev.clientX, ev.clientY,
        ["code", "llm", "if", "aggregate"].map((t) => ({
          label: NODE_TYPES[t].label,
          action: () => {
            const created = createBodyNode(t, pos);
            canvas.render();
            canvas.setSelected(created.id);
            renderSelectedBodyNode(created);
            changed();
          },
        })));
    });
  });

  function createBodyNode(type, pos) {
    const id = genId(type);
    const freePos = freeBodyNodePosition(type, pos, bodyGraph);
    const node = {
      id, type, name: nextBodyNodeName(type),
      position: freePos,
      config: defaultBodyConfig(type),
    };
    bodyGraph.nodes.push(node);
    return node;
  }

  canvas = new Canvas(wrap, bodyGraph, {
    allowTypes: ["code", "llm", "if", "aggregate"],
    showLinePlus: true,
    onCreateNode: (type, pos) => createBodyNode(type, pos),
    onChange: () => changed(),
    canConnect: (sourceId, targetId, handle) => {
      const source = bodyGraph.nodes.find((node) => node.id === sourceId);
      const target = bodyGraph.nodes.find((node) => node.id === targetId);
      if (source?.type !== "if" ||
          (handle !== "else" && !String(handle || "").startsWith("if"))) {
        if (handle !== "error") return true;
        if (source?.type !== "code" || !source.config?.error_branch) {
          toast("只有启用错误分支的 Code 节点才能连接 error 出边", "error");
          return false;
        }
        if (target?.type !== "llm") {
          toast("Code error 出边只能连接 Prompt 节点", "error");
          return false;
        }
        const exists = bodyGraph.edges.some((edge) =>
          edge.source === sourceId && edge.source_handle === "error" &&
          edge.target !== targetId);
        if (exists) {
          toast("Code error 出边只能连接一个 Prompt 节点", "error");
          return false;
        }
        return true;
      }
      const exists = bodyGraph.edges.some((edge) =>
        edge.source === sourceId && edge.source_handle === handle &&
        edge.target !== targetId);
      if (exists) {
        toast(`${handle === "else" ? "ELSE" : handle.toUpperCase()} 出口只能连接一个节点`, "error");
        return false;
      }
      return true;
    },
    onRenameNode: (node, name) => {
      const res = setBodyNodeName(bodyGraph, node, name, forNode.config);
      if (!res.ok) {
        toast(res.message, "error");
        return false;
      }
      return true;
    },
    onSelect: (id) => {
      if (!id) {
        renderEmptyBodyPanel();
        return;
      }
      const bn = bodyGraph.nodes.find((n) => n.id === id);
      if (bn) renderSelectedBodyNode(bn);
    },
  });

  wrap._canvas = canvas;
  installBodyPanelResizer(layout, bodyResizer, bodyPanel, canvas);
  requestAnimationFrame(() => canvas.render());
  setTimeout(() => canvas.render(), 80);
  setTimeout(() => canvas.render(), 240);

  function nextBodyNodeName(type) {
    const base = {
      code: "Code",
      llm: "Prompt",
      if: "IF",
      aggregate: "Aggregate",
    }[type] || NODE_TYPES[type]?.label || type;
    const used = new Set(bodyGraph.nodes.map((n) => (n.name || "").trim()));
    let i = 1;
    let name = `${base}-${i}`;
    while (used.has(name)) {
      i += 1;
      name = `${base}-${i}`;
    }
    return name;
  }
}

function installBodyPanelResizer(layout, resizer, panel, canvas) {
  resizer.addEventListener("pointerdown", (ev) => {
    ev.preventDefault();
    const startX = ev.clientX;
    const startW = panel.getBoundingClientRect().width;
    resizer.setPointerCapture(ev.pointerId);
    const move = (e2) => {
      const next = Math.max(320, Math.min(760, startW + (startX - e2.clientX)));
      layout.style.gridTemplateColumns = `minmax(520px, 1fr) 8px ${next}px`;
      requestAnimationFrame(() => canvas.render());
    };
    const up = () => {
      resizer.removeEventListener("pointermove", move);
      resizer.removeEventListener("pointerup", up);
      requestAnimationFrame(() => canvas.render());
    };
    resizer.addEventListener("pointermove", move);
    resizer.addEventListener("pointerup", up);
  });
}

function freeBodyNodePosition(type, pos, bodyGraph) {
  const width = type === "for" ? 240 : 176;
  const height = type === "for" ? 160 : 120;
  const gap = 24;
  const base = {
    x: Math.round(pos.x),
    y: Math.round(pos.y),
  };
  const occupied = (bodyGraph.nodes || []).map((node) => ({
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
  for (let ring = 1; ring <= 6; ring += 1) {
    const stepX = width + gap;
    const stepY = height + gap;
    for (const [dx, dy] of [
      [ring * stepX, 0],
      [ring * stepX, ring * stepY],
      [0, ring * stepY],
      [-ring * stepX, ring * stepY],
      [ring * stepX, -ring * stepY],
    ]) {
      const candidate = {
        x: Math.max(20, base.x + dx),
        y: Math.max(20, base.y + dy),
      };
      if (!overlaps(candidate)) return candidate;
    }
  }
  return base;
}

function setBodyNodeName(bodyGraph, node, name, ownerConfig) {
  return setGraphNodeName(bodyGraph, node, name, {
    relatedConfigs: ownerConfig ? [ownerConfig] : [],
  });
}

function inferForCollect(cfg) {
  const body = cfg.body || {};
  if (cfg.collect) return cfg.collect;
  const nodes = body.nodes || [];
  if (!nodes.length) return "item";
  const sources = new Set((body.edges || []).map((e) => e.source));
  const terminal = [...nodes].reverse().find((n) => !sources.has(n.id)) || nodes[nodes.length - 1];
  if (!terminal) return "";
  if (terminal.type === "code") {
    return (terminal.config?.outputs || [])[0]?.name || "";
  }
  if (terminal.type === "aggregate") {
    return autoOutputName(terminal);
  }
  return "";
}

function defaultBodyConfig(type) {
  switch (type) {
    case "code": return {
      inputs: [{
        name: "item",
        description: "当前循环元素",
        type: "string",
        required: true,
      }, {
        name: "index",
        description: "当前循环下标（从 0 开始）",
        type: "int",
        required: true,
      }],
      code: 'def main(params):\n    return {"result": params["item"]}\n',
      outputs: [{ name: "result", type: "string" }], timeout: 30,
      error_branch: false,
    };
    case "llm": return { prompt: "{{ item }}" };
    case "if": return {
      conditions: [{ variable: "item", operator: "不为空", value: "", value_type: "constant" }],
      branch_mode: "multi",
    };
    case "aggregate": return {
      output_type: "string", inputs: [], input_mode: "explicit",
    };
    default: return {};
  }
}

function renderBodyNodePanel(bn, container, forNode, opts = {}) {
  container.classList.remove("placeholder");
  container.innerHTML = "";
  const extra = [
    { name: "index", type: "int", from: forNode.id },
    { name: "item", type: "any", from: forNode.id },
    { name: "len", type: "int", from: forNode.id },
    { name: "total", type: "int", from: forNode.id },
  ];
  container.append(el("div", { class: "panel-title",
    text: `${NODE_TYPES[bn.type].label} · ${bn.id}` }));
  let editor = null;
  container.append(field("名称", el("input", {
    class: "input", value: bn.name || "",
    onchange: (ev) => {
      const res = opts.rename
        ? opts.rename(bn, ev.target.value)
        : { ok: true };
      if (!res.ok) {
        toast(res.message, "error");
        ev.target.value = bn.name || "";
      } else {
        ev.target.value = bn.name || "";
      }
    },
  })));

  if (bn.type === "code") {
    const cfg = bn.config;
    // Keep the same Code schema as top-level nodes.  Loop variables are
    // offered by bodyAvailableVariables, while an explicitly selected
    // source is persisted and resolved by the loop executor.
    normalizeCodeInputs(cfg);
    if (migrateDefaultCodeTemplate(cfg)) changed();
    syncCodeOutputsFromReturn(cfg);
    const inWrap = el("div", { class: "kv-list" });
    const renderRows = () => {
      inWrap.innerHTML = "";
      cfg.inputs.forEach((spec, i) => {
        if (!spec.type) spec.type = "string";
        if (spec.required == null) spec.required = true;
        inWrap.append(el("div", { class: "kv-row param-row" },
        paramNameCell(spec, () => {
          renderOutputList();
          changed();
        }),
        variableBindingCell(
          spec,
          () => bodyAvailableVariables(forNode, bn.id, extra),
          () => changed()),
        el("input", {
            class: "input", placeholder: "参数描述",
            value: spec.description || "",
            oninput: (ev) => { spec.description = ev.target.value; changed(); },
          }),
          typeSelect(spec, () => changed()),
          el("label", { class: "switch param-required" },
            el("input", {
              type: "checkbox", checked: spec.required !== false,
              onchange: (ev) => { spec.required = ev.target.checked; changed(); },
            }),
            el("span", { text: "必填" })),
          el("button", {
            class: "btn icon danger", text: "✕", title: "删除输入变量",
            onclick: () => {
              cfg.inputs.splice(i, 1);
              renderRows();
              renderOutputList();
              changed();
            },
          })));
      });
    };
    renderRows();
    const outputList = el("div", { class: "kv-list" });
    const renderOutputList = () => {
      outputList.innerHTML = "";
      const keys = parseReturnDictKeys(cfg.code || "");
      if (!keys.length) {
        outputList.append(el("div", { class: "field-hint",
          text: "尚未解析到 return 字典" }));
        return;
      }
      syncCodeOutputsFromReturn(cfg);
      (cfg.outputs || []).forEach((outSpec) => {
        outputList.append(el("div", { class: "kv-row output-row" },
          el("span", { class: "output-name mono", text: outSpec.name }),
          typeSelect(outSpec, () => changed())));
      });
    };
    editor = codeEditor(cfg.code || "", (value) => {
      cfg.code = value;
      renderOutputList();
      changed();
    }, 10);
    renderOutputList();
    container.append(
      field("输入参数（argparse schema）", inWrap,
        "可使用 / 插入变量；选择变量后会自动填入，不会出现在 Agent 入参中。"),
      el("button", {
        class: "btn", text: "+ 添加输入",
        onclick: () => {
          cfg.inputs.push({
            name: nextArgName(cfg),
            description: "参数描述",
            type: "string",
            required: true,
          });
          renderRows();
          renderOutputList();
          changed();
        },
      }),
      field("Python 代码", editor.wrap),
      field("输出变量（自动解析）", outputList),
      el("div", { class: "field-row" },
        field("超时（秒）", el("input", {
          class: "input", type: "number", min: "1",
          value: String(cfg.timeout ?? 30),
          oninput: (ev) => {
            cfg.timeout = Math.max(1, parseInt(ev.target.value, 10) || 30);
            changed();
          },
        })),
        field("错误分支", el("label", { class: "switch" },
          el("input", {
            type: "checkbox", checked: !!cfg.error_branch,
            onchange: (ev) => {
              cfg.error_branch = ev.target.checked;
              const body = forNode.config.body;
              if (cfg.error_branch) ensureCodeErrorPrompt(bn, body);
              else body.edges = (body.edges || []).filter((edge) =>
                !(edge.source === bn.id && edge.source_handle === "error"));
              opts.refreshBranchPorts?.(bn);
              changed();
            },
          }),
          el("span", { text: " 启用 error 出边" })))));
    buildDebugSection(bn, container, { inlineInputs: true });
  } else if (bn.type === "llm") {
    const ta = el("textarea", { class: "input mono prompt-editor", rows: "8" });
    ta.value = bn.config.prompt || "";
    ta.addEventListener("input", () => { bn.config.prompt = ta.value; changed(); });
    attachVarHelper(ta, () => bodyAvailableVariables(forNode, bn.id, extra));
    container.append(field("提示词", ta),
      field("自动追加内容", el("pre", {
        class: "readonly-preview mono",
        text: promptAppendPreview(
          bn, forNode.config.body || { nodes: [], edges: [] }, { loop: true }),
      })));
  } else if (bn.type === "if") {
    const cfg = bn.config;
    ensureIfConditions(cfg, "item");
    const dl = el("datalist", { id: `bdl-if-${Date.now()}` });
    bodyAvailableVariables(forNode, bn.id, extra)
      .forEach((v) => dl.append(el("option", { value: v.name })));
    container.append(dl);
    const condWrap = el("div", { class: "condition-list" });
    const renderConds = () => {
      condWrap.innerHTML = "";
      cfg.conditions.forEach((cond, i) => condWrap.append(conditionRow(cond, i, dl.id, () => {
        removeIfCondition(bn, i, forNode.config.body);
        if (opts.refreshBranchPorts) opts.refreshBranchPorts(bn);
        else renderConds();
        changed();
      })));
    };
    renderConds();
    container.append(field("IF 条件", condWrap),
      el("button", {
        class: "btn", text: "+ 添加条件",
        onclick: () => {
          cfg.conditions.push({
            variable: "item", operator: "不为空", value: "", value_type: "constant",
          });
          cfg.branch_mode = "multi";
          if (opts.refreshBranchPorts) opts.refreshBranchPorts(bn);
          else renderConds();
          changed();
        },
      }),
      el("div", { class: "field-hint",
        text: "每条条件对应一个 IF 出口，ELSE 为兜底出口；每个出口只能连接一个节点。" }));
  } else if (bn.type === "aggregate") {
    container.append(...aggregateEditorFields(
      bn,
      forNode.config.body,
      () => changed(),
      () => opts.refreshBranchPorts?.(bn),
    ));
  }
  container.append(el("button", {
    class: "btn danger", text: "删除此节点",
    onclick: () => {
      const body = forNode.config.body;
      body.nodes = body.nodes.filter((n) => n.id !== bn.id);
      body.edges = body.edges.filter((e) => e.source !== bn.id && e.target !== bn.id);
      changed();
      if (opts.refresh) opts.refresh();
    },
  }));
}

function bodyVars(forNode) {
  const out = [];
  for (const n of forNode.config.body.nodes) {
    if (n.type === "code") {
      for (const s of n.config.outputs || []) {
        if (s.name) out.push({ name: s.name, type: s.type, from: n.id });
      }
    } else if (n.type === "aggregate") {
      out.push({ name: autoOutputName(n), type: n.config?.output_type || "string", from: n.id });
    }
  }
  return out;
}

function bodyUpstreamIds(bodyGraph, targetId) {
  if (!targetId) return null;
  const reverse = new Map();
  for (const edge of bodyGraph.edges || []) {
    if (edge.source_handle === "retry") continue;
    if (!reverse.has(edge.target)) reverse.set(edge.target, []);
    reverse.get(edge.target).push(edge.source);
  }
  const seen = new Set();
  const queue = [...(reverse.get(targetId) || [])];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    queue.push(...(reverse.get(id) || []));
  }
  return seen;
}

function bodyOutMap(bodyGraph) {
  const map = new Map();
  for (const edge of bodyGraph.edges || []) {
    if (edge.source_handle === "retry") continue;
    if (!map.has(edge.source)) map.set(edge.source, []);
    map.get(edge.source).push(edge);
  }
  return map;
}

function bodyCanReach(fromId, targetId, map, seen = new Set()) {
  if (!fromId || seen.has(fromId)) return false;
  if (fromId === targetId) return true;
  seen.add(fromId);
  for (const edge of map.get(fromId) || []) {
    if (bodyCanReach(edge.target, targetId, map, seen)) return true;
  }
  return false;
}

function bodyBranchGuaranteed(bodyGraph, ownerId, targetId) {
  if (!targetId || !ownerId) return true;
  const map = bodyOutMap(bodyGraph);
  for (const ifNode of (bodyGraph.nodes || []).filter((n) => n.type === "if")) {
    const branches = (map.get(ifNode.id) || [])
      .filter((edge) => edge.source_handle === "else" ||
        String(edge.source_handle || "").startsWith("if"));
    const branchesToTarget = branches.filter((edge) =>
      bodyCanReach(edge.target, targetId, map));
    if (branchesToTarget.length < 2) continue;
    const ownerBranches = branchesToTarget.filter((edge) =>
      bodyCanReach(edge.target, ownerId, map));
    if (ownerBranches.length > 0 &&
        ownerBranches.length < branchesToTarget.length) {
      return false;
    }
  }
  return true;
}

function bodyAvailableVariables(forNode, targetId, extra = []) {
  const body = forNode.config.body || { nodes: [], edges: [] };
  const upstream = bodyUpstreamIds(body, targetId);
  const vars = [];
  const seen = new Set();
  const push = (v) => {
    if (!v?.name || seen.has(v.name)) return;
    vars.push(v);
    seen.add(v.name);
  };
  availableVariables(forNode.id, extra).forEach(push);
  for (const n of body.nodes || []) {
    if (n.id === targetId) continue;
    if (upstream && !upstream.has(n.id)) continue;
    if (!bodyBranchGuaranteed(body, n.id, targetId)) continue;
    if (n.type === "code") {
      for (const s of n.config.outputs || []) {
        if (s.name) push({ name: s.name, type: s.type, from: n.id });
      }
    } else if (n.type === "aggregate") {
      push({ name: autoOutputName(n), type: n.config?.output_type || "string", from: n.id });
    }
  }
  return vars;
}

/* ---------------- Aggregate / End ---------------- */
function nodeOutputVariables(node) {
  const cfg = node.config || {};
  if (node.type === "start") {
    return (cfg.inputs || []).filter((spec) => spec.name).map((spec) => ({
      name: spec.name, type: spec.type || "string",
    }));
  }
  if (node.type === "code") {
    return (cfg.outputs || []).filter((spec) => spec.name).map((spec) => ({
      name: spec.name, type: spec.type || "string",
    }));
  }
  if (node.type === "for") {
    return [{ name: autoOutputName(node), type: "list" }];
  }
  if (node.type === "aggregate") {
    return [{ name: autoOutputName(node), type: cfg.output_type || "string" }];
  }
  return [];
}

function directAggregateVariables(node, graph) {
  const byId = new Map((graph.nodes || []).map((item) => [item.id, item]));
  const seen = new Set();
  const variables = [];
  for (const edge of graph.edges || []) {
    if (edge.target !== node.id || edge.source_handle === "retry") continue;
    const source = byId.get(edge.source);
    if (!source) continue;
    for (const variable of nodeOutputVariables(source)) {
      if (seen.has(variable.name)) continue;
      seen.add(variable.name);
      variables.push({
        ...variable,
        from: source.name || source.id,
      });
    }
  }
  return variables;
}

function aggregateEditorFields(node, graph, onChange, onPortsChanged = null) {
  const cfg = node.config;
  if (!["string", "int", "float", "list", "dict"].includes(cfg.output_type)) {
    cfg.output_type = "string";
  }
  if (!Array.isArray(cfg.inputs)) cfg.inputs = [];
  cfg.inputs = cfg.inputs.map((raw) => typeof raw === "string"
    ? { source: raw, type: cfg.output_type }
    : { source: raw?.source || raw?.name || "", type: raw?.type || cfg.output_type });

  const direct = directAggregateVariables(node, graph);
  if (cfg.input_mode === "legacy") {
    cfg.inputs = direct.filter((item) => item.type === cfg.output_type)
      .map((item) => ({ source: item.name, type: item.type }));
    cfg.input_mode = "explicit";
    onChange();
  }
  if (!cfg.input_mode) cfg.input_mode = "explicit";

  const inputWrap = el("div", { class: "kv-list" });
  const typeSelect = el("select", { class: "input" });
  for (const type of ["string", "int", "float", "list", "dict"]) {
    typeSelect.append(el("option", { value: type, text: type }));
  }
  typeSelect.value = cfg.output_type;

  const renderInputs = () => {
    inputWrap.innerHTML = "";
    const candidates = direct.filter((item) => item.type === cfg.output_type);
    const used = new Set(cfg.inputs.map((item) => item.source).filter(Boolean));
    cfg.inputs.forEach((spec, index) => {
      const select = el("select", { class: "input mono" },
        el("option", { value: "", text: "选择变量" }));
      for (const item of candidates) {
        const option = el("option", {
          value: item.name,
          text: `${item.name} (${item.from})`,
        });
        option.disabled = item.name !== spec.source && used.has(item.name);
        select.append(option);
      }
      select.value = spec.source || "";
      select.addEventListener("change", () => {
        spec.source = select.value;
        spec.type = cfg.output_type;
        onChange();
      });
      inputWrap.append(el("div", { class: "kv-row aggregate-input-row" },
        select,
        el("button", {
          class: "btn icon danger", text: "✕", title: "删除聚合输入变量",
          onclick: () => {
            cfg.inputs.splice(index, 1);
            renderInputs();
            onChange();
          },
        })));
    });
    if (!cfg.inputs.length) {
      inputWrap.append(el("div", { class: "field-hint",
        text: candidates.length
          ? "请选择参与聚合的直连上游变量。"
          : "尚无与此聚合节点直连且类型匹配的上游变量。" }));
    }
  };
  typeSelect.addEventListener("change", () => {
    cfg.output_type = typeSelect.value;
    cfg.inputs = [];
    renderInputs();
    onPortsChanged?.();
    onChange();
  });
  renderInputs();

  return [
    field("聚合类型", typeSelect, "先选择类型，再选择同类型的直连上游变量。"),
    field("输入变量", inputWrap,
      "仅可选择直接连接到此聚合节点的变量；未命中的分支会自动跳过。"),
    el("button", {
      class: "btn", text: "+ 添加输入变量",
      onclick: () => {
        const candidates = direct.filter((item) => item.type === cfg.output_type);
        if (!candidates.length) {
          toast("请先连接能输出该类型变量的上游节点", "error");
          return;
        }
        cfg.inputs.push({ source: "", type: cfg.output_type });
        renderInputs();
        onChange();
      },
    }),
    el("div", { class: "field-hint",
      text: `输出变量：${autoOutputName(node)}（${cfg.output_type}）` }),
  ];
}

function buildAggregate(node) {
  panelRoot.append(...aggregateEditorFields(node, state.workflow, () => changed()));
}

function buildEnd(node) {
  panelRoot.append(el("div", {
    class: "field-hint",
    text: "结束节点：任一结束节点被命中即终止整个工作流。运行结果可在运行页查看。",
  }));
}
