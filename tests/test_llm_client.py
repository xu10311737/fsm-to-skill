"""LLM 客户端测试（PRD 7.3 运行规则 + 后端代理决策）。

契约：app.services.llm_client.LLMClient(config)
- complete(prompt, provider=None, model=None) -> {"content", "thinking", "usage"}
- stream(prompt, ...) -> async generator[str]
- 重试：网络错误/超时/5xx 最多重试 2 次（共 3 次）；4xx 不重试
- 默认超时 60s；未配置 API Key 报 LLMNotConfiguredError
- 异常类型：LLMNotConfiguredError / LLMRequestError（含 status_code）
"""
import json

import httpx
import pytest
import respx

from app.services.llm_client import (  # TDD：尚不存在
    LLMClient, LLMNotConfiguredError, LLMRequestError,
)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
COMPAT_URL = "http://localhost:9000/v1/chat/completions"


def make_config(**over):
    cfg = {
        "providers": {
            "openai": {"api_key": "sk-test", "base_url": "https://api.openai.com/v1"},
            "anthropic": {"api_key": "sk-ant", "base_url": "https://api.anthropic.com"},
            "compatible": {"api_key": "sk-c", "base_url": "http://localhost:9000/v1"},
        },
        "default_provider": "openai",
        "default_model": "gpt-test",
        "stream": True,
        "timeout_seconds": 60,
        "max_retries": 2,
    }
    cfg.update(over)
    return cfg


def openai_payload(text="回复", thinking=None, usage=True):
    msg = {"role": "assistant", "content": text}
    if thinking:
        msg["reasoning_content"] = thinking
    data = {"choices": [{"message": msg, "finish_reason": "stop"}]}
    if usage:
        data["usage"] = {"prompt_tokens": 3, "completion_tokens": 5}
    return data


class TestOpenAIComplete:
    def test_file_tools_read_and_write_text(self, tmp_path):
        client = LLMClient(make_config(shell_tool={"enabled": True}))
        path = tmp_path / "notes.txt"

        tools = client._shell_tools()
        assert {tool["function"]["name"] for tool in tools} == {
            "shell", "read_file", "write_file"}

        written = client._execute_shell_tool_call({
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "path": str(path), "content": "hello file"}),
            },
        })
        read = client._execute_shell_tool_call({
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": str(path)}),
            },
        })

        assert written == {
            "ok": True, "path": str(path), "bytes_written": 10}
        assert read == {"ok": True, "path": str(path), "content": "hello file"}

    @respx.mock
    async def test_complete_success(self):
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=openai_payload("你好")))
        client = LLMClient(make_config())
        r = await client.complete("hi")
        assert r["content"] == "你好"
        assert r["usage"]["prompt_tokens"] == 3

    @respx.mock
    async def test_thinking_extracted(self):
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(
            200, json=openai_payload("答", thinking="思考中")))
        client = LLMClient(make_config())
        r = await client.complete("hi")
        assert r["thinking"] == "思考中"

    @respx.mock
    async def test_auth_header_sent(self):
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=openai_payload()))
        client = LLMClient(make_config())
        await client.complete("hi")
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"

    @respx.mock
    async def test_model_in_body(self):
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(200, json=openai_payload()))
        client = LLMClient(make_config())
        await client.complete("hi", model="custom-model")
        import json
        body = json.loads(route.calls[0].request.content)
        assert body["model"] == "custom-model"

    @respx.mock
    async def test_shell_tool_trace_records_model_and_shell(self, monkeypatch):
        tool_call = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "shell",
                "arguments": '{"command": "echo hi"}',
            },
        }
        respx.post(OPENAI_URL).mock(side_effect=[
            httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "先执行命令",
                    "tool_calls": [tool_call],
                }}],
                "usage": {"prompt_tokens": 3},
            }),
            httpx.Response(200, json={
                "choices": [{"message": {
                    "role": "assistant",
                    "content": "完成",
                }}],
                "usage": {"completion_tokens": 2},
            }),
        ])
        monkeypatch.setattr(
            LLMClient, "_run_shell",
            lambda self, command, cwd=None: {
                "ok": True, "exit_code": 0, "command": command,
                "stdout": "hi\n", "stderr": "",
            })
        client = LLMClient(make_config(shell_tool={
            "enabled": True, "shell": "powershell",
            "timeout_seconds": 60, "max_calls": 100,
        }))

        resp = await client.complete("hi")

        assert resp["content"] == "完成"
        assert resp["thinking"] == "先执行命令"
        assert [item["type"] for item in resp["trace"]] == [
            "model", "shell", "model"]
        assert resp["trace"][1]["result"]["stdout"] == "hi\n"


