"""本地配置：`~/.hxmv/config.json`——存 API Key 之类的凭据。

为什么要有它（而不是只认环境变量）：
    老大要"配一次就好"。环境变量换个终端就没了、deamon 也未必继承；
    落到 `~/.hxmv/config.json`（权限 600）后，CLI 与 Web daemon 都读得到。

优先级：**环境变量 > 配置文件**（临时换 key 不动文件）。
"""
from __future__ import annotations

import json
import os
import stat

CONFIG_PATH = os.environ.get("HXMV_CONFIG", os.path.expanduser("~/.hxmv/config.json"))

# provider → 环境变量名（环境变量优先）
_ENV_KEYS = {
    "zhipu": ("HXMV_ZHIPU_KEY", "ZHIPUAI_API_KEY", "ZHIPU_API_KEY", "BIGMODEL_API_KEY"),
    "kling": ("HXMV_KLING_KEY",),
}


def load() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(data: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)   # 600：只有主人能读


def api_key(provider: str) -> str:
    """取某 provider 的 API Key：环境变量 → 配置文件 → 空串。"""
    for env in _ENV_KEYS.get(provider, ()):
        if os.environ.get(env):
            return os.environ[env].strip()
    key = (load().get("providers", {}).get(provider, {}) or {}).get("api_key", "")
    return str(key).strip()


def set_api_key(provider: str, key: str) -> str:
    """写入配置文件（返回脱敏后的回显，别把明文打到日志里）。"""
    data = load()
    data.setdefault("providers", {}).setdefault(provider, {})["api_key"] = key.strip()
    save(data)
    return mask(key)


def mask(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）"


def configured(provider: str) -> bool:
    return bool(api_key(provider))
