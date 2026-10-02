"""轻量 LLM 客户端（OpenAI 兼容协议，零第三方依赖）——**多家通用**。

端点解析顺序（`endpoint()`）：
  1. 显式 `OPENAI_API_KEY` / `OPENAI_BASE_URL` + `HXMV_LLM_MODEL`：可指任意兼容端点；
  2. 否则用**当前这家的 Key**（`active_provider()`：本轮 `HXMV_PROVIDER` → 面板选的默认家
     → 第一个配了 Key 的家）—— **一个 Key 同时管画面/视频/规划**，这是 v0.6 定的规矩；
  3. 都没有 = 空 Key，Planner 走 Mock，闭环骨架仍可完全离线跑通。
任何网络失败自动抛回，由调用方降级 Mock。

视觉评审（v0.7）：`chat_vision()` 把**真帧**（base64 JPEG data URL）交给视觉模型——
这就是把 L3「拿文字猜画面」的假闭环改成真闭环的那一步。
**必须知道模型会看图**：每家注册表里声明了自己的默认视觉档（智谱 glm-4v-flash、
Agnes agnes-2.5-flash，两者都实测能看图）；换成别的兼容端点则必须显式配 `HXMV_VLM_MODEL`。
主 LLM 多半是纯文本模型，把图发给它只会被忽略而答得一本正经——那比不检查更坏。
拿不到可用视觉模型 = `vision_available()` 为假，调用方如实标注「未做视觉检查」，不许冒充看过。
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from .. import USER_AGENT

OPENAI_BASE = "https://api.openai.com/v1"


def active_provider() -> str:
    """这一轮谁当大脑：显式 HXMV_PROVIDER（能出图的真 API 那家）→ 面板选的默认 → 第一个配了 Key 的。

    为什么认「配了 Key 且有 base」：`--provider local` 跑本地渲染时大脑仍该用已配好的
    那家 LLM（local 没有 base/Key，自动跳过），否则一换 provider 规划就退回 Mock。
    """
    from ..providers import registry
    from . import config as cfg
    for name in (os.environ.get("HXMV_PROVIDER", "").strip().lower(), cfg.default_provider()):
        spec = registry.get(name)
        if spec and spec.base_url and cfg.api_key(spec.id):
            return spec.id
    for spec in registry.api_specs():
        if spec.base_url and cfg.api_key(spec.id):
            return spec.id
    return ""


def endpoint() -> tuple[str, str, str]:
    """返回 (base_url, api_key, provider_id)。provider_id 为空 = 没有任何可用 Key。"""
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return os.environ.get("OPENAI_BASE_URL", OPENAI_BASE).rstrip("/"), key, "openai"
    from . import config as cfg          # 延迟导入：避免与 config 形成环
    pid = active_provider()
    if pid:
        return cfg.base_url(pid), cfg.api_key(pid), pid
    return "", "", ""


def llm_available() -> bool:
    return bool(endpoint()[1])


def _tier(kind: str, env: str) -> str | None:
    """档位解析：显式环境变量 → 这家在配置里选的档 → 注册表默认。

    `kind` = 注册表里的档位类别（"text" / "vision"）。
    """
    explicit = os.environ.get(env)
    if explicit:
        return explicit
    _, key, pid = endpoint()
    if not key or not pid:
        return None
    from ..providers import registry
    from . import config as cfg
    spec = registry.get(pid)
    if not spec:                                  # 自定义兼容端点：不认识图就别说会看
        return None
    return cfg.option(pid, f"{kind}_model") or spec.default_model(kind) or None


def text_model() -> str | None:
    return _tier("text", "HXMV_LLM_MODEL")


def vision_model() -> str | None:
    """视觉档：显式 `HXMV_VLM_MODEL` 优先；否则这家注册表里的视觉档（实测会看图的那类）。

    其它 OpenAI 兼容端点**不给默认值**——不知道对面模型会不会看图，就不许假装看了。
    """
    return _tier("vision", "HXMV_VLM_MODEL")


def vision_available() -> bool:
    """视觉评审可用 = 有 Key **且**拿得到确定会看图的模型（见模块 docstring 的理由）。"""
    return llm_available() and bool(vision_model())


def _call_once(base: str, key: str, model: str, messages: list[dict],
               temperature: float, max_tokens: int) -> tuple[str, str]:
    """一次请求 → (正文, finish_reason)。"""
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode())
    choice = (data.get("choices") or [{}])[0]
    return str((choice.get("message") or {}).get("content") or "").strip(), \
        str(choice.get("finish_reason") or "")


 # 免费档限流（429）与偶发 5xx 是**上游的事**，不是我们写错了：等得起就等（项目约定「不怕等」）。
# 默认最多等 120 秒，可用 HXMV_LLM_WAIT 覆盖；等的时候打印「已等/上限」，别看着像卡死。
_CONGESTION_CODES = (429, 500, 502, 503, 504)


def _call_patient(base: str, key: str, model: str, messages: list[dict],
                  temperature: float, max_tokens: int) -> tuple[str, str]:
    wait = float(os.environ.get("HXMV_LLM_WAIT", "120") or 0)
    deadline = time.time() + wait
    delay = 5.0
    while True:
        try:
            return _call_once(base, key, model, messages, temperature, max_tokens)
        except urllib.error.HTTPError as e:
            if e.code not in _CONGESTION_CODES or time.time() >= deadline:
                raise
            left = max(0.0, deadline - time.time())
            print(f"⏳ 模型端限流/不可用（HTTP {e.code}）：{delay:.0f}s 后再试"
                  f"（上限 {wait:.0f}s，剩 {left:.0f}s）")
            time.sleep(min(delay, left + 1))
            delay = min(delay * 2, 60.0)


def chat(messages: list[dict], temperature: float = 0.3, max_tokens: int = 1024,
         model: str | None = None) -> str:
    """调 chat completion，返回文本。失败抛异常（调用方自行降级）。"""
    base, key, _ = endpoint()
    if not key:
        raise RuntimeError("没有可用的 LLM Key（既没 OPENAI_API_KEY，也没配任何一家的 Key）")
    model = model or os.environ.get("HXMV_LLM_MODEL") or text_model() or "gpt-4o-mini"
    text, finish = _call_patient(base, key, model, messages, temperature, max_tokens)
    if not text and finish == "length":
        # 思维链型模型（实测 agnes-2.5-flash）会把预算全花在推理上、正文返回空字符串。
        # 放大预算再问一次（不做无界重试：仍空就交给调用方降级，别装作拿到了答案）。
        text, _ = _call_patient(base, key, model, messages, temperature, max(max_tokens * 4, 2048))
    return text


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
    # 有些模型（如 agnes-2.5-flash）会先吐思维链，max_tokens 给小了正文就空 → 放宽一点
    return chat(messages, temperature=temperature, max_tokens=max(max_tokens, 600),
                model=vision_model())
