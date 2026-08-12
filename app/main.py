"""FastAPI 应用入口（PRD 第 9 章 API 契约）。

端点：工作流 CRUD / 校验 / 运行（SSE）/ 单节点调试 / 配置读写 /
Skill 导出 / 运行记录查询。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool
import yaml

from . import deps
from .engine.executor import Engine
from .engine.models import Workflow
from .engine.command_format import command_context_from_config
from .services import storage
from .services.config_store import (
    load_config, redact_config, save_config, validate_config)
from .services.debug_service import DebugService, MissingDebugInputsError
from .services.agent_runtime import prepare_agent_task, read_agent_task
from .services.exporter import export_skill
from .validator.validator import validate_workflow

MASK = "******"

# pi-agent 的模型/鉴权配置目录（默认 ~/.pi/agent）
PI_AGENT_DIR = Path(os.environ.get(
    "PI_AGENT_DIR", str(Path.home() / ".pi" / "agent")))


def _sync_pi_agent_config(config: dict[str, Any]) -> None:
    """把页面保存的模型配置同步写入 pi-agent 的 models.json / settings.json。

    pi-agent driver（独立 Node 进程）用 ~/.pi/agent 下的配置选择模型，
    与后端 data/config.yaml 相互独立。为了让页面配置真实生效于 pi 模式，
    保存配置时把 default_provider / default_model 及 base_url / api_key
    同步过去。保留已有 model 元数据，避免破坏用户手动配置的细节。
    """
    try:
        agent_dir = Path(PI_AGENT_DIR)
        agent_dir.mkdir(parents=True, exist_ok=True)
        models_file = agent_dir / "models.json"
        settings_file = agent_dir / "settings.json"

        # 读取既有 models.json（保留用户已有 provider / model 元数据）
        try:
            existing = json.loads(models_file.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (OSError, ValueError):
            existing = {}
        providers_out = existing.get("providers") or {}

        page_providers = config.get("providers") or {}
        default_provider = config.get("default_provider") or ""
        default_model = (config.get("default_model") or "").strip()

        # 收集既有 provider 的 apiKey（按 baseUrl 索引），用于 api_key 为空时继承
        def _base_to_key(providers_map):
            m = {}
            for p in providers_map.values():
                if isinstance(p, dict) and p.get("baseUrl") and p.get("apiKey"):
                    m.setdefault(p["baseUrl"], p["apiKey"])
            return m

        inherit_keys = _base_to_key(providers_out)

        for name, pcfg in page_providers.items():
            if not isinstance(pcfg, dict):
                continue
            base_url = (pcfg.get("base_url") or "").strip()
            api_key = (pcfg.get("api_key") or "").strip()
            entry = dict(providers_out.get(name) or {})
            if base_url:
                entry["baseUrl"] = base_url
            if api_key:
                entry["apiKey"] = api_key
            elif base_url and not entry.get("apiKey"):
                # 页面 api_key 为空时，按相同 baseUrl 从既有 provider 继承 key，
                # 避免 default 指向的 provider 因缺 key 而认证失败
                entry["apiKey"] = inherit_keys.get(base_url) or ""
            # api 类型：anthropic 风格走 anthropic，其余走 openai-completions
            api_lower = base_url.lower()
            if "anthropic" in api_lower:
                entry["api"] = "anthropic"
            else:
                entry.setdefault("api", "openai-completions")
            # 确保 default_model 出现在这条 provider 的模型列表里
            models = entry.get("models") or []
            if default_model and not any(
                    (m or {}).get("id") == default_model for m in models):
                models.append(_default_pi_model(default_model))
            entry["models"] = models
            providers_out[name] = entry

        # 设置默认 provider / model
        if default_provider:
            providers_out.setdefault(default_provider, {
                "baseUrl": "", "api": "openai-completions", "apiKey": "",
                "models": ([_default_pi_model(default_model)]
                           if default_model else []),
            })
        models_file.write_text(
            json.dumps({"providers": providers_out},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

        # settings.json：defaultProvider / defaultModel
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        if default_provider:
            settings["defaultProvider"] = default_provider
        if default_model:
            settings["defaultModel"] = default_model
        settings.setdefault("defaultProjectTrust", "trusted")
        settings_file.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError as e:  # 同步失败不影响主配置保存
        import logging
        logging.getLogger("app").warning(
            "同步 pi-agent 模型配置失败: %s", e)


def _default_pi_model(model_id: str) -> dict[str, Any]:
    """为 pi 生成一条最小可用的 model 条目。"""
    return {
        "id": model_id,
        "name": model_id,
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 16384,
        "cost": {"input": 0.5, "output": 2,
                 "cacheRead": 0.1, "cacheWrite": 1},
        "compat": {"supportsDeveloperRole": False},
    }


def _new_workflow(name: str) -> dict:
    """新建工作流：默认包含一个 Start 节点。"""
    return {
        "id": f"wf-{uuid.uuid4().hex[:8]}",
        "name": name,
        "nodes": [{
            "id": "start-1", "type": "start", "name": "开始",
            "config": {"inputs": []},
            "position": {"x": 120, "y": 240},
        }],
        "edges": [],
    }


def _find_workflow_path(wf_dir: Path, wf_id: str) -> Path | None:
    for item in storage.list_workflows(wf_dir):
        if item["id"] == wf_id:
            return Path(item["path"])
    return None


def create_app(data_dir: str | Path) -> FastAPI:
    data_dir = Path(data_dir)
    wf_dir = data_dir / "workflows"
    runs_dir = data_dir / "runs"
    config_path = data_dir / "config.yaml"
    wf_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="fsm to skill")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
        allow_headers=["*"])
    app.state.debug_service = DebugService()

    def _services() -> tuple[Any, Any]:
        cfg = load_config(config_path)
        return deps.build_code_service(cfg), deps.build_llm_service(cfg)

    # --------------------------------------------------------------
    # 工作流 CRUD
    @app.post("/api/workflows")
    def create_workflow(body: dict):
        wf = _new_workflow(body.get("name") or "未命名工作流")
        storage.save_workflow(wf_dir, wf)
        return wf

    @app.get("/api/workflows")
    def list_workflows():
        return storage.list_workflows(wf_dir)

    @app.get("/api/workflows/{wf_id}")
    def get_workflow(wf_id: str):
        path = _find_workflow_path(wf_dir, wf_id)
        if path is None:
            raise HTTPException(404, detail=f"工作流不存在: {wf_id}")
        return storage.load_workflow(path)

    @app.put("/api/workflows/{wf_id}")
    def save_workflow(wf_id: str, body: dict):
        # 保存不受校验限制（允许半成品），但始终返回最新校验报告
        body["id"] = wf_id
        path = storage.save_workflow(wf_dir, body)
        return {"ok": True, "path": str(path),
                "validation": validate_workflow(body)}

    @app.delete("/api/workflows/{wf_id}")
    def delete_workflow(wf_id: str):
        path = _find_workflow_path(wf_dir, wf_id)
        if path is None:
            raise HTTPException(404, detail=f"工作流不存在: {wf_id}")
        storage.delete_workflow(path)
        return {"ok": True}

    @app.post("/api/workflows/validate")
    def validate(body: dict):
        return validate_workflow(body)

    # --------------------------------------------------------------
    # 本机文件打开 / 保存（桌面本地应用增强）
    @app.post("/api/files/save-workflow")
    def save_workflow_file(body: dict):
        wf = body.get("workflow")
        if not isinstance(wf, dict):
            raise HTTPException(400, detail="workflow 字段缺失")
        requested_path = body.get("path")
        path = requested_path if isinstance(requested_path, str) and \
            requested_path.strip() else None
        if path is None:
            path = _ask_save_workflow_path(wf.get("name") or "workflow")
        if path is None:
            return {"ok": False, "cancelled": True}
        p = Path(path)
        if p.suffix.lower() not in (".yaml", ".yml"):
            p = p.with_suffix(".yaml")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(wf, allow_unicode=True,
                                    sort_keys=False),
                     encoding="utf-8")
        return {"ok": True, "path": str(p)}

    @app.post("/api/files/open-workflow")
    def open_workflow_file():
        path = _ask_open_workflow_path()
        if path is None:
            return {"ok": False, "cancelled": True}
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
            if p.suffix.lower() in (".yaml", ".yml"):
                wf = yaml.safe_load(text)
            else:
                wf = json.loads(text)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, detail=f"读取工作流文件失败: {e}")
        if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), list):
            raise HTTPException(422, detail="文件不是有效工作流")
        return {"ok": True, "path": str(p), "workflow": wf}

    # --------------------------------------------------------------
    # 运行（SSE）与运行记录
    @app.post("/api/run")
    async def run_workflow(body: dict):
        wf = body.get("workflow")
        if not isinstance(wf, dict):
            raise HTTPException(400, detail="workflow 字段缺失")
        raw_inputs = body.get("inputs") or {}
        if not isinstance(raw_inputs, dict):
            raise HTTPException(400, detail="inputs 必须是对象")
        inputs = dict(raw_inputs)
        task_id = _ensure_task_id(inputs)
        report = validate_workflow(wf)
        if report["errors"]:
            first = next(
                (i for i in report["errors"] if i.get("node_id")),
                report["errors"][0])
            raise HTTPException(422, detail={
                "message": "存在校验错误，运行被拒绝",
                "errors": report["errors"],
                "first_error_node": first.get("node_id")})
        cfg = load_config(config_path)
        runtime_wf = _prepare_runtime_workflow(wf, cfg, data_dir, task_id)
        code_service, llm_service = deps.build_code_service(cfg), \
            deps.build_llm_service(cfg)
        engine = Engine(Workflow.from_dict(runtime_wf),
                        llm_service=llm_service,
                        code_service=code_service)
        queue: asyncio.Queue = asyncio.Queue()

        def on_event(ev: dict) -> None:
            queue.put_nowait(ev)

        requested_stream = body.get("stream")
        use_stream = bool(requested_stream) if requested_stream is not None \
            else bool(cfg.get("stream", True))

        async def event_stream():
            task = asyncio.create_task(
                engine.run(inputs, on_event=on_event, stream=use_stream))
            while True:
                ev = await queue.get()
                if (ev.get("event") == "workflow_finished" and
                        (ev.get("result") or {}).get("status") == "waiting"):
                    prepare_agent_task(data_dir, runtime_wf, ev["result"])
                    _prepare_runtime_skill_task(runtime_wf, ev["result"])
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("event") == "workflow_finished":
                    break
            result = await task
            _save_run_record(runs_dir, wf, result)

        return StreamingResponse(event_stream(),
                                 media_type="text/event-stream")

    # --------------------------------------------------------------
    # pi-agent 模拟外部 Agent（Node 驱动，进度与 token 经 SSE 转发）
    @app.post("/api/run/pi")
    async def run_pi_agent(body: dict):
        """用 pi-agent 模拟外部 Agent 驱动工作流直到完成。

        后端启动任务到首个 waiting，spawn pi-agent driver（Node 子进程），
        把 driver 的进度与 token 统计经 SSE 转发给前端。
        """
        wf = body.get("workflow")
        if not isinstance(wf, dict):
            raise HTTPException(400, detail="workflow 字段缺失")
        raw_inputs = body.get("inputs") or {}
        if not isinstance(raw_inputs, dict):
            raise HTTPException(400, detail="inputs 必须是对象")
        inputs = dict(raw_inputs)
        task_id = _ensure_task_id(inputs)
        report = validate_workflow(wf)
        if report["errors"]:
            first = next(
                (i for i in report["errors"] if i.get("node_id")),
                report["errors"][0])
            raise HTTPException(422, detail={
                "message": "存在校验错误，运行被拒绝",
                "errors": report["errors"],
                "first_error_node": first.get("node_id")})
        cfg = load_config(config_path)
        runtime_wf = _prepare_runtime_workflow(wf, cfg, data_dir, task_id)
        code_service, llm_service = \
            deps.build_code_service(cfg), deps.build_llm_service(cfg)
        engine = Engine(Workflow.from_dict(runtime_wf),
                        llm_service=llm_service,
                        code_service=code_service)
        result = await engine.run(inputs, stream=False)

        async def event_stream():
            if result.get("status") != "waiting":
                yield _sse({"type": "done",
                            "status": result.get("status"),
                            "result": result})
                return
            prepare_agent_task(data_dir, runtime_wf, result)
            first_prompt = result.get("waiting_prompt") or ""
            first_prompt_b64 = base64.urlsafe_b64encode(
                first_prompt.encode("utf-8")).decode("ascii")
            backend_root = data_dir.parent
            driver_script = backend_root / "pi-agent" / "driver.mjs"
            runtime_dir = data_dir / "runtime" / _safe_runtime_id(task_id)
            if not driver_script.exists():
                yield _sse({"type": "error",
                            "msg": f"pi-agent 驱动脚本不存在: {driver_script}",
                            "hint": "请先在 pi-agent/ 目录执行 npm install"})
                return
            try:
                proc = await asyncio.create_subprocess_exec(
                    _node_cmd(), str(driver_script),
                    "--task-id", str(task_id),
                    "--cwd", str(backend_root),
                    "--workdir", str(runtime_dir),
                    "--first-prompt-b64", first_prompt_b64,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
            except Exception as e:  # noqa: BLE001
                yield _sse({"type": "error",
                            "msg": f"启动 pi-agent 失败: {e}",
                            "hint": "请确认已安装 Node.js 且已执行 npm config pi-agent 目录的依赖"})
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:  # noqa: BLE001
                    obj = {"type": "raw", "data": text}
                yield _sse(obj)
            tail = await proc.stderr.read()
            if tail:
                yield _sse({"type": "stderr",
                            "data": tail.decode("utf-8", "replace")[-3000:]})
            await proc.wait()
            yield _sse({"type": "exit"})

        return StreamingResponse(event_stream(),
                                 media_type="text/event-stream")

    @app.get("/api/runs")
    def list_runs():
        records = _load_run_records(runs_dir)
        records.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return [{"id": r["id"], "workflow_id": r.get("workflow_id"),
                 "wf_name": r.get("wf_name"),
                 "status": r.get("status"),
                 "failed_node": r.get("failed_node"),
                 "started_at": r.get("started_at")}
                for r in records]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        path = runs_dir / f"{run_id}.json"
        if not path.exists():
            raise HTTPException(404, detail=f"运行记录不存在: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    # --------------------------------------------------------------
    # 单节点调试
    @app.post("/api/debug/node")
    async def debug_node(body: dict):
        wf = body.get("workflow")
        node_id = body.get("node_id")
        inputs = body.get("inputs")
        if not isinstance(wf, dict) or not node_id:
            raise HTTPException(400, detail="workflow / node_id 字段缺失")
        code_service, llm_service = _services()
        ds: DebugService = app.state.debug_service
        ds.code_service = code_service
        ds.llm_service = llm_service
        try:
            # Body nodes are nested under For.config.body; use the same
            # recursive lookup as DebugService so isolated loop-node debug
            # requests are accepted as well as top-level nodes.
            node = ds._find_node(wf, node_id)
            if node.get("type") == "llm":
                return await ds.debug_node_async(wf, node_id, inputs=inputs)
            return await run_in_threadpool(
                ds.debug_node, wf, node_id, inputs)
        except MissingDebugInputsError as e:
            raise HTTPException(400, detail=str(e))
        except (ValueError, TypeError) as e:
            raise HTTPException(400, detail=str(e))
        except KeyError:
            raise HTTPException(404, detail=f"节点不存在: {node_id}")

    @app.post("/api/debug/agent")
    async def debug_agent(body: dict):
        """Send a Prompt exit to the configured model as the external Agent."""
        prompt = body.get("prompt")
        messages = body.get("messages")
        task_id = body.get("task_id") or body.get("task-id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, detail="prompt 字段缺失")
        if messages is not None and not isinstance(messages, list):
            raise HTTPException(400, detail="messages 必须是数组")
        cfg = load_config(config_path)
        agent_cfg = json.loads(json.dumps(cfg, ensure_ascii=False))
        shell_cfg = dict(agent_cfg.get("shell_tool") or {})
        # 调试页的 Agent 回合必须给模型提供 shell tool；配置项只控制 shell
        # 类型、超时和最大调用次数。
        shell_cfg["enabled"] = True
        agent_cfg["shell_tool"] = shell_cfg
        llm_service = deps.build_llm_service(agent_cfg)
        try:
            started = time.perf_counter()
            resp = await llm_service.complete(prompt, messages=messages)
            duration_ms = (time.perf_counter() - started) * 1000
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, detail={
                "message": f"大模型调用失败: {e}"})
        if task_id:
            _finalize_terminal_agent_prompt(data_dir, str(task_id))
        task_state = _read_runtime_agent_task(data_dir, str(task_id)) \
            if task_id else None
        return {
            "ok": True,
            "task-id": task_id,
            "content": resp.get("content", ""),
            "thinking": resp.get("thinking"),
            "usage": resp.get("usage"),
            "duration_ms": duration_ms,
            "tool_results": resp.get("tool_results", []),
            "trace": resp.get("trace", []),
            "task_state": task_state,
        }

    @app.post("/api/debug/agent/stream")
    async def debug_agent_stream(body: dict):
        """Stream one external Agent ReAct run as SSE events."""
        prompt = body.get("prompt")
        messages = body.get("messages")
        task_id = body.get("task_id") or body.get("task-id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(400, detail="prompt 字段缺失")
        if messages is not None and not isinstance(messages, list):
            raise HTTPException(400, detail="messages 必须是数组")
        cfg = load_config(config_path)
        agent_cfg = json.loads(json.dumps(cfg, ensure_ascii=False))
        shell_cfg = dict(agent_cfg.get("shell_tool") or {})
        shell_cfg["enabled"] = True
        agent_cfg["shell_tool"] = shell_cfg
        llm_service = deps.build_llm_service(agent_cfg)

        def with_task_state(payload: dict, started: float) -> dict:
            out = dict(payload)
            out["duration_ms"] = (time.perf_counter() - started) * 1000
            if task_id:
                out["task_state"] = _read_runtime_agent_task(
                    data_dir, str(task_id))
            return out

        async def event_stream():
            started = time.perf_counter()
            initial_task_state = _read_runtime_agent_task(
                data_dir, str(task_id)) if task_id else None
            try:
                if hasattr(llm_service, "complete_events"):
                    async for item in llm_service.complete_events(
                            prompt, messages=messages):
                        if item.get("event") == "agent_final" and task_id:
                            _finalize_terminal_agent_prompt(
                                data_dir, str(task_id))
                        payload = with_task_state(item, started)
                        yield (
                            "data: "
                            + json.dumps(payload, ensure_ascii=False)
                            + "\n\n"
                        )
                        if _should_handoff_agent_prompt(
                                item, payload.get("task_state"),
                                initial_task_state):
                            handoff = with_task_state({
                                "event": "agent_final",
                                "handoff": True,
                                "response": {
                                    "content": "",
                                    "handoff": True,
                                    "trace": [],
                                    "tool_results": [],
                                },
                            }, started)
                            yield "data: " + json.dumps(
                                handoff, ensure_ascii=False) + "\n\n"
                            return
                else:
                    resp = await llm_service.complete(prompt, messages=messages)
                    if task_id:
                        _finalize_terminal_agent_prompt(data_dir, str(task_id))
                    yield (
                        "data: "
                        + json.dumps(with_task_state({
                            "event": "agent_final",
                            "response": resp,
                        }, started), ensure_ascii=False)
                        + "\n\n"
                    )
            except Exception as e:  # noqa: BLE001
                payload = with_task_state({
                    "event": "agent_error",
                    "detail": {"message": f"大模型调用失败: {e}"},
                }, started)
                yield "data: " + json.dumps(
                    payload, ensure_ascii=False) + "\n\n"

        return StreamingResponse(event_stream(),
                                 media_type="text/event-stream")

    # --------------------------------------------------------------
    # 配置
    @app.get("/api/config")
    def get_config():
        return redact_config(load_config(config_path))

    @app.put("/api/config")
    def put_config(body: dict):
        # 脱敏占位符 "******" 表示保留已存储的真实 Key
        try:
            stored = load_config(config_path)
        except Exception:  # noqa: BLE001 - 存储损坏时以提交内容为准
            stored = {}
        merged = json.loads(json.dumps(body, ensure_ascii=False))
        for name, pcfg in (merged.get("providers") or {}).items():
            if isinstance(pcfg, dict) and pcfg.get("api_key") == MASK:
                old = (stored.get("providers") or {}).get(name) or {}
                pcfg["api_key"] = old.get("api_key", "")
        errors = validate_config(merged)
        if errors:
            raise HTTPException(422, detail={
                "message": "配置校验失败", "errors": errors})
        save_config(config_path, merged)
        # 同步页面模型配置到 pi-agent，使 pi 模式使用同一模型
        _sync_pi_agent_config(merged)
        return {"ok": True}

    @app.post("/api/config/test")
    async def test_config(body: dict):
        """测试弹窗中当前配置的默认模型接口；不保存配置。"""
        try:
            stored = load_config(config_path)
        except Exception:  # noqa: BLE001
            stored = {}
        merged = json.loads(json.dumps(body, ensure_ascii=False))
        for name, pcfg in (merged.get("providers") or {}).items():
            if isinstance(pcfg, dict) and pcfg.get("api_key") == MASK:
                old = (stored.get("providers") or {}).get(name) or {}
                pcfg["api_key"] = old.get("api_key", "")
        errors = validate_config(merged)
        if errors:
            raise HTTPException(422, detail={
                "message": "配置校验失败", "errors": errors})
        llm_service = deps.build_llm_service(merged)
        try:
            resp = await llm_service.complete("请只回复 OK。")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, detail={
                "message": f"模型接口测试失败: {e}"})
        return {
            "ok": True,
            "provider": merged.get("default_provider"),
            "model": merged.get("default_model"),
            "content": resp.get("content", ""),
            "usage": resp.get("usage"),
        }

    # --------------------------------------------------------------
    # Skill 导出
    @app.post("/api/export")
    def export(body: dict):
        wf = body.get("workflow")
        target_dir = body.get("target_dir")
        if not isinstance(wf, dict):
            raise HTTPException(400, detail="workflow 字段缺失")
        overwrite = bool(body.get("overwrite", False))
        report = validate_workflow(wf)
        if report["errors"]:
            codes = ", ".join(i["code"] for i in report["errors"])
            raise HTTPException(
                422, detail=f"工作流存在校验 Error，禁止导出: {codes}")
        if not target_dir:
            parent = _ask_export_parent_path(wf.get("name") or wf.get("id")
                                             or "skill")
            if parent is None:
                return {"ok": False, "cancelled": True}
            target_dir = str(Path(parent) / _safe_filename(
                wf.get("name") or wf.get("id") or "skill"))
            if Path(target_dir).exists() and not overwrite:
                overwrite = _confirm_overwrite(Path(target_dir))
                if not overwrite:
                    return {"ok": False, "cancelled": True}
        try:
            cfg = load_config(config_path)
            context = command_context_from_config(
                cfg, Path(target_dir) / "scripts" / "main.py")
            path = export_skill(wf, target_dir, overwrite=overwrite,
                                command_context=context)
        except ValueError as e:
            raise HTTPException(422, detail=str(e))
        except FileExistsError as e:
            raise HTTPException(409, detail=str(e))
        return {"ok": True, "path": str(path)}

    # --------------------------------------------------------------
    # 前端静态资源（生产模式：服务已构建的前端）
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(static_dir),
                                   html=True), name="static")

    return app


# ----------------------------------------------------------------------
def _save_run_record(runs_dir: Path, wf: dict, result: dict) -> None:
    run_id = "run-" + datetime.now(timezone.utc).strftime(
        "%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    record = {
        "id": run_id,
        "workflow_id": wf.get("id"),
        "wf_name": wf.get("name"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": result.get("status"),
        "failed_node": result.get("failed_node"),
        "end_node": result.get("end_node"),
        "waiting_node": result.get("waiting_node"),
        "waiting_prompt": result.get("waiting_prompt"),
        "llm_call_count": result.get("llm_call_count", 0),
        "task-id": result.get("task-id") or result.get("task_id"),
        "task_id": result.get("task-id") or result.get("task_id"),
        "node_records": result.get("node_records", {}),
        "variables": result.get("variables", {}),
    }
    (runs_dir / f"{run_id}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8")


def _load_run_records(runs_dir: Path) -> list[dict]:
    records = []
    for p in sorted(runs_dir.glob("run-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and "id" in data:
            records.append(data)
    return records


def _sse(obj: Any) -> str:
    """Format one SSE event carrying a JSON object."""
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _node_cmd() -> str:
    """Locate the node executable used to spawn the pi-agent driver."""
    return shutil.which("node") or os.environ.get("PI_NODE", "node")


def _ensure_task_id(inputs: dict[str, Any]) -> str:
    task_id = str(inputs.get("task-id") or inputs.get("task_id") or
                  f"task-{uuid.uuid4().hex[:12]}")
    inputs["task-id"] = task_id
    inputs["task_id"] = task_id
    return task_id


def _prepare_runtime_workflow(wf: dict, cfg: dict,
                              data_dir: Path, task_id: str) -> dict:
    """Create the Skill package used by Agent shell commands in debug runs."""
    skill_dir = data_dir / "runtime" / _safe_runtime_id(task_id)
    context = command_context_from_config(
        cfg, skill_dir / "scripts" / "main.py")
    export_skill(wf, skill_dir, overwrite=True, command_context=context)

    runtime_wf = copy.deepcopy(wf)
    runtime_wf["_runtime"] = {
        "skill_scripts_dir": str(skill_dir / "scripts"),
        "command_context": context,
    }
    for node in runtime_wf.get("nodes", []) or []:
        _attach_command_context(node, context)
    return runtime_wf


def _prepare_runtime_skill_task(runtime_wf: dict, result: dict) -> Path | None:
    task_id = str(result.get("task-id") or result.get("task_id") or "")
    if not task_id:
        return None
    runtime = runtime_wf.get("_runtime") or {}
    scripts_dir = runtime.get("skill_scripts_dir")
    if not scripts_dir:
        return None
    task_dir = Path(scripts_dir) / ".dag2skill_tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).timestamp()
    variables = _plain_runtime_values(result.get("variables") or {})
    variables["task-id"] = task_id
    node_statuses = {
        node_id: rec.get("status")
        for node_id, rec in (result.get("node_records") or {}).items()
        if isinstance(rec, dict) and rec.get("status")
    }
    if result.get("waiting_node"):
        node_statuses[result["waiting_node"]] = "waiting"
    state = {
        "task-id": task_id,
        "created-at": now,
        "updated-at": now,
        "variables": variables,
        "waiting-node": result.get("waiting_node"),
        "node-statuses": node_statuses,
        "finished": False,
    }
    path = task_dir / f"{_safe_runtime_task_file(task_id)}.json"
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def _read_runtime_agent_task(data_dir: Path, task_id: str) -> dict | None:
    task_file = _runtime_agent_task_file(data_dir, task_id)
    if task_file.exists():
        try:
            return json.loads(task_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return read_agent_task(data_dir, task_id)


def _runtime_agent_task_file(data_dir: Path, task_id: str) -> Path:
    return data_dir / "runtime" / _safe_runtime_id(task_id) / \
        "scripts" / ".dag2skill_tasks" / \
        f"{_safe_runtime_task_file(task_id)}.json"


def _runtime_workflow_file(data_dir: Path, task_id: str) -> Path:
    return data_dir / "runtime" / _safe_runtime_id(task_id) / "workflow.yaml"


def _finalize_terminal_agent_prompt(data_dir: Path, task_id: str) -> None:
    """Mark Prompt -> End pauses complete after the external Agent replies.

    The generated Skill main.py resumes execution only when the Agent calls a
    Code step. If the waiting Prompt is terminal and its normal outgoing path
    reaches End without another Code step, the backend owns the final handoff.
    """
    task_file = _runtime_agent_task_file(data_dir, task_id)
    wf_file = _runtime_workflow_file(data_dir, task_id)
    if not task_file.exists() or not wf_file.exists():
        return
    try:
        task_state = json.loads(task_file.read_text(encoding="utf-8"))
        workflow = yaml.safe_load(wf_file.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return
    if task_state.get("finished"):
        return
    waiting = task_state.get("waiting-node")
    if not waiting:
        return
    statuses = _terminal_prompt_statuses(workflow, str(waiting))
    if not statuses:
        return
    node_statuses = task_state.setdefault("node-statuses", {})
    node_statuses[str(waiting)] = "success"
    node_statuses.update(statuses)
    task_state["finished"] = True
    task_state["waiting-node"] = None
    task_state["last-prompt"] = None
    task_state.pop("resume", None)
    task_state["updated-at"] = datetime.now(timezone.utc).timestamp()
    task_file.write_text(json.dumps(task_state, ensure_ascii=False, indent=2),
                         encoding="utf-8")


def _terminal_prompt_statuses(workflow: dict, prompt_id: str
                              ) -> dict[str, str] | None:
    graph = _graph_containing_node(workflow, prompt_id)
    if graph is None:
        return None
    by_id = {
        node.get("id"): node
        for node in graph.get("nodes", []) or []
        if node.get("id")
    }
    out_edges: dict[str, list[dict]] = {}
    for edge in graph.get("edges", []) or []:
        if edge.get("source_handle", "out") in ("error", "retry"):
            continue
        out_edges.setdefault(edge.get("source"), []).append(edge)
    queue = [edge.get("target") for edge in out_edges.get(prompt_id, [])]
    seen: set[str] = set()
    statuses: dict[str, str] = {}
    reached_end = False
    while queue:
        node_id = queue.pop(0)
        if not node_id or node_id in seen:
            continue
        seen.add(str(node_id))
        node = by_id.get(node_id)
        if not node:
            continue
        node_type = node.get("type")
        if node_type in ("code", "llm", "if", "for", "aggregate"):
            return None
        if node_type == "end":
            statuses[str(node_id)] = "success"
            reached_end = True
            continue
        queue.extend(edge.get("target") for edge in out_edges.get(node_id, []))
    return statuses if reached_end else None


def _should_handoff_agent_prompt(event: dict, task_state: dict | None,
                                 initial_task_state: dict | None) -> bool:
    if event.get("event") != "agent_shell":
        return False
    if not task_state or task_state.get("finished"):
        return False
    if not task_state.get("last-prompt"):
        return False
    return _prompt_state_marker(task_state) != _prompt_state_marker(
        initial_task_state)


def _prompt_state_marker(task_state: dict | None) -> tuple[Any, Any]:
    if not task_state:
        return None, None
    return task_state.get("updated-at"), task_state.get("last-prompt")


def _graph_containing_node(graph: dict, node_id: str) -> dict | None:
    for node in graph.get("nodes", []) or []:
        if node.get("id") == node_id:
            return graph
    for node in graph.get("nodes", []) or []:
        body = (node.get("config", {}) or {}).get("body") or {}
        found = _graph_containing_node(body, node_id)
        if found is not None:
            return found
    return None


def _plain_runtime_values(snapshot: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, info in snapshot.items():
        if isinstance(info, dict) and "value" in info:
            out[name] = info.get("value")
        else:
            out[name] = info
    return out


def _attach_command_context(node: dict, context: dict) -> None:
    if node.get("type") == "llm":
        cfg = node.setdefault("config", {})
        cfg["command_context"] = dict(context)
    body = (node.get("config", {}) or {}).get("body") or {}
    for child in body.get("nodes", []) or []:
        _attach_command_context(child, context)


def _tk_root():
    try:
        import tkinter as tk
    except Exception as e:  # noqa: BLE001
        raise HTTPException(501, detail=f"当前环境不支持系统文件窗口: {e}")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def _ask_save_workflow_path(name: str) -> str | None:
    try:
        from tkinter import filedialog
        root = _tk_root()
        try:
            path = filedialog.asksaveasfilename(
                title="保存工作流",
                defaultextension=".yaml",
                initialfile=f"{_safe_filename(name)}.yaml",
                filetypes=[
                    ("Workflow YAML", "*.yaml;*.yml"),
                ],
            )
        finally:
            root.destroy()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(501, detail=f"打开保存窗口失败: {e}")
    return path or None


def _ask_open_workflow_path() -> str | None:
    try:
        from tkinter import filedialog
        root = _tk_root()
        try:
            path = filedialog.askopenfilename(
                title="打开工作流",
                filetypes=[
                    ("Workflow YAML", "*.yaml;*.yml"),
                ],
            )
        finally:
            root.destroy()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(501, detail=f"打开文件窗口失败: {e}")
    return path or None


def _ask_export_parent_path(name: str) -> str | None:
    try:
        from tkinter import filedialog
        root = _tk_root()
        try:
            return filedialog.askdirectory(
                title=f"选择「{_safe_filename(name)}」SKILL 导出父目录")
        finally:
            root.destroy()
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(501, detail=f"打开目录选择窗口失败: {e}")


def _confirm_overwrite(path: Path) -> bool:
    try:
        from tkinter import messagebox
        root = _tk_root()
        try:
            return bool(messagebox.askyesno(
                "目录已存在",
                f"目标目录已存在，是否覆盖？\n{path}"))
        finally:
            root.destroy()
    except HTTPException:
        raise
    except Exception:
        return False


def _safe_filename(name: str) -> str:
    bad = '\\/:*?"<>|'
    cleaned = "".join("_" if ch in bad else ch for ch in str(name))
    return cleaned[:80] or "workflow"


def _safe_runtime_id(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "task"))
    return safe.strip(".-")[:80] or f"task-{uuid.uuid4().hex[:12]}"


def _safe_runtime_task_file(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "-", str(value or "task")) or "task"