class TestAnthropicComplete:
    @respx.mock
    async def test_anthropic_format(self):
        respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={
            "content": [{"type": "text", "text": "来自Claude"}],
            "usage": {"input_tokens": 4, "output_tokens": 6},
        }))
        client = LLMClient(make_config())
        r = await client.complete("hi", provider="anthropic")
        assert r["content"] == "来自Claude"
        assert r["usage"]["prompt_tokens"] == 4
        assert r["usage"]["completion_tokens"] == 6

    @respx.mock
    async def test_anthropic_headers(self):
        route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(
            200, json={"content": [{"type": "text", "text": "x"}]}))
        client = LLMClient(make_config())
        await client.complete("hi", provider="anthropic")
        headers = route.calls[0].request.headers
        assert headers["x-api-key"] == "sk-ant"
        assert "anthropic-version" in headers


class TestRetryPolicy:
    @respx.mock
    async def test_retry_on_5xx_then_success(self):
        route = respx.post(OPENAI_URL).mock(side_effect=[
            httpx.Response(500, json={"error": "server"}),
            httpx.Response(502, json={"error": "bad gateway"}),
            httpx.Response(200, json=openai_payload("成功")),
        ])
        client = LLMClient(make_config())
        r = await client.complete("hi")
        assert r["content"] == "成功"
        assert route.call_count == 3

    @respx.mock
    async def test_no_retry_on_4xx(self):
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"}))
        client = LLMClient(make_config())
        with pytest.raises(LLMRequestError) as exc:
            await client.complete("hi")
        assert exc.value.status_code == 401
        assert route.call_count == 1

    @respx.mock
    async def test_retry_exhausted_raises(self):
        route = respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(503, json={"error": "down"}))
        client = LLMClient(make_config())
        with pytest.raises(LLMRequestError) as exc:
            await client.complete("hi")
        assert exc.value.status_code == 503
        assert route.call_count == 3  # 1 + 2 次重试

    @respx.mock
    async def test_retry_on_network_error(self):
        route = respx.post(OPENAI_URL).mock(side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=openai_payload("ok")),
        ])
        client = LLMClient(make_config())
        r = await client.complete("hi")
        assert r["content"] == "ok"
        assert route.call_count == 2

    @respx.mock
    async def test_retry_on_timeout(self):
        route = respx.post(OPENAI_URL).mock(side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.Response(200, json=openai_payload("ok")),
        ])
        client = LLMClient(make_config())
        r = await client.complete("hi")
        assert route.call_count == 2


class TestConfigValidation:
    async def test_missing_api_key(self):
        cfg = make_config()
        cfg["providers"]["openai"]["api_key"] = ""
        client = LLMClient(cfg)
        with pytest.raises(LLMNotConfiguredError):
            await client.complete("hi")

    async def test_unknown_provider(self):
        client = LLMClient(make_config())
        with pytest.raises(LLMNotConfiguredError):
            await client.complete("hi", provider="unknown")

    @respx.mock
    async def test_compatible_base_url(self):
        route = respx.post(COMPAT_URL).mock(
            return_value=httpx.Response(200, json=openai_payload("本地模型")))
        client = LLMClient(make_config())
        r = await client.complete("hi", provider="compatible")
        assert r["content"] == "本地模型"
        assert route.called


class TestStream:
    @respx.mock
    async def test_stream_tokens(self):
        sse_body = (
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n'
            'data: [DONE]\n\n'
        )
        respx.post(OPENAI_URL).mock(return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=sse_body))
        client = LLMClient(make_config())
        tokens = [t async for t in client.stream("hi")]
        assert "".join(tokens) == "你好"

    @respx.mock
    async def test_stream_error_status_raises(self):
        respx.post(OPENAI_URL).mock(
            return_value=httpx.Response(500, json={"error": "x"}))
        client = LLMClient(make_config())
        with pytest.raises(LLMRequestError):
            _ = [t async for t in client.stream("hi")]
