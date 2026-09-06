"""轻量 LLM 客户端（OpenAI 兼容协议，零第三方依赖）。

V0.1 原则：闭环骨架可完全离线跑通（Mock 模式），
配了 OPENAI_API_KEY / OPENAI_BASE_URL（可指 DeepSeek 等任意兼容端点）后，
Planner 与 Critic-L3 自动切换为真 LLM。
任何网络失败自动抛回，由调用方降级 Mock。
"""
from __future__ import annotations

import json
import os
import urllib.request


def llm_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1024) -> str:
    """调 chat completion，返回文本。失败抛异常（调用方自行降级）。"""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("HXMV_LLM_MODEL", "gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()
