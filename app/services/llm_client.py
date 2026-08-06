"""LLM Provider 客户端：OpenAI / Anthropic / OpenAI-compatible（PRD 7.3）。

- complete(prompt, provider=None, model=None) -> {"content", "thinking", "usage"}
- stream(prompt, ...) -> 异步生成 token（SSE）
- 重试：网络错误/超时/5xx 最多重试 max_retries 次；4xx 不重试
- 未配置 API Key / 未知 Provider -> LLMNotConfiguredError
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


class LLMError(Exception):
    """LLM 调用基础异常。"""


class LLMNotConfiguredError(LLMError):
    """Provider 未配置（缺 API Key / 未知 Provider）。"""


class LLMRequestError(LLMError):
    """请求失败（4xx/5xx/网络/超时，重试耗尽后抛出）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LLMClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    # ------------------------------------------------------------------
    # 配置解析
    def _provider_cfg(self, provider: str | None) -> tuple[str, dict]:
        name = provider or self.config.get("default_provider")
        providers = self.config.get("providers", {})
        if not name or name not in providers:
            raise LLMNotConfiguredError(f"未知的 LLM Provider: {name!r}")
        cfg = providers[name]
        if not cfg.get("api_key"):
            raise LLMNotConfiguredError(
                f"Provider {name} 未配置 API Key，请在设置页填写")
        return name, cfg

    def _model(self, model: str | None) -> str:
        return model or self.config.get("default_model") or ""

    @property
    def _timeout(self) -> float:
        return float(self.config.get("timeout_seconds", 60))

    @property
    def _max_retries(self) -> int:
        return int(self.config.get("max_retries", 2))

    # ------------------------------------------------------------------
    # 请求构造
    @staticmethod
    def _is_anthropic(name: str) -> bool:
        return "anthropic" in name.lower()

    def _build_request(self, name: str, cfg: dict, model: str,
                       prompt: str, stream: bool,
                       messages: list[dict[str, Any]] | None = None,
                       tools: list[dict[str, Any]] | None = None
                       ) -> tuple[str, dict, dict]:
        """返回 (url, headers, body)。"""
        base = cfg.get("base_url", "").rstrip("/")
        if self._is_anthropic(name):
            url = (f"{base}/messages" if base.endswith("/v1")
                   else f"{base}/v1/messages")
            return (
                url,
                {"x-api-key": cfg["api_key"],
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
                {"model": model, "max_tokens": 4096,
                 "messages": messages or [{"role": "user", "content": prompt}],
                 "stream": stream},
            )
        body = (
            {"model": model,
             "messages": messages or [{"role": "user", "content": prompt}],
             "stream": stream}
        )
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return (
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {cfg['api_key']}",
             "Content-Type": "application/json"},
            body,
        )

    # ------------------------------------------------------------------
    async def complete(self, prompt: str, provider: str | None = None,
                       model: str | None = None,
                       node_id: str | None = None,
                       messages: list[dict[str, Any]] | None = None
                       ) -> dict[str, Any]:
        """返回 {"content", "thinking", "usage"}。"""
        name, cfg = self._provider_cfg(provider)
        if self._shell_tool_enabled() and not self._is_anthropic(name):
            return await self._complete_with_tools(
                name, cfg, self._model(model), prompt, messages=messages)
        url, headers, body = self._build_request(
            name, cfg, self._model(model), prompt, stream=False)
        resp = await self._request_with_retry(url, headers, body)
        return self._parse_complete(name, resp)

    async def complete_events(
            self, prompt: str, provider: str | None = None,
            model: str | None = None,
            messages: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """逐步产出模型回合、shell 调用和最终结果。

        ``messages`` 为可选的持续会话历史：传入时，会把 ``prompt`` 作为
        新的 user 消息追加到历史末尾，从而跨 LLM 节点共享上下文（路径 A1）。
        ``agent_final.response.messages`` 会返回更新后的完整会话，供前端
        累积传递到下一个节点。
        """
        name, cfg = self._provider_cfg(provider)
        if self._shell_tool_enabled() and not self._is_anthropic(name):
            async for item in self._complete_with_tools_events(
                    name, cfg, self._model(model), prompt, messages):
                yield item
            return
        resp = await self.complete(prompt, provider=provider, model=model)
        model_item = {
            "type": "model",
            "turn": 1,
            "content": resp.get("content", ""),
            "thinking": resp.get("thinking"),
            "tool_calls": [],
            "usage": resp.get("usage"),
        }
        yield {"event": "agent_model", "item": model_item}
        yield {"event": "agent_final", "response": {
            **resp,
            "trace": [model_item],
            "messages": (list(messages or [])
                         + [{"role": "user", "content": prompt}]),
        }}

    async def _complete_with_tools(self, name: str, cfg: dict, model: str,
                                   prompt: str,
                                   messages: list[dict[str, Any]] | None = None
                                   ) -> dict[str, Any]:
        messages = list(messages or [])
        messages.append({"role": "user", "content": prompt})
        total_usage: dict[str, int] = {}
        thinking_parts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        max_calls = int((self.config.get("shell_tool") or {})
                        .get("max_calls", 100))
        for turn in range(max_calls + 1):
            url, headers, body = self._build_request(
                name, cfg, model, prompt, stream=False, messages=messages,
                tools=self._shell_tools())
            resp = await self._request_with_retry(url, headers, body)
            data = resp.json()
            self._merge_usage(total_usage, data.get("usage"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {}
            if msg.get("reasoning_content"):
                thinking_parts.append(msg.get("reasoning_content"))
            tool_calls = msg.get("tool_calls") or []
            trace.append({
                "type": "model",
                "turn": turn + 1,
                "content": msg.get("content") or "",
                "thinking": msg.get("reasoning_content"),
                "tool_calls": tool_calls,
            })
            if not tool_calls:
                return {
                    "content": msg.get("content", ""),
                    "thinking": "\n\n".join(thinking_parts) or None,
                    "usage": total_usage or data.get("usage"),
                    "tool_results": tool_results,
                    "trace": trace,
                }
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                result = self._execute_shell_tool_call(call)
                item = {
                    "tool_call_id": call.get("id"),
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": (call.get("function") or {}).get("arguments"),
                    "result": result,
                }
                tool_results.append(item)
                trace.append({
                    "type": "shell",
                    "turn": turn + 1,
                    **item,
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
        raise LLMRequestError("shell tool 调用次数超过 shell_tool.max_calls")

    async def _complete_with_tools_events(
            self, name: str, cfg: dict, model: str,
            prompt: str,
            messages: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        messages = list(messages or [])
        messages.append({"role": "user", "content": prompt})
        total_usage: dict[str, int] = {}
        thinking_parts: list[str] = []
        tool_results: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        max_calls = int((self.config.get("shell_tool") or {})
                        .get("max_calls", 100))
        for turn in range(max_calls + 1):
            url, headers, body = self._build_request(
                name, cfg, model, prompt, stream=False, messages=messages,
                tools=self._shell_tools())
            resp = await self._request_with_retry(url, headers, body)
            data = resp.json()
            usage = data.get("usage")
            self._merge_usage(total_usage, usage)
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {}) or {}
            if msg.get("reasoning_content"):
                thinking_parts.append(msg.get("reasoning_content"))
            tool_calls = msg.get("tool_calls") or []
            model_item = {
                "type": "model",
                "turn": turn + 1,
                "content": msg.get("content") or "",
                "thinking": msg.get("reasoning_content"),
                "tool_calls": tool_calls,
                "usage": usage,
            }
            trace.append(model_item)
            yield {"event": "agent_model", "item": model_item}
            if not tool_calls:
                yield {"event": "agent_final", "response": {
                    "content": msg.get("content", ""),
                    "thinking": "\n\n".join(thinking_parts) or None,
                    "usage": total_usage or usage,
                    "tool_results": tool_results,
                    "trace": trace,
                    "messages": messages,
                }}
                return
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                result = self._execute_shell_tool_call(call)
                item = {
                    "tool_call_id": call.get("id"),
                    "name": (call.get("function") or {}).get("name"),
                    "arguments": (call.get("function") or {}).get("arguments"),
                    "result": result,
                }
                tool_results.append(item)
                shell_item = {
                    "type": "shell",
                    "turn": turn + 1,
                    **item,
                }
                trace.append(shell_item)
                yield {"event": "agent_shell", "item": shell_item}
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": json.dumps(result, ensure_ascii=False),
                })
        raise LLMRequestError("shell tool 调用次数超过 shell_tool.max_calls")

    async def _request_with_retry(self, url: str, headers: dict,
                                  body: dict) -> httpx.Response:
        attempts = 1 + self._max_retries
        last_error: LLMRequestError | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_error = LLMRequestError(f"网络错误: {e}")
                if attempt < attempts - 1:
                    await asyncio.sleep(0.1 * (2 ** attempt))
                    continue
                raise last_error
            if resp.status_code < 400:
                return resp
            err = LLMRequestError(
                f"LLM 请求失败（HTTP {resp.status_code}）: {resp.text[:200]}",
                status_code=resp.status_code)
            if 400 <= resp.status_code < 500:
                raise err  # 4xx 不重试
            last_error = err
            if attempt < attempts - 1:
                await asyncio.sleep(0.1 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_complete(name: str, resp: httpx.Response) -> dict[str, Any]:
        data = resp.json()
        if "anthropic" in name.lower():
            text = "".join(
                b.get("text", "") for b in data.get("content", [])
                if b.get("type") == "text")
            usage_raw = data.get("usage") or {}
            usage = {
                "prompt_tokens": usage_raw.get("input_tokens"),
                "completion_tokens": usage_raw.get("output_tokens"),
                "cache_creation_input_tokens": usage_raw.get(
                    "cache_creation_input_tokens"),
                "cache_read_input_tokens": usage_raw.get(
                    "cache_read_input_tokens"),
            } if usage_raw else None
            return {"content": text, "thinking": None, "usage": usage}
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        return {
            "content": msg.get("content", ""),
            "thinking": msg.get("reasoning_content"),
            "usage": data.get("usage"),
        }

    # ------------------------------------------------------------------
    async def stream(self, prompt: str, provider: str | None = None,
                     model: str | None = None,
                     node_id: str | None = None) -> AsyncIterator[str]:
        """SSE 流式产出 token。"""
        if self._shell_tool_enabled():
            resp = await self.complete(prompt, provider=provider, model=model,
                                       node_id=node_id)
            for ch in resp.get("content", ""):
                yield ch
            return
        name, cfg = self._provider_cfg(provider)
        url, headers, body = self._build_request(
            name, cfg, self._model(model), prompt, stream=True)
        anthropic = self._is_anthropic(name)
        attempts = 1 + self._max_retries
        last_error: LLMRequestError | None = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    async with client.stream(
                            "POST", url, headers=headers,
                            json=body) as resp:
                        if resp.status_code >= 400:
                            await resp.aread()
                            raise LLMRequestError(
                                f"LLM 流式请求失败（HTTP {resp.status_code}）",
                                status_code=resp.status_code)
                        async for token in self._iter_sse(resp, anthropic):
                            yield token
                        return
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_error = LLMRequestError(f"网络错误: {e}")
            except LLMRequestError as e:
                last_error = e
                if e.status_code is not None and 400 <= e.status_code < 500:
                    raise  # 4xx 不重试
            if attempt < attempts - 1:
                await asyncio.sleep(0.1 * (2 ** attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    async def _iter_sse(resp: httpx.Response,
                        anthropic: bool) -> AsyncIterator[str]:
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if anthropic:
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    text = delta.get("text")
                    if text:
                        yield text
            else:
                for choice in data.get("choices", []):
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text

    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """默认 Provider 是否已配置 API Key。"""
        try:
            self._provider_cfg(None)
            return True
        except LLMNotConfiguredError:
            return False

    def _shell_tool_enabled(self) -> bool:
        return bool((self.config.get("shell_tool") or {}).get("enabled"))

    @staticmethod
    def _shell_tools() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "shell",
                    "description": (
                        "Execute a local shell command for the current DAG Skill "
                        "task on the CURRENT HOST. "
                        "IMPORTANT: The host is Windows PowerShell (not bash). "
                        "Do NOT use bash syntax: no '&&', no '||', no 'ls -la', "
                        "no 'test -f', no 'rg', no '&& echo'. Use PowerShell "
                        "cmdlets: 'Get-ChildItem', 'Test-Path', 'Get-Content', "
                        "separate commands with ';' instead of '&&'. "
                        "Use the exact command template provided in the prompt "
                        "(it already uses --step-param-b64 '<base64>' so JSON "
                        "needs no quoting). Do NOT hand-write --step-param with "
                        "a raw JSON string, it will be mangled by PowerShell. "
                        "Replace the <placeholders> in the template with real "
                        "values, re-base64 the JSON if needed, then run it."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Command line to execute.",
                            },
                            "cwd": {
                                "type": "string",
                                "description": "Optional working directory.",
                            },
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a UTF-8 text file from the local task workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute or current-workspace-relative file path.",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Create or replace a UTF-8 text file in the local task workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute or current-workspace-relative file path.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Complete UTF-8 text content to write.",
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ]

    def _execute_shell_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError as exc:
            return {"ok": False, "error": f"工具参数不是 JSON: {exc}"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "工具参数必须是 JSON object"}
        name = fn.get("name")
        if name in ("shell", "bash"):
            return self._execute_shell_args(args)
        if name == "read_file":
            return self._read_tool_file(args.get("path"))
        if name == "write_file":
            return self._write_tool_file(args.get("path"), args.get("content"))
        return {"ok": False, "error": f"未知工具: {name}"}

    def _execute_shell_args(self, args: dict[str, Any]) -> dict[str, Any]:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"ok": False, "error": "command 不能为空"}
        cwd = args.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return {"ok": False, "error": "cwd 必须是字符串"}
        return self._run_shell(command, cwd=cwd)

    @staticmethod
    def _tool_file_path(value: Any) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return Path(value).expanduser().resolve()

    def _read_tool_file(self, value: Any) -> dict[str, Any]:
        path = self._tool_file_path(value)
        if path is None:
            return {"ok": False, "error": "path 不能为空"}
        try:
            if not path.is_file():
                return {"ok": False, "path": str(path), "error": "文件不存在"}
            return {
                "ok": True,
                "path": str(path),
                "content": _limit_tool_text(path.read_text(encoding="utf-8")),
            }
        except (OSError, UnicodeError) as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def _write_tool_file(self, value: Any, content: Any) -> dict[str, Any]:
        path = self._tool_file_path(value)
        if path is None:
            return {"ok": False, "error": "path 不能为空"}
        if not isinstance(content, str):
            return {"ok": False, "path": str(path), "error": "content 必须是字符串"}
        if len(content) > 1_000_000:
            return {"ok": False, "path": str(path), "error": "content 超过 1,000,000 字符限制"}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"ok": True, "path": str(path), "bytes_written": len(content.encode("utf-8"))}
        except OSError as exc:
            return {"ok": False, "path": str(path), "error": str(exc)}

    def _run_shell(self, command: str, cwd: str | None = None) -> dict[str, Any]:
        cfg = self.config.get("shell_tool") or {}
        timeout = float(cfg.get("timeout_seconds", 60))
        shell = str(cfg.get("shell") or "auto")
        argv = self._shell_argv(command, shell)
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd or None,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            stdout = _decode_shell_bytes(proc.stdout)
            stderr = _decode_shell_bytes(proc.stderr)
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "command": command,
                "stdout": _limit_tool_text(stdout),
                "stderr": _limit_tool_text(stderr),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "exit_code": None,
                "command": command,
                "stdout": _limit_tool_text(_decode_shell_bytes(exc.stdout)),
                "stderr": _limit_tool_text(_decode_shell_bytes(exc.stderr)),
                "error": f"shell 命令超过 {timeout:g} 秒被终止",
            }
        except OSError as exc:
            return {"ok": False, "exit_code": None,
                    "command": command, "error": str(exc)}

    @staticmethod
    def _shell_argv(command: str, shell: str) -> str | list[str]:
        shell = _resolve_shell(shell)
        if shell in ("powershell", "pwsh"):
            exe = "powershell" if shell == "powershell" else "pwsh"
            return [exe, "-NoProfile", "-Command", command]
        exe = shell
        if shell == "bash" and shutil.which("bash") is None:
            exe = "sh"
        return [exe, "-lc", command]

    @staticmethod
    def _merge_usage(total: dict[str, int],
                     usage: dict[str, Any] | None) -> None:
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
            elif isinstance(value, dict):
                nested = total.setdefault(key, {})
                if isinstance(nested, dict):
                    LLMClient._merge_usage(nested, value)


def _limit_tool_text(value: str, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n... truncated {len(value) - limit} chars"


def _decode_shell_bytes(data: bytes | None) -> str:
    """解码 shell 输出，优先 UTF-8，失败时回退到 GBK（Windows 常见乱码）。"""
    if not data:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("gbk", errors="replace")
        except (UnicodeDecodeError, LookupError):
            return data.decode("utf-8", errors="replace")


def _resolve_shell(shell: str) -> str:
    value = (shell or "auto").strip().lower()
    if value == "auto":
        return "powershell" if sys.platform.startswith("win") else "bash"
    if value == "cmd":
        return "powershell" if sys.platform.startswith("win") else "bash"
    if value in ("powershell", "pwsh", "bash", "sh"):
        return value
    return "powershell" if sys.platform.startswith("win") else "bash"
