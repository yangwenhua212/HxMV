"""轻量 LLM 客户端（OpenAI 兼容协议，零第三方依赖）。

端点解析顺序（`endpoint()`）：
  1. 显式 `OPENAI_API_KEY` / `OPENAI_BASE_URL` + `HXMV_LLM_MODEL` —— 可指 DeepSeek 等任意兼容端点；
  2. 否则用平台里存的智谱 Key（`--set-key zhipu`）—— **一个 Key 同时管视频生成、视觉评审、规划**；
  3. 都没有 = 空 Key，Planner 走 Mock，闭环骨架仍可完全离线跑通。
任何网络失败自动抛回，由调用方降级 Mock。

视觉评审（v0.7 新增）：`chat_vision()` 把**真帧**（base64 JPEG data URL）交给视觉模型——
这就是把 L3「拿文字猜画面」的假闭环改成真闭环的那一步。
**必须知道模型会看图**：走智谱 Key 时默认 `glm-4v-flash`；用其它 OpenAI 兼容端点则
必须显式配 `HXMV_VLM_MODEL`。主 LLM 多半是纯文本模型，把图发给它只会被忽略而答得一本正经——
那比不检查更坏。拿不到可用视觉模型 = `vision_available()` 为假，
调用方如实标注「未做视觉检查」，不许冒充看过。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.request

from .. import USER_AGENT


ZHIPU_BASE = "https://open.bigmodel.cn/api/paas/v4"
OPENAI_BASE = "https://api.openai.com/v1"
ZHIPU_TEXT_MODEL = "glm-4-flash"      # 免费档
ZHIPU_VISION_MODEL = "glm-4v-flash"   # 免费档


def endpoint() -> tuple[str, str, bool]:
    """返回 (base_url, api_key, 是否走平台里存的智谱 Key)。

    显式环境变量优先；否则回落到 `~/.hxmv/config.json` 里的智谱 Key——
    用户只需要 `--set-key zhipu <key>` 一次，视频/视觉/规划就全通了。
    """
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return os.environ.get("OPENAI_BASE_URL", OPENAI_BASE).rstrip("/"), key, False
    try:
        from . import config as cfg          # 延迟导入：避免与 config 形成环
        zhipu = cfg.api_key("zhipu")
    except Exception:
        zhipu = None
    if zhipu:
        return ZHIPU_BASE, zhipu, True
    return os.environ.get("OPENAI_BASE_URL", OPENAI_BASE).rstrip("/"), "", False


def llm_available() -> bool:
    return bool(endpoint()[1])


def vision_model() -> str | None:
    """视觉模型：显式 `HXMV_VLM_MODEL` 优先；走智谱 Key 时默认 glm-4v-flash（免费档）。

    其它 OpenAI 兼容端点**不给默认值**——不知道对面模型会不会看图，就不许假装看了。
    """
    explicit = os.environ.get("HXMV_VLM_MODEL")
    if explicit:
        return explicit
    _, key, is_zhipu = endpoint()
    if not (key and is_zhipu):
        return None
    try:
        from . import config as cfg
        return cfg.option("zhipu", "vlm_model") or ZHIPU_VISION_MODEL   # 面板设置页选的档位
    except Exception:
        return ZHIPU_VISION_MODEL


def vision_available() -> bool:
    """视觉评审可用 = 有 Key **且**拿得到确定会看图的模型（见模块 docstring 的理由）。"""
    return llm_available() and bool(vision_model())


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1024,
         model: str | None = None) -> str:
    """调 chat completion，返回文本。失败抛异常（调用方自行降级）。"""
    base, key, is_zhipu = endpoint()
    if not key:
        raise RuntimeError("没有可用的 LLM Key（既没 OPENAI_API_KEY，也没配智谱 Key）")
    model = model or os.environ.get("HXMV_LLM_MODEL") or (ZHIPU_TEXT_MODEL if is_zhipu else "gpt-4o-mini")
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
