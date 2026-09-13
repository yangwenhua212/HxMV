"""轻量 LLM 客户端（OpenAI 兼容协议，零第三方依赖）。

V0.1 原则：闭环骨架可完全离线跑通（Mock 模式），
配了 OPENAI_API_KEY / OPENAI_BASE_URL（可指 DeepSeek 等任意兼容端点）后，
Planner 与 Critic-L3 自动切换为真 LLM。
任何网络失败自动抛回，由调用方降级 Mock。

视觉评审（v0.7 新增）：`chat_vision()` 把**真帧**（base64 JPEG data URL）交给视觉模型——
这就是把 L3「拿文字猜画面」的假闭环改成真闭环的那一步。
**必须显式配 `HXMV_VLM_MODEL`**（如智谱 glm-4v-flash、gpt-4o-mini 等）：主 LLM 多半是纯文本模型，
把图发给它只会被忽略而答得一本正经——那比不检查更坏。没配 = `vision_available()` 为假，
调用方如实标注「未做视觉检查」，不许冒充看过。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request

from .. import USER_AGENT


def llm_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def vision_available() -> bool:
    """视觉评审可用 = 有 Key **且**显式指定了视觉模型（见模块 docstring 的理由）。"""
    return llm_available() and bool(os.environ.get("HXMV_VLM_MODEL"))


def vision_model() -> str | None:
    return os.environ.get("HXMV_VLM_MODEL") or None


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1024,
         model: str | None = None) -> str:
    """调 chat completion，返回文本。失败抛异常（调用方自行降级）。"""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ["OPENAI_API_KEY"]
    model = model or os.environ.get("HXMV_LLM_MODEL", "gpt-4o-mini")
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}",
                 "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode())
    return data["choices"][0]["message"]["content"].strip()


def _data_url(path: str, max_bytes: int = 900_000) -> str | None:
    """本地图 → base64 data URL；读不到或超限返回 None（宁缺勿爆请求体）。"""
    try:
        if os.path.getsize(path) <= 0 or os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def chat_vision(system: str, text: str, image_paths: list[str],
                temperature: float = 0.0, max_tokens: int = 400) -> str:
    """多模态评审：真帧随问题一起交给视觉模型（OpenAI 兼容 content parts）。

    返回模型文本（调用方负责解析 JSON）。没有可用帧 → 抛 ValueError，
    调用方据此退回纯文字路径，**不许**在没有图的情况下假装看了图。
    """
    parts: list[dict] = [{"type": "text", "text": text}]
    for path in image_paths:
        url = _data_url(path)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    if len(parts) == 1:
        raise ValueError("没有可发送的帧（chat_vision）")
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": parts})
    return chat(messages, temperature=temperature, max_tokens=max_tokens,
                model=vision_model())
