/* fsm to skill - run/debug page */
import { state, serializeWorkflow, getNode, changed, autoOutputName } from "./store.js";
import { api, runWorkflowSSE } from "./api.js?v=20260730-agent-debug";
import { el, toast, parseTypedValue } from "./ui.js";
import { Canvas } from "./canvas.js";

let runCanvas = null;
let session = null;
let agentAbort = null;
let chatBox, inputsWrap, statsBox, detailBox, summaryLine;
let userMessageInput;
let tokenBuffers = {};
let runSessionId = null;
let eventLog = [];
let lastUserInput = "";

export function initRun() {
  chatBox = document.getElementById("chat");
  inputsWrap = document.getElementById("run-inputs");
  statsBox = document.getElementById("run-stats");
  detailBox = document.getElementById("node-detail");
  summaryLine = document.getElementById("run-summary-line");
  userMessageInput = document.getElementById("run-user-input");

  const wrap = document.getElementById("run-canvas-wrap");
  runCanvas = new Canvas(wrap, state.workflow, {
    readOnly: true,
    onSelect: (id) => renderNodeDetail(id),
  });

  document.getElementById("btn-exec").addEventListener("click", startRun);
  document.getElementById("btn-stop").addEventListener("click", stopRun);
  document.getElementById("btn-export-record")
    .addEventListener("click", exportRecord);
  document.getElementById("btn-export-record").disabled = !state.lastResult;
  userMessageInput?.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") startRun();
  });
}

export function syncRunPage() {
  runCanvas.setGraph(state.workflow);
  runCanvas.setStatuses(state.nodeStatuses);
  renderRunContext();
  if (!state.lastResult) {
    statsBox.innerHTML = '<div class="placeholder">尚未运行</div>';
    updateSummaryLine(null);
  } else {
    renderStats(state.lastResult);
    updateSummaryLine(state.lastResult);
  }
  document.getElementById("btn-export-record").disabled = !state.lastResult;
}

function renderRunContext() {
  inputsWrap.innerHTML = "";
  const start = state.workflow.nodes.find((n) => n.type === "start");
  const specs = (start?.config?.inputs || []).filter((s) => s.name);
  inputsWrap.append(el("div", { class: "run-context-card" },
    el("span", { class: "run-context-label", text: "task-id" }),
    el("span", { class: "run-context-value mono", text: currentSessionId() })));
  if (!specs.length) {
    inputsWrap.append(el("div", { class: "run-context-note",
      text: "当前无用户新增初始变量" }));
    return;
  }
  for (const spec of specs) {
    const remove = el("button", {
      class: "run-var-delete",
      title: "删除变量",
      type: "button",
      text: "×",
      onclick: (ev) => {
        ev.stopPropagation();
        removeInitialVariable(spec.name);
      },
    });
    inputsWrap.append(el("div", { class: "run-context-card" },
      el("span", { class: "run-context-label",
        text: `${spec.name} · ${spec.type || "string"}` }),
      el("span", { class: "run-context-value mono",
        text: formatValue(spec.default ?? "") || "空" }),
      remove));
  }
}

function collectInputs() {
  const message = (userMessageInput?.value || "").trim();
  if (!message) throw new Error("请输入消息");
  lastUserInput = message;
  const out = {
    "task-id": currentSessionId(),
  };
  const start = state.workflow.nodes.find((n) => n.type === "start");
  for (const spec of (start?.config?.inputs || [])) {
    if (!spec.name) continue;
    out[spec.name] = parseTypedValue(spec.type || "string", spec.default ?? "");
  }
  return out;
}

function removeInitialVariable(name) {
  const start = state.workflow.nodes.find((n) => n.type === "start");
  if (!start) return;
  start.config.inputs = (start.config.inputs || [])
    .filter((spec) => spec.name !== name);
  changed();
  renderRunContext();
  toast(`已删除初始变量：${name}`, "success");
}

function currentSessionId() {
  if (!runSessionId) {
    runSessionId = `task-${Date.now().toString(36)}-${Math.random()
      .toString(36).slice(2, 8)}`;
  }
  return runSessionId;
}

