"""FastAPI 接口契约测试（PRD 第 11 章 + SSE 运行决策）。

契约：app.main.create_app(data_dir: Path) -> FastAPI
端点：
  GET    /api/workflows                     列表
  POST   /api/workflows                     新建（含默认 start 节点）
  GET    /api/workflows/{wf_id}             详情
  PUT    /api/workflows/{wf_id}             保存（允许带 Error 保存，返回校验报告）
  DELETE /api/workflows/{wf_id}             删除
  POST   /api/workflows/validate            校验（body 为 workflow）
  POST   /api/run                           运行（SSE 事件流；有 Error 则 422）
  POST   /api/debug/node                    单节点调试
  GET    /api/config                        读配置（Key 脱敏）
  PUT    /api/config                        写配置
  POST   /api/export                        导出 Skill（有 Error 则 422）
  GET    /api/runs                          运行记录列表
  GET    /api/runs/{run_id}                 运行详情（回放）
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import yaml

from app.main import create_app  # TDD：尚不存在


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 引擎内真实 LLM 调用替换为假服务（通过依赖注入钩子）
    from app import deps
    from tests.conftest import FakeLLMService
    monkeypatch.setattr(deps, "build_llm_service",
                        lambda cfg: FakeLLMService(reply="接口回复"))
    app = create_app(tmp_path)
    return TestClient(app)


@pytest.fixture
def wf_payload(factories):
    return factories["workflow"](
        [factories["start"](), factories["code"](), factories["end"]()],
        [factories["edge"]("start-1", "code-1"),
         factories["edge"]("code-1", "end-1")],
        wf_id="wf-api", name="接口测试")


class TestWorkflowCrud:
    def test_create_has_start_node(self, client):
        r = client.post("/api/workflows", json={"name": "新工作流"})
        assert r.status_code == 200
        data = r.json()
        starts = [n for n in data["nodes"] if n["type"] == "start"]
        assert len(starts) == 1

    def test_list_and_get(self, client, wf_payload):
        client.put(f"/api/workflows/{wf_payload['id']}", json=wf_payload)
        items = client.get("/api/workflows").json()
        assert any(i["id"] == "wf-api" for i in items)
        detail = client.get("/api/workflows/wf-api").json()
        assert detail["name"] == "接口测试"

    def test_save_with_errors_allowed(self, client, factories):
        """保存时即使有 Error 也允许保存，并返回校验报告。"""
        bad = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt=""),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")],
            wf_id="wf-bad")
        r = client.put("/api/workflows/wf-bad", json=bad)
        assert r.status_code == 200
        body = r.json()
        assert any(i["code"] == "EMPTY_PROMPT"
                   for i in body["validation"]["errors"])
        # 确实已保存
        assert client.get("/api/workflows/wf-bad").status_code == 200

    def test_delete(self, client, wf_payload):
        client.put(f"/api/workflows/{wf_payload['id']}", json=wf_payload)
        assert client.delete("/api/workflows/wf-api").status_code == 200
        assert client.get("/api/workflows/wf-api").status_code == 404


class TestValidateEndpoint:
    def test_validate_ok(self, client, wf_payload):
        r = client.post("/api/workflows/validate", json=wf_payload)
        assert r.status_code == 200
        assert r.json()["errors"] == []

    def test_validate_reports_issues(self, client, factories):
        bad = factories["workflow"](
            [factories["start"](), factories["end"]("e1"),
             factories["end"]("e2")],
            [factories["edge"]("start-1", "e1")])  # e2 不可达
        r = client.post("/api/workflows/validate", json=bad)
        codes = {i["code"] for i in r.json()["errors"]}
        assert "UNREACHABLE_NODE" in codes


class TestRunEndpoint:
    def test_run_blocked_on_errors(self, client, factories):
        bad = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt=""),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        r = client.post("/api/run", json={
            "workflow": bad, "inputs": {"user_input": "x"}})
        assert r.status_code == 422
        assert r.json()["detail"]["first_error_node"] is not None

    def test_run_sse_stream(self, client, wf_payload):
        with client.stream("POST", "/api/run", json={
                "workflow": wf_payload,
                "inputs": {"user_input": "sse测试"}}) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers["content-type"]
            events = []
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        kinds = [e["event"] for e in events]
        assert "node_started" in kinds
        assert kinds[-1] == "workflow_finished"
        final = events[-1]
        assert final["result"]["variables"]["result"]["value"] == "sse测试"

    def test_run_creates_run_record(self, client, wf_payload):
        with client.stream("POST", "/api/run", json={
                "workflow": wf_payload, "inputs": {"user_input": "x"}}):
            pass
        runs = client.get("/api/runs").json()
        assert len(runs) >= 1
        detail = client.get(f"/api/runs/{runs[0]['id']}").json()
        assert "node_records" in detail
        assert detail["node_records"]["code-1"]["status"] == "success"

    def test_run_creates_runtime_skill_snapshot_for_agent(
            self, client, factories, tmp_path):
        wf = factories["workflow"](
            [
                factories["start"]("start-1", inputs=[]),
                factories["llm"]("prompt-1", prompt="下一步"),
                factories["code"](
                    "code-1",
                    inputs=[{"name": "arg-1", "type": "string",
                             "required": True}],
                    outputs=[{"name": "result", "type": "string"}],
                    code="def main(params):\n"
                         "    return {\"result\": params[\"arg-1\"]}\n"),
                factories["end"]("end-1"),
            ],
            [
                factories["edge"]("start-1", "prompt-1"),
                factories["edge"]("prompt-1", "code-1"),
                factories["edge"]("code-1", "end-1"),
            ],
            wf_id="wf-runtime", name="中文工作流")
        with client.stream("POST", "/api/run", json={
                "workflow": wf,
                "inputs": {"task-id": "task-api-runtime"}}) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        result = events[-1]["result"]
        prompt = result["waiting_prompt"]
        runtime_dir = tmp_path / "runtime" / "task-api-runtime"

        assert result["status"] == "waiting"
        assert (runtime_dir / "SKILL.md").exists()
        assert (runtime_dir / "scripts" / "main.py").exists()
        assert str(runtime_dir / "scripts" / "main.py") in prompt
        assert "中文工作流" not in prompt
        assert (
            runtime_dir / "scripts" / ".dag2skill_tasks" /
            "task-api-runtime.json"
        ).exists()


class TestDebugEndpoint:
    def test_debug_requires_inputs_first(self, client, wf_payload):
        r = client.post("/api/debug/node", json={
            "workflow": wf_payload, "node_id": "code-1", "inputs": None})
        assert r.status_code == 400

    def test_debug_success_then_cache(self, client, wf_payload):
        r = client.post("/api/debug/node", json={
            "workflow": wf_payload, "node_id": "code-1",
            "inputs": {"user_input": "调试"}})
        assert r.status_code == 200
        assert r.json()["result"] == {"result": "调试"}
        assert r.json()["cache_hit"] is False
        r2 = client.post("/api/debug/node", json={
            "workflow": wf_payload, "node_id": "code-1", "inputs": None})
        assert r2.status_code == 200
        assert r2.json()["result"] == {"result": "调试"}
        assert r2.json()["cache_hit"] is True

    def test_debug_loop_body_code(self, client, factories):
        body_code = factories["code"](
            "body-code",
            inputs=[{
                "name": "item", "type": "string", "source": "item",
            }],
            outputs=[{"name": "result", "type": "string"}],
            code=("def main(params):\n"
                  "    return {\"result\": params[\"item\"].upper()}\n"),
        )
        loop = factories["for_"]("for-1", body_nodes=[body_code])
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[
                {"name": "items", "type": "list"},
            ]), loop, factories["end"]()],
            [factories["edge"]("start-1", "for-1"),
             factories["edge"]("for-1", "end-1")],
        )

        response = client.post("/api/debug/node", json={
            "workflow": wf,
            "node_id": "body-code",
            "inputs": {"item": "loop debug"},
        })

        assert response.status_code == 200
        assert response.json()["result"] == {"result": "LOOP DEBUG"}

    def test_debug_agent_sends_prompt_to_model(self, client):
        r = client.post("/api/debug/agent", json={
            "task-id": "task-api",
            "prompt": "请回复测试",
        })
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["content"] == "接口回复"

    def test_debug_agent_stream_returns_sse_final(self, client):
        with client.stream("POST", "/api/debug/agent/stream", json={
            "task-id": "task-api-stream",
            "prompt": "请回复测试",
        }) as r:
            assert r.status_code == 200
            text = "".join(r.iter_text())
        payloads = []
        for chunk in text.split("\n\n"):
            for line in chunk.splitlines():
                if not line.startswith("data:"):
                    continue
                payloads.append(json.loads(line[5:].strip()))
        assert payloads[-1]["event"] == "agent_final"
        assert payloads[-1]["response"]["content"] == "接口回复"
        assert payloads[-1]["duration_ms"] >= 0


    def test_debug_agent_finalizes_terminal_prompt_to_end(
            self, client, linear_llm_workflow):
        with client.stream("POST", "/api/run", json={
                "workflow": linear_llm_workflow,
                "inputs": {
                    "user_input": "hello",
                    "task-id": "task-terminal-prompt",
                }}) as resp:
            assert resp.status_code == 200
            events = []
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        result = events[-1]["result"]
        assert result["status"] == "waiting"
        assert result["waiting_node"] == "llm-1"

        with client.stream("POST", "/api/debug/agent/stream", json={
            "task-id": "task-terminal-prompt",
            "prompt": result["waiting_prompt"],
        }) as resp:
            assert resp.status_code == 200
            payloads = []
            for chunk in "".join(resp.iter_text()).split("\n\n"):
                for line in chunk.splitlines():
                    if line.startswith("data:"):
                        payloads.append(json.loads(line[5:].strip()))

        task_state = payloads[-1]["task_state"]
        assert task_state["finished"] is True
        assert task_state["waiting-node"] is None
        assert task_state["last-prompt"] is None
        assert task_state["node-statuses"]["llm-1"] == "success"
        assert task_state["node-statuses"]["end-1"] == "success"


class TestConfigEndpoint:
    def test_get_config_masked(self, client):
        r = client.get("/api/config")
        assert r.status_code == 200
        cfg = r.json()
        for prov in cfg["providers"].values():
            assert prov["api_key"] in ("", "******")

    def test_put_config(self, client):
        cfg = client.get("/api/config").json()
        cfg["default_model"] = "new-model"
        cfg["providers"]["openai"]["api_key"] = "sk-real-key"
        r = client.put("/api/config", json=cfg)
        assert r.status_code == 200
        # 读回时 Key 脱敏
        cfg2 = client.get("/api/config").json()
        assert cfg2["providers"]["openai"]["api_key"] == "******"
        assert cfg2["default_model"] == "new-model"

    def test_post_config_test_uses_masked_saved_key(self, client):
        cfg = client.get("/api/config").json()
        cfg["default_model"] = "new-model"
        cfg["providers"]["openai"]["api_key"] = "sk-real-key"
        assert client.put("/api/config", json=cfg).status_code == 200

        masked = client.get("/api/config").json()
        r = client.post("/api/config/test", json=masked)

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["provider"] == "openai"
        assert body["model"] == "new-model"

    def test_put_invalid_config_rejected(self, client):
        cfg = client.get("/api/config").json()
        cfg["providers"]["openai"]["base_url"] = "bad-url"
        assert client.put("/api/config", json=cfg).status_code == 422


class TestFileFlows:
    def test_save_workflow_file_forces_yaml(self, client, wf_payload, tmp_path,
                                            monkeypatch):
        from app import main as app_main

        selected = tmp_path / "chosen.txt"
        monkeypatch.setattr(app_main, "_ask_save_workflow_path",
                            lambda name: str(selected))
        r = client.post("/api/files/save-workflow",
                        json={"workflow": wf_payload})
        assert r.status_code == 200
        saved = Path(r.json()["path"])
        assert saved.suffix == ".yaml"
        assert saved.exists()
        loaded = yaml.safe_load(saved.read_text(encoding="utf-8"))
        assert loaded["id"] == wf_payload["id"]

    def test_open_workflow_file_roundtrip(self, client, wf_payload, tmp_path,
                                          monkeypatch):
        from app import main as app_main

        source = tmp_path / "opened.yaml"
        source.write_text(yaml.safe_dump(wf_payload, allow_unicode=True,
                                         sort_keys=False),
                          encoding="utf-8")
        monkeypatch.setattr(app_main, "_ask_open_workflow_path",
                            lambda: str(source))
        r = client.post("/api/files/open-workflow")
        assert r.status_code == 200
        body = r.json()
        assert body["workflow"]["id"] == wf_payload["id"]
        assert body["workflow"]["nodes"][1]["id"] == "code-1"

    def test_export_skill_uses_native_directory_branch(
            self, client, wf_payload, tmp_path, monkeypatch):
        from app import main as app_main

        monkeypatch.setattr(app_main, "_ask_export_parent_path",
                            lambda name: str(tmp_path))
        monkeypatch.setattr(app_main, "_confirm_overwrite", lambda path: True)
        r = client.post("/api/export", json={"workflow": wf_payload})
        assert r.status_code == 200
        out = Path(r.json()["path"])
        assert out.exists()
        assert (out / "SKILL.md").exists()
        assert (out / "agent_interface.json").exists()


class TestExportEndpoint:
    def test_export_success(self, client, wf_payload, tmp_path):
        r = client.post("/api/export", json={
            "workflow": wf_payload, "target_dir": str(tmp_path / "out")})
        assert r.status_code == 200
        assert (tmp_path / "out" / "SKILL.md").exists()

    def test_export_blocked_on_errors(self, client, factories, tmp_path):
        bad = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt=""),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        r = client.post("/api/export", json={
            "workflow": bad, "target_dir": str(tmp_path / "out2")})
        assert r.status_code == 422
        assert not (tmp_path / "out2").exists()
