"""Config 配置测试（PRD 第 10 章）。

契约：app.services.config_store
- load_config(path) -> dict（不存在时自动创建默认配置）
- save_config(path, cfg) -> None（保存前校验）
- validate_config(cfg) -> list[str]（错误列表，空为通过）
- ConfigCorruptedError：文件损坏时抛出，禁止覆盖
"""
import pytest
import yaml

from app.services.config_store import (  # TDD：尚不存在
    ConfigCorruptedError, load_config, save_config, validate_config,
)


class TestLoad:
    def test_auto_create_when_missing(self, tmp_path):
        p = tmp_path / "config.yaml"
        cfg = load_config(p)
        assert p.exists()
        assert "providers" in cfg
        assert "default_provider" in cfg
        assert cfg["timeout_seconds"] == 60
        assert cfg["max_retries"] == 2

    def test_load_existing(self, tmp_path, default_config):
        p = tmp_path / "config.yaml"
        p.write_text(yaml.safe_dump(default_config), encoding="utf-8")
        cfg = load_config(p)
        assert cfg["default_model"] == "test-model"

    def test_corrupted_raises_and_not_overwritten(self, tmp_path):
        p = tmp_path / "config.yaml"
        p.write_text("{{{ 不是yaml: [", encoding="utf-8")
        with pytest.raises(ConfigCorruptedError):
            load_config(p)
        # 禁止覆盖：损坏内容仍在
        assert p.read_text(encoding="utf-8") == "{{{ 不是yaml: ["


class TestValidate:
    def test_valid_config(self, default_config):
        assert validate_config(default_config) == []

    def test_empty_api_key_allowed(self, default_config):
        """API Key 为空允许保存。"""
        default_config["providers"]["openai"]["api_key"] = ""
        assert validate_config(default_config) == []

    def test_invalid_base_url(self, default_config):
        default_config["providers"]["compatible"]["base_url"] = "ftp://x"
        errors = validate_config(default_config)
        assert any("base_url" in e.lower() or "url" in e.lower()
                   for e in errors)

    def test_base_url_must_be_http(self, default_config):
        default_config["providers"]["openai"]["base_url"] = "not-a-url"
        assert validate_config(default_config) != []

    def test_missing_default_provider(self, default_config):
        default_config["default_provider"] = "不存在"
        assert validate_config(default_config) != []

    def test_missing_default_model(self, default_config):
        default_config["default_model"] = ""
        assert validate_config(default_config) != []


class TestSave:
    def test_save_and_reload(self, tmp_path, default_config):
        p = tmp_path / "config.yaml"
        save_config(p, default_config)
        assert load_config(p)["default_model"] == "test-model"

    def test_save_invalid_rejected(self, tmp_path, default_config):
        default_config["default_provider"] = "ghost"
        with pytest.raises(ValueError):
            save_config(tmp_path / "config.yaml", default_config)

    def test_api_key_not_in_logs_repr(self, tmp_path, default_config):
        """Key 明文存文件，但导出的日志工具函数需脱敏。"""
        from app.services.config_store import redact_config
        default_config["providers"]["openai"]["api_key"] = "sk-secret-123"
        redacted = redact_config(default_config)
        assert redacted["providers"]["openai"]["api_key"] == "******"
        # 原对象不被修改
        assert default_config["providers"]["openai"]["api_key"] == "sk-secret-123"
