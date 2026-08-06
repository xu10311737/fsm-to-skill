"""Skill 导出测试（PRD 第 8 章）。

契约：app.services.exporter.export_skill(wf_dict, target_dir,
                                        overwrite=False) -> Path
结构：
  skill-package/
  ├── SKILL.md
  ├── inference/prompt-N.md
  ├── scripts/main.py + <code节点>.py
  └── workflow.yaml
"""
import json
import subprocess
import sys
import time

import pytest
import yaml

from app.services.exporter import export_skill  # TDD：尚不存在


@pytest.fixture
def exportable_workflow(factories):
    code = factories["code"](
        "code-1", outputs=[{"name": "upper", "type": "string"}],
        code="def main(user_input):\n"
             "    return {\"result\": user_input.upper()}\n")
    llm = factories["llm"]("llm-1", prompt="翻译: {{ upper }}")
    wf = factories["workflow"](
        [factories["start"](), code, llm, factories["end"]()],
        [factories["edge"]("start-1", "code-1"),
         factories["edge"]("code-1", "llm-1"),
         factories["edge"]("llm-1", "end-1")],
        wf_id="wf-export", name="导出测试")
    return wf


class TestExportStructure:
    def test_full_structure(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "skill-package")
        assert (out / "SKILL.md").exists()
        assert (out / "workflow.yaml").exists()
        assert (out / "scripts" / "main.py").exists()
        assert (out / "scripts" / "code-1.py").exists()
        assert (out / "inference" / "prompt-1.md").exists()

    def test_code_file_content(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        content = (out / "scripts" / "code-1.py").read_text(encoding="utf-8")
        assert "def main(user_input):" in content

    def test_prompt_template_raw(self, tmp_path, exportable_workflow):
        """inference 保存渲染前的模板。"""
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        tpl = (out / "inference" / "prompt-1.md").read_text(encoding="utf-8")
        assert tpl == "翻译: {{ upper }}"

    def test_workflow_yaml_valid(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        data = yaml.safe_load(
            (out / "workflow.yaml").read_text(encoding="utf-8"))
        assert data["id"] == "wf-export"
        assert len(data["nodes"]) == 4
        assert len(data["edges"]) == 3

    def test_multiple_prompts_numbered(self, tmp_path, factories):
        llm1 = factories["llm"]("llm-1", prompt="一")
        llm2 = factories["llm"]("llm-2", prompt="二")
        wf = factories["workflow"](
            [factories["start"](), llm1, llm2, factories["end"]()],
            [factories["edge"]("start-1", "llm-1"),
             factories["edge"]("llm-1", "llm-2"),
             factories["edge"]("llm-2", "end-1")])
        out = export_skill(wf, tmp_path / "pkg")
        assert (out / "inference" / "prompt-1.md").exists()
        assert (out / "inference" / "prompt-2.md").exists()


class TestSkillMd:
    def test_generated_main_uses_bound_variable_from_task_state(
            self, tmp_path, factories):
        code = factories["code"](
            "agent-code",
            inputs=[
                {"name": "profile", "type": "string", "required": True,
                 "source": "profile"},
                {"name": "note", "type": "string", "required": True},
            ],
            outputs=[{"name": "result", "type": "string"}],
            code=(
                "def main(params):\n"
                "    return {\"result\": params[\"profile\"] + \":\" + params[\"note\"]}\n"),
        )
        prompt = factories["llm"]("prompt-1", prompt="Prepare {{ profile }}")
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[
                {"name": "profile", "type": "string"},
            ]), prompt, code, factories["end"]()],
            [factories["edge"]("start-1", "prompt-1"),
             factories["edge"]("prompt-1", "agent-code"),
             factories["edge"]("agent-code", "end-1")],
        )
        out = export_skill(wf, tmp_path / "pkg-bound-variable")
        task_path = out / "scripts" / ".dag2skill_tasks" / "task-bound.json"
        task_path.parent.mkdir()
        now = time.time()
        task_path.write_text(json.dumps({
            "task-id": "task-bound",
            "created-at": now,
            "updated-at": now,
            "variables": {"task-id": "task-bound", "profile": "Ada"},
            "waiting-node": "prompt-1",
            "node-statuses": {"prompt-1": "waiting"},
            "finished": False,
        }), encoding="utf-8-sig")

        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-bound",
             "--step-id", "agent-code",
             "--step-param", json.dumps({"note": "ready"})],
            capture_output=True, text=True, encoding="utf-8", timeout=10)

        assert proc.returncode == 0, proc.stderr
        assert "Ada:ready" in proc.stdout
        interface = json.loads(
            (out / "agent_interface.json").read_text(encoding="utf-8"))
        entry = next(item for item in interface["entries"]
                     if item["node_id"] == "agent-code")
        assert [item["name"] for item in entry["input_schema"]] == ["note"]

    def test_uses_standard_skill_md_template(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        md = (out / "SKILL.md").read_text(encoding="utf-8")
        assert md.startswith("---\n")
        assert 'name: "导出测试"' in md
        assert 'description: ""' in md
        assert "# 任务描述" in md
        assert "# 最终输出" in md
        assert "# SOP" in md
        assert "你需要在状态机的指引下,一步步完成任务, let us step by step!" in md
        assert "**翻译: {{ upper }}**" in md

    def test_contains_workflow_name(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        md = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "导出测试" in md

    def test_no_legacy_input_table(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        md = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "| 参数 |" not in md

    def test_no_legacy_llm_output_name(self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        md = (out / "SKILL.md").read_text(encoding="utf-8")
        assert "llm-1_output" not in md

    def test_generated_main_py_importable(self, tmp_path, exportable_workflow):
        """main.py 是状态机入口，语法必须有效。"""
        import ast
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        src = (out / "scripts" / "main.py").read_text(encoding="utf-8")
        ast.parse(src)  # 不抛异常即通过
        assert "def main" in src or "if __name__" in src

    def test_agent_interface_uses_argparse_schema(
            self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        data = json.loads(
            (out / "agent_interface.json").read_text(encoding="utf-8"))
        entry = data["entries"][0]
        assert "--task-id" in entry["command_template"]
        assert "--step-id" in entry["command_template"]
        assert "code-1" in entry["command_template"]
        assert "scripts" in entry["command_template"]
        assert "main.py" in entry["command_template"]
        # Bound workflow variables are supplied from persisted task state,
        # so the Agent must not be asked to provide them again.
        assert entry["input_schema"] == []
        assert "--task-id" in data["agent_envelope"]["agent_command"]
        assert "--step-id" in data["agent_envelope"]["agent_command"]
        assert "<code_node_id>" in data["agent_envelope"]["agent_command"]
        assert data["agent_envelope"]["task-id"] == "<task-id>"
        assert data["agent_envelope"]["step-id"] == "<actual_code_node_id>"
        assert data["agent_envelope"]["step-param"] == {"<arg>": "<value>"}

    def test_command_template_uses_fixed_step_param_placeholder(
            self, tmp_path, exportable_workflow):
        from app.engine.command_format import format_step_command

        out = export_skill(exportable_workflow, tmp_path / "pkg")
        command = format_step_command({
            "shell": "auto",
            "python_path": r"C:\Python314\python.exe",
            "main_path": str(out / "scripts" / "main.py"),
        }, "task-pwsh", "code-1", {"arg-1": "你好"})

        assert r"C:\Python314\python.exe" in command
        assert "--task-id task-pwsh" in command
        assert "--step-id code-1" in command
        assert "--step-param-b64 '" in command
        assert "--task_id" not in command
        assert "--step-param <下文中实际节点入参>" not in command
        assert "\\\n" not in command  # single-line, no POSIX continuation
        # the base64 payload decodes back to the original step params
        import base64 as _b64
        import json as _json
        b64 = command.split("--step-param-b64 '")[1].split("'")[0]
        assert _json.loads(_b64.urlsafe_b64decode(b64)) == {"arg-1": "你好"}

    def test_command_template_placeholder_can_be_filled_and_run(
            self, tmp_path, factories):
        code = factories["code"](
            "code-1",
            inputs=[{"name": "arg-1", "type": "string", "required": True}],
            outputs=[{"name": "upper", "type": "string"}],
            code="def main(params):\n"
                 "    return {\"upper\": params[\"arg-1\"].upper()}\n")
        prompt = factories["llm"]("prompt-1", prompt="结果: {{ upper }}")
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[]), code, prompt,
             factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "prompt-1"),
             factories["edge"]("prompt-1", "end-1")])
        out = export_skill(wf, tmp_path / "pkg-task-id-alias")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task_id", "task-placeholder",
             "--step-id", "code-1",
             "--step-param", json.dumps({"arg-1": "hello"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)

        assert proc.returncode == 0, proc.stderr
        assert "结果: HELLO" in proc.stdout

    def test_generated_main_py_cli_reaches_prompt(
            self, tmp_path, exportable_workflow):
        out = export_skill(exportable_workflow, tmp_path / "pkg")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-export",
             "--step-id", "code-1",
             "--step-param", json.dumps({"user_input": "hi"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert "翻译: HI" in proc.stdout
        assert "task-id: task-export" in proc.stdout

    def test_generated_main_appends_next_code_schema(
            self, tmp_path, factories):
        code1 = factories["code"](
            "code-1", outputs=[{"name": "upper", "type": "string"}],
            code="def main(user_input):\n"
                 "    return {\"result\": user_input.upper()}\n")
        prompt1 = factories["llm"]("llm-1", prompt="第一步: {{ upper }}")
        code2 = factories["code"](
            "code-2",
            inputs=[{
                "name": "arg2",
                "description": "下一步参数",
                "type": "string",
                "required": True,
            }],
            outputs=[{"name": "second", "type": "string"}],
            code="def main(arg2):\n    return {\"result\": arg2}\n")
        prompt2 = factories["llm"]("llm-2", prompt="第二步: {{ second }}")
        wf = factories["workflow"](
            [factories["start"](), code1, prompt1, code2, prompt2,
             factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "llm-1"),
             factories["edge"]("llm-1", "code-2"),
             factories["edge"]("code-2", "llm-2"),
             factories["edge"]("llm-2", "end-1")])
        out = export_skill(wf, tmp_path / "pkg")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-next",
             "--step-id", "code-1",
             "--step-param", json.dumps({"user_input": "go"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert "第一步: GO" in proc.stdout
        assert "task-id: task-next" in proc.stdout
        removed_heading = "下一步 Code 输入 " + "schema"
        assert removed_heading not in proc.stdout
        assert "## 下一个step待执行命令:" in proc.stdout
        assert "**step-param 入参说明**:" in proc.stdout
        assert "--step-param-b64" in proc.stdout
        assert "--step-param <下文中实际节点入参>" not in proc.stdout
        assert "code-2" in proc.stdout
        assert "| arg2 | string | 是 | 下一步参数 |" in proc.stdout

    def test_generated_main_accepts_hyphen_step_param(
            self, tmp_path, factories):
        code = factories["code"](
            "code-1",
            inputs=[{
                "name": "arg-1",
                "description": "连字符参数",
                "type": "string",
                "required": True,
            }],
            outputs=[{"name": "upper", "type": "string"}],
            code="def main(arg_1):\n    return {\"upper\": arg_1.upper()}\n")
        prompt = factories["llm"]("prompt-1", prompt="结果: {{ upper }}")
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[]), code, prompt,
             factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "prompt-1"),
             factories["edge"]("prompt-1", "end-1")])
        out = export_skill(wf, tmp_path / "pkg-hyphen")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-hyphen",
             "--step-id", "code-1",
             "--step-param", json.dumps({"arg-1": "hello"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert "结果: HELLO" in proc.stdout

    def test_generated_main_accepts_params_dict_style(
            self, tmp_path, factories):
        code = factories["code"](
            "code-1",
            inputs=[{
                "name": "arg-1",
                "description": "连字符参数",
                "type": "string",
                "required": True,
            }],
            outputs=[{"name": "upper", "type": "string"}],
            code="def main(params):\n"
                 "    return {\"upper\": params[\"arg-1\"].upper()}\n")
        prompt = factories["llm"]("prompt-1", prompt="结果: {{ upper }}")
        wf = factories["workflow"](
            [factories["start"]("start-1", inputs=[]), code, prompt,
             factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "prompt-1"),
             factories["edge"]("prompt-1", "end-1")])
        out = export_skill(wf, tmp_path / "pkg-params")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-params",
             "--step-id", "code-1",
             "--step-param", json.dumps({"arg-1": "hello"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert "结果: HELLO" in proc.stdout

    def test_generated_main_pauses_on_prompt_inside_for_body(
            self, tmp_path, factories):
        code = factories["code"](
            "code-1",
            outputs=[{"name": "items", "type": "list"}],
            code="def main(user_input):\n"
                 "    return {\"items\": [user_input, \"second\"]}\n")
        prompt = factories["llm"]("body-prompt", prompt="处理: {{ item }}")
        should_not_run = factories["code"](
            "body-code-after-prompt",
            inputs=[{"name": "arg-1", "type": "string", "required": True}],
            code="def main(params):\n"
                 "    return {\"result\": params[\"arg-1\"]}\n")
        loop = factories["for_"](
            "for-1",
            list_source="items",
            body_nodes=[prompt, should_not_run],
            body_edges=[factories["edge"](
                "body-prompt", "body-code-after-prompt")],
        )
        wf = factories["workflow"](
            [factories["start"](), code, loop, factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "for-1"),
             factories["edge"]("for-1", "end-1")])
        out = export_skill(wf, tmp_path / "pkg-for-prompt")
        proc = subprocess.run(
            [sys.executable, str(out / "scripts" / "main.py"),
             "--task-id", "task-for-prompt",
             "--step-id", "code-1",
             "--step-param", json.dumps({"user_input": "first"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert proc.returncode == 0, proc.stderr
        assert "处理: first" in proc.stdout
        assert "循环上下文: 当前轮次 1/2" in proc.stdout
        assert "arg-1" not in proc.stderr

    def test_generated_main_continues_after_for_body_code(
            self, tmp_path, factories):
        code = factories["code"](
            "code-1",
            outputs=[{"name": "items", "type": "list"}],
            code="def main(user_input):\n"
                 "    return {\"items\": [user_input, \"second\"]}\n")
        body_prompt = factories["llm"](
            "body-prompt", prompt="处理循环项: {{ item }}")
        body_code = factories["code"](
            "body-code",
            inputs=[
                {"name": "item", "type": "string", "required": True},
                {"name": "index", "type": "int", "required": True},
            ],
            outputs=[{"name": "result", "type": "string"}],
            code="def main(params):\n"
                 "    return {\"result\": params[\"item\"]}\n")
        loop = factories["for_"](
            "for-1",
            list_source="items",
            body_nodes=[body_prompt, body_code],
            body_edges=[factories["edge"]("body-prompt", "body-code")],
        )
        wf = factories["workflow"](
            [factories["start"](), code, loop, factories["end"]()],
            [factories["edge"]("start-1", "code-1"),
             factories["edge"]("code-1", "for-1"),
             factories["edge"]("for-1", "end-1")])
        out = export_skill(wf, tmp_path / "pkg-for-continuation")
        main = out / "scripts" / "main.py"
        task_dir = out / "scripts" / ".dag2skill_tasks"

        first = subprocess.run(
            [sys.executable, str(main),
             "--task-id", "task-for-continuation",
             "--step-id", "code-1",
             "--step-param", json.dumps({"user_input": "first"},
                                        ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert first.returncode == 0, first.stderr
        state = json.loads(
            (task_dir / "task-for-continuation.json").read_text(
                encoding="utf-8"))
        assert state["resume"]["for_node"] == "for-1"
        assert state["resume"]["index"] == 0
        assert state["waiting-node"] == "body-prompt"
        assert state["node-statuses"]["code-1"] == "success"
        assert state["node-statuses"]["for-1"] == "waiting"
        assert state["node-statuses"]["body-prompt"] == "waiting"
        assert "body-code" in first.stdout

        second = subprocess.run(
            [sys.executable, str(main),
             "--task-id", "task-for-continuation",
             "--step-id", "body-code",
             "--step-param", json.dumps(
                 {"item": "first", "index": 0}, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert second.returncode == 0, second.stderr
        assert "循环上下文: 当前轮次 2/2" in second.stdout
        state = json.loads(
            (task_dir / "task-for-continuation.json").read_text(
                encoding="utf-8"))
        assert state["resume"]["index"] == 1
        assert state["waiting-node"] == "body-prompt"
        assert state["node-statuses"]["body-code"] == "success"
        assert state["node-statuses"]["for-1"] == "waiting"

        third = subprocess.run(
            [sys.executable, str(main),
             "--task-id", "task-for-continuation",
             "--step-id", "body-code",
             "--step-param", json.dumps(
                 {"item": "second", "index": 1}, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert third.returncode == 0, third.stderr
        assert "任务已完成" in third.stdout
        state = json.loads(
            (task_dir / "task-for-continuation.json").read_text(
                encoding="utf-8"))
        assert state["finished"] is True
        assert state["waiting-node"] is None
        assert state["node-statuses"]["body-prompt"] == "success"
        assert state["node-statuses"]["body-code"] == "success"
        assert state["node-statuses"]["for-1"] == "success"
        assert state["node-statuses"]["end-1"] == "success"


class TestExportRules:
    def test_no_auto_overwrite(self, tmp_path, exportable_workflow):
        target = tmp_path / "pkg"
        export_skill(exportable_workflow, target)
        with pytest.raises(FileExistsError):
            export_skill(exportable_workflow, target)

    def test_overwrite_explicit(self, tmp_path, exportable_workflow):
        target = tmp_path / "pkg"
        export_skill(exportable_workflow, target)
        out = export_skill(exportable_workflow, target, overwrite=True)
        assert (out / "SKILL.md").exists()

    def test_export_blocked_on_errors(self, tmp_path, factories):
        """存在 Error 时禁止导出。"""
        wf = factories["workflow"](
            [factories["start"](), factories["llm"]("l1", prompt=""),
             factories["end"]()],
            [factories["edge"]("start-1", "l1"),
             factories["edge"]("l1", "end-1")])
        with pytest.raises(ValueError, match="校验|Error|error"):
            export_skill(wf, tmp_path / "pkg")
        assert not (tmp_path / "pkg").exists()

    def test_failure_cleans_partial_dir(self, tmp_path,
                                        exportable_workflow, monkeypatch):
        """导出失败不保留不完整目录。"""
        import app.services.exporter as exporter

        orig = exporter.export_skill

        def boom(*a, **k):
            target = a[1]
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text("partial", encoding="utf-8")
            raise RuntimeError("中途失败")

        monkeypatch.setattr(exporter, "_write_skill_md",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("中途失败")))
        with pytest.raises(RuntimeError):
            orig(exportable_workflow, tmp_path / "pkg")
        assert not (tmp_path / "pkg").exists()