async function startRun() {
  if (state.running) return;
  let inputs;
  try {
    inputs = collectInputs();
  } catch (err) {
    toast(err.message, "error");
    return;
  }
  try {
    const rep = await fetchValidate();
    state.validation = rep;
    if ((rep.errors || []).length) {
      toast(`存在 ${rep.errors.length} 个校验错误，请先修复`, "error", 4500);
      document.getElementById("btn-validate")?.classList.add("has-error");
      renderValidationErrors(rep.errors || []);
      return;
    }
  } catch (err) {
    toast(`校验失败：${err.message}`, "error");
    return;
  }

  resetRunView();
  setRunning(true);
  addChat("prompt", lastUserInput, null, "输入");
  addChat("system", `开始运行，task-id: ${inputs["task-id"]}`);

  session = runWorkflowSSE(serializeWorkflow(), inputs, false, {
    onEvent: handleEvent,
  });
  try {
    await session.done;
    if (state.lastResult?.status === "waiting") {
      await callDebugAgent(state.lastResult);
    }
  } catch (err) {
    if (err.name !== "AbortError") {
      addChat("system", `运行请求失败：${err.message}`, "error");
      toast(err.message, "error");
    }
  } finally {
    setRunning(false);
    session = null;
  }
}

async function fetchValidate() {
  const resp = await fetch("/api/workflows/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(serializeWorkflow()),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function callDebugAgent(result) {
  const taskId = result["task-id"] || result.task_id ||
    result.variables?.["task-id"]?.value;
  result.user_input = lastUserInput;
  let prompt = result.waiting_prompt;
  if (!prompt || !taskId) return;
  addChat("system", "Prompt 已发送给大模型，等待 Agent 执行");
  updateSummaryLine({ running: true, agent: true });
  // agent 调试阶段必须显式显示停止按钮（startRun 的 setRunning(true) 在 workflow
  // 进入 waiting/结束后可能已失效，导致 btn-stop 一直隐藏、无法中止）。
  setRunning(true);
  agentAbort = new AbortController();
  try {
    const responses = [];
    const seenPrompts = new Set();
    let messages = [];
    for (let round = 0; round < 20; round++) {
      const modelPrompt = agentPromptForRound(lastUserInput, prompt, round);
      result.agent_prompt = modelPrompt;
      if (seenPrompts.has(modelPrompt)) {
        addChat("system", "Agent 输出重复 Prompt，流程已停止以避免循环", "error");
        break;
      }
      seenPrompts.add(modelPrompt);
      const resp = await streamDebugAgent(taskId, modelPrompt, messages,
        agentAbort.signal);
      if (Array.isArray(resp.messages) && resp.messages.length) {
        messages = resp.messages;
      }
      responses.push(resp);
      state.lastResult.agent_response = resp;
      state.lastResult.agent_responses = responses;
      if (!resp.handoff) {
        addChat("llm", resp.content || "大模型未返回文本", null,
          `大模型回复 ${round + 1}`);
      }
      const taskState = resp.task_state || {};
      syncAgentTaskState(taskState);
      state.lastResult.agent_duration_ms = agentDurationMs(state.lastResult);
      if (taskState.finished) {
        state.lastResult.status = "success";
        state.lastResult.waiting_node = null;
        state.lastResult.waiting_prompt = null;
        addChat("system", "Agent task 已完成", "ok");
        break;
      }
      const nextPrompt = taskState["last-prompt"];
      if (!nextPrompt || nextPrompt === prompt) {
        if (taskState.variables) {
          addChat("system", "Agent task 状态已更新", "ok");
        }
        break;
      }
      prompt = nextPrompt;
      state.lastResult.status = "waiting";
      state.lastResult.waiting_node = taskState["waiting-node"] || null;
      state.lastResult.waiting_prompt = prompt;
      addChat("prompt", agentPromptForRound(lastUserInput, prompt, round + 1), null,
        `脚本输出的 Prompt (${state.lastResult.waiting_node || "等待节点"})`);
    }
    renderStats(state.lastResult);
    updateSummaryLine(state.lastResult);
  } catch (err) {
    if (err.name !== "AbortError") {
      addChat("system", `大模型调用失败：${err.message}`, "error");
      toast(err.message, "error");
    }
  } finally {
    agentAbort = null;
  }
}

function syncAgentTaskState(taskState) {
  if (!taskState) return;
  if (taskState.variables) {
    state.lastResult.variables = Object.fromEntries(
      Object.entries(taskState.variables).map(([name, value]) => [
        name,
        { type: inferType(value), value, owner: "agent" },
      ]));
  }
  const statuses = taskState["node-statuses"] || taskState.node_statuses;
  if (statuses && typeof statuses === "object") {
    state.nodeStatuses = { ...state.nodeStatuses, ...statuses };
    runCanvas.setStatuses(state.nodeStatuses);
  }
}

function inferType(value) {
  if (Array.isArray(value)) return "list";
  if (value === null) return "string";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  if (typeof value === "object") return "dict";
  return "string";
}

function composeFirstAgentPrompt(userInput, prompt) {
  const left = String(userInput || "").trim();
  const right = String(prompt || "").trim();
  if (!left) return right;
  if (!right) return left;
  return `${left}\n\n${right}`;
}

function agentPromptForRound(userInput, prompt, round) {
  const text = String(prompt || "").trim();
  return round === 0 ? composeFirstAgentPrompt(userInput, text) : text;
}

async function postDebugAgent(taskId, prompt, messages, signal) {
  if (api && typeof api.debugAgent === "function") {
    return api.debugAgent(taskId, prompt, messages);
  }
  const resp = await fetch("/api/debug/agent", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      { "task-id": taskId, task_id: taskId, prompt, messages }),
    signal,
  });
  const text = await resp.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!resp.ok) {
    const err = new Error(
      (data && data.detail && (data.detail.message || data.detail)) ||
      resp.statusText);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

async function streamDebugAgent(taskId, prompt, messages, signal) {
  const completedDuration = agentDurationMs(state.lastResult || {});
  const resp = await fetch("/api/debug/agent/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(
      { "task-id": taskId, task_id: taskId, prompt, messages }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    return postDebugAgent(taskId, prompt, messages, signal);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResp = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop();
    for (const chunk of chunks) {
      for (const line of chunk.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload) continue;
        let evt;
        try { evt = JSON.parse(payload); } catch { continue; }
        handleAgentStreamEvent(evt, completedDuration);
        if (evt.event === "agent_final") {
          finalResp = evt.response || {};
          finalResp.handoff = !!(evt.handoff || finalResp.handoff);
          finalResp.duration_ms = evt.duration_ms;
          finalResp.task_state = evt.task_state;
        }
      }
    }
  }
  if (!finalResp) throw new Error("Agent 流式响应未返回最终结果");
  return finalResp;
}

