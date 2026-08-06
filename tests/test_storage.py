"""Workflow YAML 存储测试（PRD 5.1）。

契约：app.services.storage
- save_workflow(dir, wf_dict) -> Path
- load_workflow(path) -> dict
- list_workflows(dir) -> list[{"id", "name", "path"}]
- delete_workflow(path) -> None
"""
import pytest

from app.services.storage import (  # TDD：尚不存在
    delete_workflow, list_workflows, load_workflow, save_workflow,
)


class TestStorage:
    def test_save_and_load_roundtrip(self, tmp_path, simple_workflow):
        path = save_workflow(tmp_path, simple_workflow)
        assert path.exists() and path.suffix in (".yaml", ".yml")
        loaded = load_workflow(path)
        assert loaded["id"] == simple_workflow["id"]
        assert loaded["nodes"] == simple_workflow["nodes"]
        assert loaded["edges"] == simple_workflow["edges"]

    def test_unicode_preserved(self, tmp_path, simple_workflow):
        simple_workflow["name"] = "中文工作流名字"
        path = save_workflow(tmp_path, simple_workflow)
        assert load_workflow(path)["name"] == "中文工作流名字"

    def test_filename_from_id(self, tmp_path, simple_workflow):
        path = save_workflow(tmp_path, simple_workflow)
        assert "wf-test" in path.name

    def test_list_workflows(self, tmp_path, factories):
        save_workflow(tmp_path, factories["workflow"](
            [factories["start"](), factories["end"]()],
            [factories["edge"]("start-1", "end-1")],
            wf_id="wf-a", name="甲"))
        save_workflow(tmp_path, factories["workflow"](
            [factories["start"](), factories["end"]()],
            [factories["edge"]("start-1", "end-1")],
            wf_id="wf-b", name="乙"))
        items = list_workflows(tmp_path)
        names = {i["name"] for i in items}
        assert names == {"甲", "乙"}
        assert all("path" in i and "id" in i for i in items)

    def test_list_empty_dir(self, tmp_path):
        assert list_workflows(tmp_path) == []

    def test_load_corrupted_raises(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("{{{bad: [", encoding="utf-8")
        with pytest.raises(ValueError):
            load_workflow(p)

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_workflow(tmp_path / "nope.yaml")

    def test_delete(self, tmp_path, simple_workflow):
        path = save_workflow(tmp_path, simple_workflow)
        delete_workflow(path)
        assert not path.exists()

    def test_skip_invalid_files_on_list(self, tmp_path, simple_workflow):
        save_workflow(tmp_path, simple_workflow)
        (tmp_path / "junk.yaml").write_text("{{{junk: [", encoding="utf-8")
        items = list_workflows(tmp_path)
        assert len(items) == 1
