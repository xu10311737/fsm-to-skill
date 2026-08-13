/**
 * pi-agent 驱动脚本：用 pi（earendil-works）模拟「外部 Agent」与 fsm-to-skill 交互。
 *
 * 用法：
 *   node driver.mjs \
 *     --task-id <task-id> \
 *     --cwd <fsm-backend 根目录> \
 *     --workdir <task 工作区目录，默认为 <cwd>/data/runtime/<safe_task_id>> \
 *     --first-prompt-b64 <首个 prompt 的 base64> \
 *     [--data-dir <data 目录，默认 <cwd>/data>] \
 *     [--max-rounds 30] \
 *     [--python python] \
 *     [--thinking low]
 *
 * 前置：本机已配置 pi 的模型供应商，即存在 ~/.pi/agent/models.json 与 settings.json。
 *
 * 输出：stdout 每行一个 JSON 对象，供后端解析后经 SSE 转发给前端：
 *   { "type": "info", ... } / { "type": "model", ... } / { "type": "round", ... }
 *   { "type": "agent_thinking", ... } / { "type": "agent_text", ... }（每轮模型思考与回复）
 *   { "type": "fsm_cmd", ... } / { "type": "agent_tool_result", ... }（每个 fsm step 的返回输出）
 *   { "type": "agent_tool", ... } / { "type": "agent_end", ... }
 *   { "type": "token", ... } / { "type": "done", ... } / { "type": "error", ... } / { "type": "exit" }
 */
