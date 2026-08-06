import json
import subprocess
import sys

from app.services.agent_runtime import (
    _code_input_schema, execute_agent_step, prepare_agent_task, read_agent_task)


def _workflow(factories):
    code = factories["code"](
        "code-1",
        inputs=[{
            "name": "arg-1",
            "description": "参数描述",
            "type": "string",
            "required": True,
        }],
        outputs=[{"name": "result", "type": "string"}],
        code="def main(params):\n"
             "    return {\"result\": params[\"arg-1\"].upper()}\n")
    prompt1 = factories["llm"]("prompt-1", prompt="第一步")
    prompt2 = factories["llm"]("prompt-2", prompt="结果: {{ result }}")
    return factories["workflow"](
        [factories["start"]("start-1", inputs=[]), prompt1, code, prompt2,
         factories["end"]()],
        [factories["edge"]("start-1", "prompt-1"),
         factories["edge"]("prompt-1", "code-1"),
         factories["edge"]("code-1", "prompt-2"),
         factories["edge"]("prompt-2", "end-1")],
    )


def test_agent_runtime_step_reaches_next_prompt(
        tmp_path, factories, inline_code):
    wf = _workflow(factories)
    prepare_agent_task(tmp_path, wf, {
        "status": "waiting",
        "task-id": "task-agent",
        "waiting_node": "prompt-1",
        "variables": {
            "task-id": {"type": "string", "value": "task-agent",
                        "owner": "system"},
        },
    })

    result = execute_agent_step(tmp_path, "task-agent", "code-1",
                                {"arg-1": "hello"}, inline_code)

    assert result["status"] == "waiting"
    assert result["waiting_node"] == "prompt-2"
    assert "结果: HELLO" in result["prompt"]
    state = read_agent_task(tmp_path, "task-agent")
    assert state["variables"]["result"] == "HELLO"


def test_agent_runtime_uses_bound_variables_without_agent_input(
        tmp_path, factories, inline_code):
    wf = _workflow(factories)
    code = next(node for node in wf["nodes"] if node["id"] == "code-1")
    code["config"]["inputs"] = [
        {"name": "profile", "type": "string", "required": True,
         "source": "profile"},
        {"name": "message", "type": "string", "required": True},
    ]
    code["config"]["code"] = (
        "def main(params):\n"
        "    return {\"result\": params[\"profile\"] + \": \" + params[\"message\"]}\n")
    prepare_agent_task(tmp_path, wf, {
        "status": "waiting",
        "task-id": "task-bound-input",
        "waiting_node": "prompt-1",
        "variables": {
            "task-id": {"type": "string", "value": "task-bound-input",
                        "owner": "system"},
            "profile": {"type": "string", "value": "Ada", "owner": "start"},
        },
    })

    assert _code_input_schema(code) == [{
        "name": "message", "description": "", "type": "string",
        "required": True,
    }]
    result = execute_agent_step(tmp_path, "task-bound-input", "code-1",
                                {"message": "hello"}, inline_code)

    assert result["status"] == "waiting"
    assert "Ada: hello" in result["prompt"]


def test_agent_runtime_normalizes_code_bool_output(
        tmp_path, factories, inline_code):
    """Code 节点输出 bool 应归一化为 int（系统无 bool 类型），
    后续声明 int 的下游节点读取时不报类型错误。"""
    make = factories["code"](
        "make-1",
        inputs=[],
        outputs=[{"name": "flag", "type": "int"}],
        code="def main():\n    return {\"flag\": True}\n")
    use = factories["code"](
        "use-1",
        inputs=[{"name": "flag", "type": "int", "source": "flag"}],
        outputs=[{"name": "out", "type": "int"}],
        code="def main(flag):\n    return {\"out\": flag + 1}\n")
    prompt1 = factories["llm"]("prompt-1", prompt="检查 {{ flag }}")
    prompt2 = factories["llm"]("prompt-2", prompt="完成")
    wf = factories["workflow"](
        [factories["start"]("start-1", inputs=[]), make, prompt1, use, prompt2,
         factories["end"]()],
        [factories["edge"]("start-1", "make-1"),
         factories["edge"]("make-1", "prompt-1"),
         factories["edge"]("prompt-1", "use-1"),
         factories["edge"]("use-1", "prompt-2"),
         factories["edge"]("prompt-2", "end-1")],
    )
    prepare_agent_task(tmp_path, wf, {
        "status": "waiting",
        "task-id": "task-bool",
        "waiting_node": "prompt-1",
        "variables": {
            "task-id": {"type": "string", "value": "task-bool",
                        "owner": "system"},
        },
    })

    r1 = execute_agent_step(tmp_path, "task-bool", "make-1", {}, inline_code)
    assert r1["status"] == "waiting"
    state = read_agent_task(tmp_path, "task-bool")
    # bool True 归一化为 int 1
    assert state["variables"]["flag"] == 1
    assert state["variables"]["flag"] is not True

    r2 = execute_agent_step(tmp_path, "task-bool", "use-1", {}, inline_code)
    assert r2["status"] == "waiting"
    state = read_agent_task(tmp_path, "task-bool")
    assert state["variables"]["out"] == 2


def test_agent_runtime_reads_utf8_bom_task_state(tmp_path):
    path = tmp_path / "tasks" / "task-bom.json"
    path.parent.mkdir()
    expected = {"task-id": "task-bom", "variables": {"value": 1}}
    path.write_text(json.dumps(expected), encoding="utf-8-sig")

    assert read_agent_task(tmp_path, "task-bom") == expected


def test_root_main_cli_uses_persisted_task(
        tmp_path, factories, inline_code):
    wf = _workflow(factories)
    prepare_agent_task(tmp_path, wf, {
        "status": "waiting",
        "task-id": "task-cli",
        "waiting_node": "prompt-1",
        "variables": {
            "task-id": {"type": "string", "value": "task-cli",
                        "owner": "system"},
        },
    })
    cfg = {
        "python_path": sys.executable,
        "providers": {
            "openai": {"api_key": "sk-test",
                       "base_url": "https://api.openai.com/v1"},
        },
        "default_provider": "openai",
        "default_model": "test-model",
        "timeout_seconds": 60,
        "max_retries": 0,
        "idle_timeout": 600,
        "max_task_runtime": 3600,
        "shell_tool": {"enabled": True, "shell": "auto",
                       "timeout_seconds": 60, "max_calls": 3},
    }
    import yaml
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "main.py",
         "--task-id", "task-cli",
         "--step-id", "code-1",
         "--step-param", json.dumps({"arg-1": "hello"},
                                    ensure_ascii=False)],
        cwd=".",
        env={**__import__("os").environ,
             "DAG2SKILL_DATA_DIR": str(tmp_path),
             "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=10)

    assert proc.returncode == 0, proc.stderr
    assert "结果: HELLO" in proc.stdout