function handleAgentStreamEvent(evt, completedDuration) {
  const duration = Number(evt.duration_ms || 0);
  state.lastResult.agent_duration_ms = completedDuration +
    (Number.isFinite(duration) ? duration : 0);
  if (evt.task_state) syncAgentTaskState(evt.task_state);
  if (evt.event === "agent_model" && evt.item) {
    renderAgentTrace({ trace: [evt.item] });
  } else if (evt.event === "agent_shell" && evt.item) {
    renderAgentTrace({ trace: [evt.item] });
  } else if (evt.event === "agent_error") {
    const detail = evt.detail || {};
    throw new Error(detail.message || "Agent 流式调用失败");
  }
  renderStats(state.lastResult);
  updateSummaryLine(state.lastResult);
}

function formatToolResult(item) {
  let args = {};
  try { args = JSON.parse(item.arguments || "{}"); } catch { /* ignore */ }
  const res = item.result || {};
  const parts = [];
  parts.push(`tool: ${item.name || "shell"}`);
  if (args.path || res.path) parts.push(`path: ${args.path || res.path}`);
  if (args.command || res.command) {
    parts.push(`command:\n${args.command || res.command}`);
  }
  if (res.exit_code != null) parts.push(`exit_code: ${res.exit_code}`);
  if (res.content != null) parts.push(`content:\n${res.content}`);
  if (res.bytes_written != null) parts.push(`bytes_written: ${res.bytes_written}`);
  if (res.stdout) parts.push(`stdout:\n${res.stdout}`);
  if (res.stderr) parts.push(`stderr:\n${res.stderr}`);
  if (res.error) parts.push(`error:\n${res.error}`);
  return parts.join("\n\n");
}