import { createAgentSession, defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const execFileP = promisify(execFile);

function getArg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const taskId = getArg("--task-id");
const backendCwd = getArg("--cwd") || process.cwd();
const dataDir = getArg("--data-dir") || join(backendCwd, "data");
const explicitWorkdir = getArg("--workdir");
// task 默认工作区：data/runtime/<safe_task_id>
const workdir = explicitWorkdir || join(dataDir, "runtime", String(taskId).replace(/[^0-9A-Za-z_.-]+/g, "-"));
const maxRounds = parseInt(getArg("--max-rounds") || "30", 10);
const pythonCmd = getArg("--python") || "python";
const thinkingLevel = getArg("--thinking") || "low";
const firstPromptB64 = getArg("--first-prompt-b64");

function emitLine(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function readTask() {
  try {
    return JSON.parse(readFileSync(join(dataDir, "tasks", `${taskId}.json`), "utf-8"));
  } catch {
    return null;
  }
}

// 透传任务当前的最新 node-statuses 与 waiting prompt，供前端联动流程图与调试面板。
function emitTaskStatus() {
  const t = readTask();
  if (t && t["node-statuses"] && typeof t["node-statuses"] === "object") {
    emitLine({ type: "node_statuses", taskId, statuses: t["node-statuses"] });
  }
  if (t && t["waiting-node"] && t["last-prompt"]) {
    emitLine({ type: "prompt", nodeId: t["waiting-node"], content: t["last-prompt"] });
  }
}

if (!taskId) {
  emitLine({ type: "error", msg: "缺少参数 --task-id" });
  process.exit(1);
}

let taskFinished = false;
let lastStepId = null;
let ranFsmStepThisRound = false;

const runFsmStep = defineTool({
  name: "run_fsm_step",
  label: "Run FSM Step",
  description:
    "执行 fsm-to-skill 工作流的下一步。传入 task-id、要进入的 Code 节点 step-id（必须从上一条消息中的『下一个 step 待执行命令』模板读取），以及你推理出的真实参数 step-param（JSON 对象）。" +
    "工具返回引擎输出：若返回新的 prompt（waiting）说明任务仍在继续，等待下一步指令；若返回『任务已完成』说明任务结束。任务结束后本工具将不再真正执行，直接返回『任务已完成』。",
  promptSnippet: "run_fsm_step(taskId, stepId, stepParam) - 执行 fsm 工作流的下一步",
  promptGuidelines: [
    "step-id 必须取自上一条消息『下一个 step 待执行命令』模板中的 step-id，不要臆造。",
    "step-param 必须根据上一条消息中的参数说明（类型为 <type> 或 arg 描述）推理出真实的 JSON 值，绝不要用 <type> 占位符本身。",
    "工具返回内容若包含『任务已完成』，说明任务结束，不要再调用；否则等待下一条指令。",
  ],
  parameters: Type.Object({
    taskId: Type.String(),
    stepId: Type.String(),
    stepParam: Type.Record(Type.String(), Type.Any()),
  }),
  execute: async (_toolCallId, params) => {
    // 任务已完成：任何后续调用都短路返回，不再真正执行，避免重放已完成节点造成无限循环。
    if (taskFinished) {
      return {
        content: [
          { type: "text", text: "任务已完成，工作流已结束。无需再执行任何 step，请直接结束并总结。" },
        ],
      };
    }
    const nowTask = readTask();
    if (nowTask && nowTask.finished) {
      taskFinished = true;
      return {
        content: [
          { type: "text", text: "任务已完成，工作流已结束。无需再执行任何 step，请直接结束并总结。" },
        ],
      };
    }
    // 防重放：同一 step 在任务未推进的情况下被重复调用时直接短路，
    // 避免模型在单轮 waitForIdle 内反复执行同一节点造成无限循环 / 重复副作用。
    if (lastStepId && params.stepId === lastStepId) {
      return {
        content: [
          { type: "text", text: `step ${params.stepId} 已在本轮执行过，任务仍停留在同一等待点。请勿重复调用 run_fsm_step，直接结束本轮并等待下一条指令。` },
        ],
      };
    }
    lastStepId = params.stepId;
    ranFsmStepThisRound = true;
    const json = JSON.stringify(params.stepParam);
    const b64 = Buffer.from(json, "utf-8").toString("base64");
    emitLine({
      type: "fsm_cmd",
      taskId: params.taskId,
      stepId: params.stepId,
      stepParam: params.stepParam,
    });
    try {
      const { stdout, stderr } = await execFileP(
        pythonCmd,
        ["main.py", "--task-id", params.taskId, "--step-id", params.stepId, "--step-param-b64", b64],
        { cwd: backendCwd, maxBuffer: 10 * 1024 * 1024, timeout: 300000 }
      );
      if (stderr && stderr.trim()) emitLine({ type: "fsm_stderr", stderr: stderr.slice(0, 2000) });
      emitTaskStatus();
      const text = stdout.length > 12000 ? stdout.slice(0, 12000) + "\n...[输出已截断]" : stdout;
      return { content: [{ type: "text", text }] };
    } catch (e) {
      emitLine({ type: "fsm_error", err: `${e.stderr || e.message}`.slice(0, 2000) });
      return { content: [{ type: "text", text: `run_fsm_step 执行失败: ${e.stderr || e.message}` }] };
    }
  },
});

function buildInstruction(prompt) {
  return [
    "你是 fsm-to-skill 工作流的外部 Agent 执行者。以下是引擎暂停时给你的 prompt，其中包含『下一个 step 待执行命令』模板和参数说明。",
    "",
    "请按以下规则执行：",
    "1. 阅读 prompt，从『下一个 step 待执行命令』模板中取出 step-id。",
    "2. 根据 prompt 中的参数说明（<type> 或 arg 描述），推理出 step-param 的真实 JSON 值（禁止使用 <type> 占位符）。",
    "3. 调用 run_fsm_step 工具执行该 step。",
    "4. 观察工具返回：",
    "   - 若返回内容包含『任务已完成』：任务已彻底结束，立即停止，禁止再次调用 run_fsm_step，用一句话总结结果即可。",
    "   - 若返回的是新的 prompt（等待下一个 step）：本步已完成，等待下一条指令即可，不要重复调用。",
    "",
    "---BEGIN PROMPT---",
    prompt,
    "---END PROMPT---",
  ].join("\n");
}

async function main() {
  const firstPrompt = firstPromptB64
    ? Buffer.from(firstPromptB64, "base64").toString("utf-8")
    : "";

  emitLine({ type: "info", msg: `pi-agent driver 启动: task=${taskId} workdir=${workdir}` });

  let session;
  try {
    const result = await createAgentSession({
      cwd: workdir,
      agentDir: undefined, // 默认 ~/.pi/agent
      noTools: "builtin",
      customTools: [runFsmStep],
      thinkingLevel,
    });
    session = result.session;
  } catch (e) {
    emitLine({
      type: "error",
      msg: `createAgentSession 失败: ${e.message}`,
      hint: "请先配置 pi：npm i -g @earendil-works/pi-coding-agent 并运行 pi setup，或手动创建 ~/.pi/agent/models.json 与 settings.json",
    });
    process.exit(1);
  }

  const model = session.model;
  emitLine({
    type: "model",
    name: model?.info?.name,
    provider: model?.info?.provider,
    id: model?.info?.id,
  });

  let accThinking = "";
  let accText = "";
  session.subscribe((event) => {
    if (event.type === "message_update") {
      const ame = event.assistantMessageEvent;
      const kind = ame?.type || "";
      if (kind === "thinking_delta") {
        accThinking += ame.delta || "";
      } else if (kind === "text_delta") {
        accText += ame.delta || "";
      } else if (kind === "thinking_end") {
        if (accThinking.trim()) {
          emitLine({ type: "agent_thinking", round: rounds + 1, content: accThinking.trim() });
        }
        accThinking = "";
      } else if (kind === "text_end") {
        if (accText.trim()) {
          emitLine({ type: "agent_text", round: rounds + 1, content: accText.trim() });
        }
        accText = "";
      }
    }

    if (event.type === "tool_execution_start") {
      emitLine({ type: "agent_tool", name: event?.toolCall?.name });
    }
    if (event.type === "tool_execution_end") {
      const res = event.result;
      const out = res && (res.content ?? res.output ?? res.result);
      let text;
      if (typeof out === "string") {
        text = out;
      } else if (Array.isArray(out)) {
        text = out.map((b) => (b && b.type === "text" ? b.text : "")).filter(Boolean).join("\n");
      } else {
        text = JSON.stringify(out ?? res ?? "");
      }
      emitLine({ type: "agent_tool_result", name: event?.toolName, stepId: lastStepId, content: text.slice(0, 4000) });
    }
    if (event.type === "agent_end") {
      emitLine({ type: "agent_end" });
    }
  });

  let rounds = 0;
  let prompt = firstPrompt;

  while (rounds < maxRounds) {
    ranFsmStepThisRound = false;
    const task = readTask();
    if (task && task.finished) {
      emitLine({ type: "done", status: "completed", taskId });
      break;
    }
    emitTaskStatus();
    const currentPrompt = (task && task["last-prompt"]) || prompt;
    if (!currentPrompt) {
      emitLine({ type: "error", msg: "没有可用的 prompt（任务可能未处于 waiting 状态）" });
      break;
    }
    rounds++;
    emitLine({ type: "round", n: rounds, taskId });
    // 首个 prompt（task 尚无 last-prompt）不会经 emitTaskStatus 发射，
    // 这里主动发射一次，保证调试面板展示输入给 agent 的初始内容。
    if (!task || !task["last-prompt"]) {
      emitLine({ type: "prompt", nodeId: task && task["waiting-node"], content: currentPrompt });
    }
    try {
      await session.prompt(buildInstruction(currentPrompt));
      await session.waitForIdle();
    } catch (e) {
      emitLine({ type: "error", msg: `本轮 prompt 失败: ${e.message}` });
      break;
    }
    // 工具层已检测到任务完成：立即结束，不再进入下一轮。
    if (taskFinished) {
      emitLine({ type: "done", status: "completed", taskId });
      break;
    }
    const stats = session.getSessionStats();
    emitLine({ type: "token", tokens: stats.tokens, cost: stats.cost });
    let checked = readTask();
    if (checked && checked.finished) {
      emitLine({ type: "done", status: "completed", taskId });
      break;
    }
    // agent 本轮没有调用任何 run_fsm_step（例如收到 terminal prompt 后不再调用工具），
    // 说明 agent 认为任务已结束。此时触发后端最终化（把 waiting 的 terminal prompt 标记完成），
    // 否则 task.finished 永远不会置 true，driver 会陷入死循环。
    if (!ranFsmStepThisRound) {
      try {
        await execFileP(pythonCmd, ["main.py", "--task-id", taskId, "--finalize"], {
          cwd: backendCwd,
          timeout: 30000,
        });
      } catch (fe) {
        emitLine({ type: "fsm_stderr", stderr: `finalize 失败: ${fe.message}` });
      }
      emitTaskStatus();
      checked = readTask();
      if (checked && checked.finished) {
        emitLine({ type: "done", status: "completed", taskId });
        break;
      }
      // 若最终化后仍未完成，说明并非 terminal prompt，但 agent 已停止调用工具，
      // 继续等待会死循环，因此以「已完成」结束本轮。
      emitLine({ type: "done", status: "completed", taskId });
      break;
    }
  }

  if (rounds >= maxRounds) {
    emitLine({ type: "error", msg: `达到最大轮次 ${maxRounds}，任务可能未完成` });
  }

  const finalStats = session.getSessionStats();
  emitLine({
    type: "token",
    tokens: finalStats.tokens,
    cost: finalStats.cost,
    userMessages: finalStats.userMessages,
    assistantMessages: finalStats.assistantMessages,
    toolCalls: finalStats.toolCalls,
  });

  try {
    session.dispose();
  } catch {
    /* ignore */
  }
  emitLine({ type: "exit" });
}

main().catch((e) => {
  emitLine({ type: "error", msg: `driver 异常: ${e.stack || e.message}` });
  process.exit(1);
});