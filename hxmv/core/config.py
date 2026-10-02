"""本地配置：`~/.hxmv/config.json`——存 API Key 与档位选择。

为什么要有它（而不是只认环境变量）：
    老大要"配一次就好"。环境变量换个终端就没了、daemon 也未必继承；
    落到 `~/.hxmv/config.json`（权限 600）后，CLI 与 Web daemon 都读得到。

优先级：**环境变量 > 配置文件**（临时换 key 不动文件）。
provider 的环境变量名从 `providers/registry.py` 读——**别在这里再列一遍 provider 名字**，
加一家 API 时只改注册表。
"""
from __future__ import annotations

import json
import os
import stat

CONFIG_PATH = os.environ.get("HXMV_CONFIG", os.path.expanduser("~/.hxmv/config.json"))

# 历史环境变量别名（换注册表之前就在用的名字，别改，用户的 shell 里可能有）
_ENV_ALIASES = {
    ("zhipu", "video_model"): "HXMV_ZHIPU_MODEL",
    ("zhipu", "vlm_model"): "HXMV_VLM_MODEL",
}


def _spec(provider: str):
    from ..providers import registry
    return registry.get(provider)


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


def _env_names(provider: str, name: str) -> tuple[str, ...]:
    """这个 provider 的这一项，环境变量叫什么（注册表为准 + 历史别名）。"""
    alias = _ENV_ALIASES.get((provider, name))
    return (alias,) if alias else (f"HXMV_{provider.upper()}_{name.upper()}",)


def api_key(provider: str) -> str:
    """取某 provider 的 API Key：环境变量 → 配置文件 → 空串。"""
    spec = _spec(provider)
    envs = (spec.env_keys if spec and spec.env_keys else (f"HXMV_{provider.upper()}_KEY",))
    for env in envs:
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


def option(provider: str, name: str) -> str:
    """读 provider 的非凭据配置（档位等）：环境变量 → 配置文件 → 空串。"""
    for env in _env_names(provider, name):
        if os.environ.get(env):
            return os.environ[env].strip()
    val = (load().get("providers", {}).get(provider, {}) or {}).get(name, "")
    return str(val).strip()


def set_option(provider: str, name: str, value: str) -> None:
    """写 provider 的非凭据配置；空值 = 删掉该项（回到注册表默认档位）。"""
    data = load()
    book = data.setdefault("providers", {}).setdefault(provider, {})
    if value.strip():
        book[name] = value.strip()
    else:
        book.pop(name, None)
    save(data)


def base_url(provider: str) -> str:
    """该 provider 的接口地址：环境变量覆盖 → 注册表默认。"""
    spec = _spec(provider)
    if not spec:
        return ""
    if spec.env_base and os.environ.get(spec.env_base):
        return os.environ[spec.env_base].strip().rstrip("/")
    return (spec.base_url or "").rstrip("/")


def default_provider() -> str:
    """面板里选的「默认用哪家」；没选过返回空串（由调用方按顺序挑）。"""
    return str(load().get("default_provider", "") or "").strip().lower()


def set_default_provider(provider: str) -> None:
    data = load()
    if provider.strip():
        data["default_provider"] = provider.strip().lower()
    else:
        data.pop("default_provider", None)
    save(data)


def mask(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）"


def configured(provider: str) -> bool:
    return bool(api_key(provider))