function renderValidationErrors(errors) {
  chatBox.innerHTML = "";
  statsBox.innerHTML = '<div class="placeholder error-text">校验未通过</div>';
  updateSummaryLine(null);
  addChat("system", `存在 ${errors.length} 个校验错误，运行已停止`, "error");
  for (const item of errors.slice(0, 6)) {
    const where = item.node_id ? `${item.node_id} ` : "";
    addChat("system", `${where}${item.code || "ERROR"}：${item.message || ""}`, "error");
  }
}

function resetRunView() {
  state.nodeStatuses = Object.fromEntries(
    (state.workflow.nodes || []).map((node) => [node.id, "skipped"]));
  state.lastResult = null;
  tokenBuffers = {};
  eventLog = [];
  runCanvas.setStatuses(state.nodeStatuses);
  chatBox.innerHTML = "";
  detailBox.innerHTML = '<div class="placeholder">运行中：点击节点查看详情</div>';
  statsBox.innerHTML = '<div class="placeholder">运行中...</div>';
  updateSummaryLine({ running: true });
}

function stopRun() {
  if (session) session.abort();
  if (agentAbort) agentAbort.abort();
}

function setRunning(running) {
  state.running = running;
  document.getElementById("btn-exec").disabled = running;
  const stopBtn = document.getElementById("btn-stop");
  stopBtn.disabled = !running;
  stopBtn.classList.toggle("hidden", !running);
  const topRun = document.getElementById("btn-run-top");
  if (topRun) topRun.disabled = running;
}

function handleEvent(evt) {
  appendLog(evt);
  switch (evt.event) {
    case "node_started":
      state.nodeStatuses[evt.node_id] = "running";
      runCanvas.setStatuses(state.nodeStatuses);
      break;
    case "llm_token":
      appendStreamingToken(evt);
      break;
    case "node_finished":
      state.nodeStatuses[evt.node_id] = evt.status;
      runCanvas.setStatuses(state.nodeStatuses);
      break;
    case "workflow_finished":
      finishRun(evt);
      break;
  }
}

function appendStreamingToken(evt) {
  let bubble = tokenBuffers[evt.node_id];
  if (!bubble) {
    const node = getNode(evt.node_id);
    bubble = addChat("llm", "", null,
      node ? (node.name || evt.node_id) : evt.node_id);
    tokenBuffers[evt.node_id] = bubble;
  }
  bubble.textContent += evt.token;
  chatBox.scrollTop = chatBox.scrollHeight;
}

function finishRun(evt) {
  const result = evt.result;
  if (result) {
    const prev = state.lastResult;
    // workflow_finished 的结果不含 agent 响应，保留上一份 agent_responses/response，
    // 避免覆盖后缓存命中 token 等 usage 统计丢失。
    if (!result.agent_response && !result.agent_responses) {
      if (prev && prev.agent_responses) result.agent_responses = prev.agent_responses;
      else if (prev && prev.agent_response) result.agent_response = prev.agent_response;
    }
    state.lastResult = result;
    for (const [nid, rec] of Object.entries(result.node_records || {})) {
      state.nodeStatuses[nid] = rec.status;
    }
    if (result.status === "waiting" && result.waiting_node) {
      state.nodeStatuses[result.waiting_node] = "waiting";
    }
    runCanvas.setStatuses(state.nodeStatuses);
    renderConversation(result);
    renderStats(result);
    updateSummaryLine(result);
    appendVariableLog(result);
    document.getElementById("btn-export-record").disabled = false;
  }
  const status = result?.status || evt.status;
  const duration = elapsedSeconds(evt.total_duration_ms || result?.total_duration_ms);
  if (status === "waiting") {
    addChat("system",
      `已输出 Prompt，等待大模型输入（节点 ${result?.waiting_node || evt.waiting_node || "?"}，耗时 ${duration} s）`,
      "waiting");
  } else {
    addChat("system",
      status === "success"
        ? `运行完成，耗时 ${duration} s`
        : `运行失败（节点 ${evt.failed_node || result?.failed_node || "?"}）`,
      status === "success" ? "ok" : "error");
  }
}

