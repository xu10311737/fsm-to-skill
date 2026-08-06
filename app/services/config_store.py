"""本地 Config 读写与校验（PRD 第 10 章）。

配置文件为 YAML。文件缺失时自动创建默认配置；文件损坏时抛
ConfigCorruptedError 且绝不覆盖；保存前强制校验。
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import yaml


class ConfigCorruptedError(Exception):
    """配置文件损坏：禁止覆盖，提示用户修复。"""


DEFAULT_CONFIG: dict[str, Any] = {
    "python_path": sys.executable,
    "providers": {
        "openai": {"api_key": "",
                   "base_url": "https://api.openai.com/v1"},
        "compatible": {"api_key": "",
                       "base_url": "http://localhost:8000/v1"},
        "anthropic": {"api_key": "",
                      "base_url": "https://api.anthropic.com"},
    },
    "default_provider": "openai",
    "default_model": "",
    "stream": True,
    "timeout_seconds": 60,
    "max_retries": 2,
    "idle_timeout": 600,
    "max_task_runtime": 3600,
    "shell_tool": {
        "enabled": False,
        "shell": "auto",
        "timeout_seconds": 60,
        "max_calls": 100,
    },
}


def load_config(path: str | Path) -> dict[str, Any]:
    """加载配置；不存在时自动创建默认配置；损坏时抛 ConfigCorruptedError。"""
    p = Path(path)
    if not p.exists():
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        return cfg
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigCorruptedError(
            f"配置文件损坏（{p}）：{e}。请手动修复或删除后重启。") from e
    if not isinstance(data, dict):
        raise ConfigCorruptedError(
            f"配置文件损坏（{p}）：内容不是有效的配置结构，"
            f"请手动修复或删除后重启。")
    return _with_defaults(data)


def save_config(path: str | Path, config: dict[str, Any]) -> None:
    """保存前校验，非法配置抛 ValueError。"""
    errors = validate_config(config)
    if errors:
        raise ValueError("配置校验失败: " + "; ".join(errors))
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")


def validate_config(config: dict[str, Any]) -> list[str]:
    """返回错误信息列表，空列表为通过。"""
    errors: list[str] = []
    providers = config.get("providers")
    if not isinstance(providers, dict) or not providers:
        errors.append("providers 必须是非空映射")
        providers = {}
    for name, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            errors.append(f"providers.{name} 必须是映射结构")
            continue
        base_url = pcfg.get("base_url", "")
        if not isinstance(base_url, str) or not base_url.startswith(
                ("http://", "https://")):
            errors.append(
                f"providers.{name}.base_url 必须是 http(s) URL: {base_url!r}")
    default_provider = config.get("default_provider")
    if default_provider not in providers:
        errors.append(
            f"default_provider {default_provider!r} 不在 providers 中")
    if not config.get("default_model"):
        errors.append("default_model 不能为空")
    timeout = config.get("timeout_seconds", 60)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        errors.append("timeout_seconds 必须是正数")
    retries = config.get("max_retries", 2)
    if not isinstance(retries, int) or retries < 0:
        errors.append("max_retries 必须是非负整数")
    idle_timeout = config.get("idle_timeout", 600)
    if not isinstance(idle_timeout, (int, float)) or idle_timeout <= 0:
        errors.append("idle_timeout 必须是正数（秒）")
    max_task_runtime = config.get("max_task_runtime", 3600)
    if not isinstance(max_task_runtime, (int, float)) or max_task_runtime <= 0:
        errors.append("max_task_runtime 必须是正数（秒）")
    if (isinstance(idle_timeout, (int, float)) and
            isinstance(max_task_runtime, (int, float)) and
            idle_timeout > max_task_runtime):
        errors.append("idle_timeout 不能大于 max_task_runtime")
    shell_tool = config.get("shell_tool", {})
    if shell_tool is None:
        shell_tool = {}
    if not isinstance(shell_tool, dict):
        errors.append("shell_tool 必须是映射结构")
    else:
        shell = shell_tool.get("shell", "auto")
        if shell not in ("auto", "bash", "sh", "powershell", "pwsh"):
            errors.append("shell_tool.shell 必须是 auto/bash/sh/powershell/pwsh")
        tool_timeout = shell_tool.get("timeout_seconds", 60)
        if not isinstance(tool_timeout, (int, float)) or tool_timeout <= 0:
            errors.append("shell_tool.timeout_seconds 必须是正数")
        max_calls = shell_tool.get("max_calls", 100)
        if not isinstance(max_calls, int) or max_calls < 0:
            errors.append("shell_tool.max_calls 必须是非负整数")
    return errors


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """返回 API Key 脱敏后的副本（原对象不变）。"""
    redacted = copy.deepcopy(config)
    for pcfg in (redacted.get("providers") or {}).values():
        if isinstance(pcfg, dict) and pcfg.get("api_key"):
            pcfg["api_key"] = "******"
    return redacted


def _with_defaults(config: dict[str, Any]) -> dict[str, Any]:
    """加载旧配置时补齐新字段，不覆盖用户已有值。"""
    merged = copy.deepcopy(DEFAULT_CONFIG)
    _deep_update(merged, config)
    return merged


def _deep_update(base: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
