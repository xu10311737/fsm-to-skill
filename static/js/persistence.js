import { api } from "./api.js?v=20260730-agent-debug";
import { isDirty, markSaved, serializeWorkflow, state } from "./store.js";
import { toast } from "./ui.js";

export async function saveWorkflowToFile({ silent = false, forceDialog = false } = {}) {
  const path = forceDialog ? "" : (state.savedPath || "");
  const resp = await api.saveWorkflowFile(serializeWorkflow(), path);
  if (resp?.cancelled) return false;
  markSaved(state.workflowId, resp?.path);
  if (!silent) toast(`已保存到 ${resp.path || "本地文件"}`, "success");
  return true;
}

export async function ensureWorkflowSaved(reason = "操作前") {
  if (!isDirty() && state.savedPath) return true;
  try {
    const ok = await saveWorkflowToFile({
      silent: true,
      forceDialog: !state.savedPath,
    });
    if (ok) toast(`${reason}已保存`, "success");
    return ok;
  } catch (err) {
    toast(`保存失败：${err.data?.detail?.message || err.message}`, "error");
    return false;
  }
}