function addChat(kind, text, extraClass, title) {
  const wrap = el("div", {
    class: `chat-msg ${kind}${extraClass ? " " + extraClass : ""}`,
  });
  if (title) {
    wrap.append(el("div", { class: "chat-msg-head" },
      el("span", { class: "chat-title", text: title }),
      el("span", { class: "chat-time", text: new Date().toLocaleTimeString() })));
  }
  const body = el("div", { class: "chat-body" });
  setChatBody(body, text, kind);
  wrap.append(body);
  chatBox.append(wrap);
  chatBox.scrollTop = chatBox.scrollHeight;
  return body;
}

function setChatBody(body, text, kind) {
  const value = text == null ? "" : String(text);
  if (kind === "llm" || kind === "thinking" || kind === "prompt") {
    body.innerHTML = markdownLite(value);
  } else {
    body.textContent = value;
  }
}

function renderConversation(result) {
  chatBox.innerHTML = "";
  const records = result.node_records || {};
  const userMessage = result.user_input || lastUserInput;
  let waitingPromptShown = false;
  if (userMessage) addChat("prompt", userMessage, null, "输入");
  if (result["task-id"]) addChat("system", `task-id: ${result["task-id"]}`);

  for (const node of state.workflow.nodes || []) {
    const rec = records[node.id];
    if (!rec || rec.status === "skipped") continue;
    const nodeLabel = node.name || node.id;
    if (rec.error_message) {
      addChat("system",
        `${nodeLabel}: ${rec.error_type || "Error"} ${rec.error_message}`,
        "error");
      continue;
    }
    if (node.type === "llm") {
      if (rec.prompt) {
        const shownPrompt = rec.prompt === result.waiting_prompt
          ? composeFirstAgentPrompt(userMessage, rec.prompt)
          : rec.prompt;
        addChat("prompt", shownPrompt, null, `脚本输出的 Prompt (${nodeLabel})`);
        if (rec.prompt === result.waiting_prompt) waitingPromptShown = true;
      }
      if (rec.thinking) {
        addChat("thinking", rec.thinking, null, "大模型思考过程");
      }
      if (rec.content !== undefined && rec.content !== null) {
        addChat("llm", rec.content, null, "大模型回复");
      }
    } else if (node.type === "code") {
      if (rec.result !== undefined) {
        addChat("code", JSON.stringify(rec.result, null, 2), null,
          `脚本运行结果 (${node.id})`);
      }
      if (rec.stdout || rec.stderr) {
        addChat("code",
          `stdout:\n${rec.stdout || "无"}\n\nstderr:\n${rec.stderr || "无"}`,
          null, `脚本输出 (${node.id})`);
      }
    } else if (node.type === "for" && rec.collect) {
      addChat("system", `${nodeLabel}: 循环收集变量 ${rec.collect}`, "ok");
    }
  }
  if (result.status === "waiting" && result.waiting_prompt && !waitingPromptShown) {
    addChat("prompt", composeFirstAgentPrompt(userMessage, result.waiting_prompt), null,
      `脚本输出的 Prompt (${result.waiting_node || "等待节点"})`);
  }
  // 重建对话时补渲染 agent 调试阶段的 trace（思考/工具调用/回复）。
  // 否则 workflow_finished 走 renderConversation 清空 chatBox 后，
  // 只剩 eventLog 有思考内容，对话里不显示。
  const agentResponses = Array.isArray(result.agent_responses) && result.agent_responses.length
    ? result.agent_responses
    : (result.agent_response ? [result.agent_response] : []);
  for (const resp of agentResponses) {
    if (resp && (resp.trace || resp.thinking)) renderAgentTrace(resp);
  }
  if (!chatBox.children.length) addChat("system", "暂无调试内容");
}

function renderStats(result) {
  statsBox.innerHTML = "";
  const records = Object.values(result.node_records || {});
  const metrics = tokenMetrics(records, agentUsages(result));
  const card = (label, value) => el("div", { class: "stat-card" },
    el("div", { class: "stat-value", text: String(value) }),
    el("div", { class: "stat-label", text: label }));
  statsBox.append(
    card("输入 token", metrics.hasInput ? metrics.input : "-"),
    card("输出 token", metrics.hasOutput ? metrics.output : "-"),
    card("缓存命中 token", metrics.hasCache ? metrics.cache : "-"),
    card("耗时 (s)", elapsedSeconds(totalDurationMs(result))),
  );
}

