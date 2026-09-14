"""对话入口：把「聊天」变成 HxMV 能懂的生产指令。

为什么需要它（老大原话「hxmv 也需要可以聊天啊」）：现在的入口是**一个目标输入框**，
不满意只能干看着。有了对话，用户可以像跟人说话一样提需求、改需求（"太暗了"/"换成夜里"/
"再来一条，5 秒"），HxMV 回一句人话，并把**可执行的目标**（goal）交出来；
他点「开工」才真的跑——不点就不烧钱、不落档。

设计铁律：
- 模型只回 JSON：`{"reply": "...", "goal": "..." | null}`。goal 为 null = 纯聊天/问问题。
- **改需求要基于上下文**：把项目档案（角色/场景键）和最近几次跑的目标喂进去，
  这样"刚才那条改成夜晚"才改得准，而不是凭空新起一个。
- 没有模型 Key 时不许装作能聊：调用方拿到 `llm=False` 要如实显示，并把用户原话
  直接当 goal 交出去（可跑性是底线，跟 Planner 降级 Mock 一个道理）。
"""
from __future__ import annotations

import json

from . import llm

SYSTEM = (
    "你是 HxMV，一个**内容生产 Agent**（规划→执行→观察→判断→修正的闭环，视频是它的第一个场景）。\n"
    "你的主人正在跟你对话，说他想做什么片子、想改哪里。\n"
    "你的任务：① 用一句大白话回他（中文、口语、别客套、别复述他的话）；"
    "② 如果他要开工/改片，给出一个**可执行的生产目标** goal。\n"
    "goal 要求：一句话，写清主体 + 动作 + 环境 + 时长（如「一只小猴子在森林里翻跟头，5 秒，暖色调」）；"
    "如果用户是在改上一条片子，就在那条目标的基础上改，别新起一个不相干的目标。\n"
    "如果用户只是闲聊、问问题、或者需求还不明确（缺主体/动作），goal 给 null，并在 reply 里问清最关键的那一点。\n"
    '只输出 JSON，形如 {"reply": "...", "goal": "..."}（没有目标就给 null）。不要输出 JSON 以外的任何文字。'
)


def _brief(project, recent: list[dict] | None) -> str:
    """把项目档案 + 最近几次跑喂给模型——改需求要站在已有设定上，不能凭空想。"""
    lines: list[str] = []
    if project is not None:
        try:
            chars = "、".join(project.characters) or "无"
            scenes = "、".join(project.scenes) or "无"
            lines.append(f"项目「{project.id}」已有角色键：{chars}；场景键：{scenes}；"
                         f"已登记镜头 {len(project.shots)} 个。")
        except Exception:
            pass
    if recent:
        lines.append("最近几次生产：")
        for r in recent[:3]:
            goal = str(r.get("goal") or "")[:40]
            st = {"done": "已完成", "running": "正在跑", "aborted": "被打断"}.get(
                str(r.get("status")), str(r.get("status") or ""))
            lines.append(f"  - 「{goal}」→ {st}")
    return "\n".join(lines)


def reply(message: str, *, project=None, recent: list[dict] | None = None,
          history: list[dict] | None = None) -> dict:
    """用户一句话 → {"reply": 回话, "goal": 可执行目标 or None, "llm": 是否真用了模型}。

    没有可用模型时**如实降级**：reply 说明情况，goal 直接用用户原话（能跑就先跑）。
    """
    text = (message or "").strip()
    if not text:
        return {"reply": "说点什么吧（比如「一只小猴子在森林里翻跟头，5 秒」）。", "goal": None, "llm": False}
    if not llm.llm_available():
        return {"reply": "这台还没配模型 Key，先按你说的直接开工（去「设置」配一下就能聊了）。",
                "goal": text, "llm": False}

    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    brief = _brief(project, recent)
    if brief:
        messages.append({"role": "system", "content": "当前状况：\n" + brief})
    for h in (history or [])[-6:]:            # 只带最近几轮，够改需求就行
        role = "assistant" if h.get("role") == "assistant" else "user"
        content = str(h.get("content") or "")[:500]
        if content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": text})

    raw = llm.chat(messages, temperature=0.4, max_tokens=500)
    data = _parse(raw)
    out = {"reply": str(data.get("reply") or "").strip(), "goal": None, "llm": True}
    goal = data.get("goal")
    if isinstance(goal, str) and goal.strip() and goal.strip().lower() != "null":
        out["goal"] = goal.strip()
    if not out["reply"]:
        out["reply"] = "（模型没回上话，再说一次？）"
    return out


def _parse(raw: str) -> dict:
    """模型输出解析：优先整段 JSON，其次抠出第一个 {...}。失败就当纯文本回复。"""
    s = (raw or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s.split("\n", 1)[1] if "\n" in s else s
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else {"reply": s}
    except ValueError:
        pass
    i, j = s.find("{"), s.rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(s[i:j + 1])
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    return {"reply": s or "（没听清，再说一次？）"}
