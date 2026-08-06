/* fsm to skill - 后端 API 封装 */
const BASE = "";

async function request(path, options = {}) {
  const resp = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
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

export const api = {
  listWorkflows: () => request("/api/workflows"),
  getWorkflow: (id) => request(`/api/workflows/${encodeURIComponent(id)}`),
  createWorkflow: (wf) =>
    request("/api/workflows", { method: "POST", body: JSON.stringify(wf) }),
  updateWorkflow: (id, wf) =>
    request(`/api/workflows/${encodeURIComponent(id)}`,
            { method: "PUT", body: JSON.stringify(wf) }),
  deleteWorkflow: (id) =>
    request(`/api/workflows/${encodeURIComponent(id)}`, { method: "DELETE" }),
  validateWorkflow: (wf) =>
    request("/api/workflows/validate", { method: "POST", body: JSON.stringify(wf) }),
  saveWorkflowFile: (workflow, path = "") =>
    request("/api/files/save-workflow", {
      method: "POST",
      body: JSON.stringify({ workflow, path }),
    }),
  openWorkflowFile: () =>
    request("/api/files/open-workflow", { method: "POST", body: "{}" }),
  exportSkill: (workflow, targetDir, overwrite) =>
    request("/api/export", {
      method: "POST",
      body: JSON.stringify({ workflow, target_dir: targetDir, overwrite }),
    }),
  debugNode: (workflow, nodeId, inputs) =>
    request("/api/debug/node", {
      method: "POST",
      body: JSON.stringify({ workflow, node_id: nodeId, inputs }),
    }),
  debugAgent: (taskId, prompt) =>
    request("/api/debug/agent", {
      method: "POST",
      body: JSON.stringify({ "task-id": taskId, task_id: taskId, prompt }),
    }),
  listRecords: () => request("/api/runs"),
  getRecord: (id) => request(`/api/runs/${encodeURIComponent(id)}`),
  getConfig: () => request("/api/config"),
  saveConfig: (cfg) =>
    request("/api/config", { method: "PUT", body: JSON.stringify(cfg) }),
  testConfig: (cfg) =>
    request("/api/config/test", { method: "POST", body: JSON.stringify(cfg) }),
};

/** SSE 运行：fetch + ReadableStream 解析，事件回调。返回 {done, abort} */
export function runWorkflowSSE(workflow, inputs, stream, handlers) {
  const controller = new AbortController();
  const done = (async () => {
    const resp = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ workflow, inputs, stream }),
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) {
      let data = null;
      try { data = await resp.json(); } catch { /* ignore */ }
      const err = new Error(
        (data && data.detail && (data.detail.message || data.detail)) ||
        `运行请求失败 (${resp.status})`);
      err.status = resp.status;
      err.data = data;
      throw err;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done: finished } = await reader.read();
      if (finished) break;
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
          if (handlers.onEvent) handlers.onEvent(evt);
          if (evt.event === "llm_token" && handlers.onToken) {
            handlers.onToken(evt.node_id, evt.token);
          }
        }
      }
    }
  })();
  return { done, abort: () => controller.abort() };
}