function tokenMetrics(records, agentUsageList) {
  const out = {
    input: 0,
    output: 0,
    cache: 0,
    hasInput: false,
    hasOutput: false,
    hasCache: false,
  };
  for (const r of records) {
    if (!r.usage) continue;
    addUsage(out, r.usage);
  }
  const usages = Array.isArray(agentUsageList)
    ? agentUsageList : (agentUsageList ? [agentUsageList] : []);
  for (const usage of usages) addUsage(out, usage);
  return out;
}

function agentUsages(result) {
  if (Array.isArray(result.agent_responses) && result.agent_responses.length) {
    return result.agent_responses.map((resp) => resp?.usage).filter(Boolean);
  }
  return result.agent_response?.usage ? [result.agent_response.usage] : [];
}

function agentDurationMs(result) {
  const responses = Array.isArray(result.agent_responses) &&
    result.agent_responses.length ? result.agent_responses
    : (result.agent_response ? [result.agent_response] : []);
  return responses.reduce((sum, resp) => {
    const value = Number(resp?.duration_ms || 0);
    return Number.isFinite(value) ? sum + value : sum;
  }, 0);
}

function totalDurationMs(result) {
  const base = Number(result?.total_duration_ms || 0);
  const agent = Number(result?.agent_duration_ms || agentDurationMs(result));
  return (Number.isFinite(base) ? base : 0) +
    (Number.isFinite(agent) ? agent : 0);
}

function addUsage(out, usage) {
  const input = usageNumber(usage, ["input_tokens", "prompt_tokens"]);
  const output = usageNumber(usage, ["output_tokens", "completion_tokens"]);
  const cache = usageNumber(usage, [
      "cached_tokens",
      "cache_hit_tokens",
      "prompt_cache_hit_tokens",
      "cached_input_tokens",
      "cache_read_input_tokens",
      "input_tokens_details.cached_tokens",
      "prompt_tokens_details.cached_tokens",
  ]);
  if (input != null) { out.input += input; out.hasInput = true; }
  if (output != null) { out.output += output; out.hasOutput = true; }
  if (cache != null) { out.cache += cache; out.hasCache = true; }
}

function usageNumber(usage, paths) {
  for (const path of paths) {
    const value = path.split(".").reduce((cur, key) =>
      cur == null ? undefined : cur[key], usage);
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

function updateSummaryLine(result) {
  if (!summaryLine) return;
  if (!result) {
    summaryLine.textContent = "等待运行";
    return;
  }
  if (result.running) {
    summaryLine.textContent = result.agent
      ? "正在等待大模型 / Agent 工具调用"
      : "正在运行调试流程";
    return;
  }
  if (result.agent_response) {
    summaryLine.textContent = "大模型回复已返回";
    return;
  }
  if (result.status === "waiting") {
    summaryLine.textContent = `已输出 Prompt，等待大模型输入：${result.waiting_node || "-"}`;
    return;
  }
  const records = Object.values(result.node_records || {});
  const metrics = tokenMetrics(records, agentUsages(result));
  summaryLine.textContent = [
    `输入token: ${metrics.hasInput ? metrics.input : "-"}`,
    `输出token: ${metrics.hasOutput ? metrics.output : "-"}`,
    `缓存命中token: ${metrics.hasCache ? metrics.cache : "-"}`,
    `耗时: ${elapsedSeconds(totalDurationMs(result))}s`,
  ].join("；");
}

function appendLog(evt) {
  if (evt.event === "llm_token") return;
  const line = logLine(evt);
  eventLog.push(line);
}

function renderAgentTrace(resp) {
  const trace = Array.isArray(resp.trace) && resp.trace.length
    ? resp.trace
    : legacyAgentTrace(resp);
  for (const item of trace) {
    if (item.type === "model") {
      const title = `模型回合 ${item.turn || ""}`.trim();
      const thinking = (item.thinking || "").trim();
      const content = (item.content || "").trim();
      if (thinking) {
        addChat("thinking", thinking, null, `${title} · 思考`);
        appendAgentLog("模型思考", thinking);
      } else if (content) {
        // 部分模型 reasoning_content 为空，推理文本在 content 里，同样作为
        // 思考渲染到对话区，保证"大模型思考"可见。
        addChat("thinking", content, null, `${title} · 思考`);
        appendAgentLog("模型思考", content);
      }
      if (content) {
        appendAgentLog(title, content);
      }
      const calls = item.tool_calls || [];
      if (calls.length) {
        appendAgentLog(title, `请求工具 ${calls.length} 次`);
      }
    } else if (item.type === "shell") {
      const text = formatToolResult(item);
      const name = item.name || "shell";
      addChat("code", text, null, `${name} 工具调用 ${item.turn ? `#${item.turn}` : ""}`);
      appendAgentLog(`${name} 工具调用`, text);
    }
  }
}

function legacyAgentTrace(resp) {
  const out = [];
  if (resp.thinking) out.push({ type: "model", turn: 1, thinking: resp.thinking });
  for (const item of resp.tool_results || []) {
    out.push({ type: "shell", turn: 1, ...item });
  }
  return out;
}

function appendAgentLog(kind, text) {
  const value = String(text || "").trim();
  if (!value) return;
  eventLog.push(`[${kind}]\n${value}`);
}

function logLine(evt) {
  if (evt.event === "node_started") return `节点开始：${evt.node_id}`;
  if (evt.event === "node_finished") {
    return `节点结束：${evt.node_id} -> ${evt.status}，耗时 ${Math.round(evt.duration_ms || 0)} ms`;
  }
  if (evt.event === "workflow_finished") {
    if (evt.status === "waiting") {
      return `工作流暂停：等待大模型输入，节点 ${evt.waiting_node || "?"}，耗时 ${elapsedSeconds(evt.total_duration_ms)} s`;
    }
    return `工作流结束：${evt.status}，耗时 ${elapsedSeconds(evt.total_duration_ms)} s`;
  }
  if (evt.event === "llm_token") return `LLM token：${evt.node_id}`;
  return JSON.stringify(evt);
}

function appendVariableLog(result) {
  const lines = variableChangeLines(result);
  if (!lines.length) return;
  for (const line of lines) {
    eventLog.push(`[变量变化]\n${line}`);
  }
}

function variableChangeLines(result) {
  const lines = [];
  const records = result.node_records || {};
  const vars = result.variables || {};
  for (const [nodeId, rec] of Object.entries(records)) {
    const node = getNode(nodeId);
    const label = node ? (node.name || nodeId) : nodeId;
    const outputs = rec.outputs && typeof rec.outputs === "object"
      ? rec.outputs : {};
    const pairs = Object.entries(outputs);
    if (pairs.length) {
      lines.push(`[${label}]`);
      for (const [name, value] of pairs) {
        lines.push(`  ${name} = ${logValue(value)}`);
      }
      continue;
    }
    const autoName = node && (node.type === "for" || node.type === "aggregate")
      ? autoOutputName(node) : null;
    if (autoName && vars[autoName]) {
      lines.push(`[${label}]`);
      lines.push(`  ${autoName} = ${logValue(vars[autoName].value)}`);
    }
  }
  const finalVars = Object.entries(vars);
  if (finalVars.length) {
    lines.push("[最终变量快照]");
    for (const [name, info] of finalVars) {
      lines.push(
        `  ${name} (${info.type || "-"}) <= ${info.owner || "-"}: ${logValue(info.value)}`);
    }
  }
  return lines;
}

function logValue(value) {
  const text = formatValue(value);
  return text.length > 700 ? `${text.slice(0, 700)}...` : text;
}

function renderNodeDetail(nodeId) {
  detailBox.innerHTML = "";
  if (!nodeId) {
    detailBox.innerHTML = '<div class="placeholder">点击节点查看详情</div>';
    return;
  }
  const node = getNode(nodeId);
  detailBox.append(el("div", { class: "panel-title",
    text: node ? `${node.name || nodeId}（${node.type}）` : nodeId }));
  const rec = state.lastResult?.node_records?.[nodeId];
  const live = state.nodeStatuses[nodeId];
  if (!rec && !live) {
    detailBox.append(el("div", { class: "muted", text: "该节点尚未执行" }));
    return;
  }
  const kv = (k, v) => detailBox.append(el("div", { class: "detail-row" },
    el("span", { class: "detail-key", text: k }),
    el("span", { class: "detail-val", text: v })));
  kv("状态", rec ? rec.status : live);
  if (rec) {
    kv("耗时", `${Math.round(rec.duration_ms || 0)} ms`);
    if (rec.condition_result !== undefined) kv("条件结果", String(rec.condition_result));
    if (rec.error_type) kv("错误类型", rec.error_type);
    if (rec.usage) kv("Token", JSON.stringify(rec.usage));
  }
  const pre = (label, text) => {
    if (text == null || text === "") return;
    detailBox.append(el("div", { class: "field-label", text: label }),
      el("pre", { class: "debug-pre",
        text: typeof text === "string" ? text : JSON.stringify(text, null, 2) }));
  };
  if (rec) {
    pre("错误信息", rec.error_message);
    pre("Prompt", rec.prompt);
    pre("思考过程", rec.thinking);
    pre("stdout", rec.stdout);
    pre("stderr", rec.stderr);
    pre("result", rec.result);
  }
  const vars = state.lastResult?.variables || {};
  const produced = Object.entries(vars).filter(([, v]) => v.owner === nodeId);
  if (produced.length) {
    detailBox.append(el("div", { class: "field-label", text: "产出变量" }));
    for (const [name, v] of produced) pre(`${name}（${v.type}）`, v.value);
  }
}

function exportRecord() {
  if (!state.lastResult) {
    toast("暂无日志可导出", "error");
    return;
  }
  const blob = new Blob([buildReadableLog()],
    { type: "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `debug-log-${Date.now()}.txt`;
  a.click();
  URL.revokeObjectURL(a.href);
}

function buildReadableLog() {
  const result = state.lastResult || {};
  const variableLines = variableChangeLines(result);
  const lines = [
    `工作流：${state.workflow.name || "未命名工作流"}`,
    `导出时间：${new Date().toLocaleString()}`,
    `task-id：${result["task-id"] || result.variables?.["task-id"]?.value || ""}`,
    `用户输入：${result.user_input || lastUserInput || ""}`,
    `状态：${result.status || ""}`,
    `等待节点：${result.waiting_node || ""}`,
    `耗时：${elapsedSeconds(totalDurationMs(result))} s`,
    "",
    "事件日志",
    "--------",
    ...(eventLog.length ? eventLog : ["暂无事件日志"]),
    "",
    "变量变化",
    "--------",
    ...(variableLines.length ? variableLines : ["暂无变量变化"]),
    "",
    "节点详情",
    "--------",
  ];
  for (const [nodeId, rec] of Object.entries(result.node_records || {})) {
    lines.push(`[${nodeId}] ${rec.status || ""} ${Math.round(rec.duration_ms || 0)} ms`);
    if (rec.prompt) lines.push("Prompt:", rec.prompt);
    if (rec.content) lines.push("大模型回复:", rec.content);
    if (rec.stdout) lines.push("stdout:", rec.stdout);
    if (rec.stderr) lines.push("stderr:", rec.stderr);
    if (rec.result !== undefined) lines.push("result:", JSON.stringify(rec.result, null, 2));
    lines.push("");
  }
  return lines.join("\n");
}

function markdownLite(text) {
  const escaped = escapeHtml(text);
  const lines = escaped.split(/\r?\n/);
  let inCode = false;
  const out = [];
  for (const line of lines) {
    if (line.startsWith("```")) {
      out.push(inCode ? "</code></pre>" : '<pre class="md-code"><code>');
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      out.push(`${line}\n`);
    } else if (line.startsWith("### ")) {
      out.push(`<div class="md-h3">${line.slice(4)}</div>`);
    } else if (line.startsWith("## ")) {
      out.push(`<div class="md-h2">${line.slice(3)}</div>`);
    } else if (/^[-*]\s+/.test(line)) {
      out.push(`<div class="md-li">${line.replace(/^[-*]\s+/, "")}</div>`);
    } else if (line.trim() === "") {
      out.push("<br>");
    } else {
      out.push(`<div>${line}</div>`);
    }
  }
  if (inCode) out.push("</code></pre>");
  return out.join("");
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatValue(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function elapsedSeconds(ms) {
  return ((ms || 0) / 1000).toFixed(2);
}
