"""编剧：HxMV 自己写剧情 —— 目标 → 结构化分镜（场次 / 镜头 / 运镜 / 时长 / 演员）。

现状与缺口（这是本模块存在的理由）：`STORYBOARD` 动作在 v0.8 之前是**空壳**
（executor 直接返回「镜头 1: <目标前12字>…」这种假分镜，还没有人消费它）。于是 HxMV
只会两种活：用户贴剧本 → `script.parse` 照做；不贴 → LLM 一次性给几句镜头 prompt，
**没有故事、没有场次、镜头之间没有叙事关系**。老大 2026-09 的要求原话是：
「相当于 hxmv 会自己写剧情」——所以分镜必须由它自己产出，而且是**结构化**的。

边界（勿破）：
- 编剧 = 「想做什么」，属于 LLM 的活；**怎么派活是代码的事**（planner 把分镜翻成任务）。
- 产出结构与 `script.parse()` 同形（`{title, shots[], narration, sfx, total_duration}`），
  这样「用户剧本」和「自己写的剧本」共用同一条生产路径 —— 少一条岔路就少一处不一致。
- **可跑性是底线**：没配 Key / 模型不听话 / JSON 解析失败 → 走确定性兜底分镜（3 镜头模板），
  绝不返回空、绝不硬失败。但兜底分镜会如实标 `written_by: "fallback"`，不许冒充模型写的。
"""
from __future__ import annotations

import json
import re

from . import camera, llm

# 自己写的分镜最多几个镜头。**别贪多**：默认预算 max_total_attempts=30，而每个镜头最多 5 次
# 尝试——5 个镜头一旦都要返工就会把预算吃光、连成片都拼不出来（实测踩过：5 镜吃满 30 次尝试）。
# 免费档单镜也就 5 秒，要长片靠"接着做"（同一部片子再来一集），不是一镜到底铺满。
MAX_SHOTS = 4
MIN_SHOTS = 2
SHOT_SECONDS = 5.0

SYSTEM = (
    "你是 HxMV 的编剧兼分镜师。用户给一个创作目标，你要**自己写出剧情**，"
    "并把它拆成可直接拍摄的分镜表。\n"
    "只输出合法 JSON，形如：\n"
    '{"title": "片名", "logline": "一句话剧情",\n'
    ' "scenes": [{"key": "scene_1", "name": "海边崖顶", "time": "黄昏"}],\n'
    ' "shots": [{"n": 1, "scene": "scene_1", "camera": "push_in", "speed": "slow",\n'
    '            "duration": 5, "cast": ["主角"],\n'
    '            "prompt": "中文画面描述：主体 + 动作 + 环境 + 景别"}],\n'
    ' "narration": "旁白（没有就留空）", "sfx": "环境音（没有就留空）"}\n'
    "硬要求（违反就作废）：\n"
    f"① {MIN_SHOTS}~{MAX_SHOTS} 个镜头，**按剧情顺序**推进（起 → 承 → 转/合），不是同一画面的重复；\n"
    "② 同一场戏的镜头 `scene` 键必须相同；换地点就换键（并在 scenes 里声明该键）；\n"
    f"③ `camera` 只能取：{'/'.join(camera.MOVES)}；`speed` 只能取 slow/normal/fast；\n"
    "④ `cast` 列这镜头里出场的角色键；**空镜（风景/特写物件）写 []**，别硬塞主角；\n"
    "⑤ `prompt` 只写画面内容（主体/动作/环境/景别），**不要写运镜**（运镜由 camera 字段表达）；\n"
    f"⑥ 每个镜头 duration 取 3~10 秒（免费档单镜上限 {SHOT_SECONDS:.0f} 秒）。\n"
    "不要输出任何 JSON 以外的文字。"
)


def _parse_json(text: str) -> dict | None:
    """宽容解析：容忍 ```json 围栏与前后废话；解析不出来返回 None（调用方走兜底）。"""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean_cast(raw, archive_chars: list[str]) -> list[str]:
    """演员键：模型爱写中文名/别名 → 收敛到项目档案里已有的键（沿用才有一致性抓手）。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[、,，/|]", raw) if x.strip()]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        if archive_chars:
            hit = next((c for c in archive_chars if c == name or c in name or name in c), None)
            name = hit or archive_chars[0]        # 认不出就落到档案里的第一个角色
        if name not in out:
            out.append(name)
    return out


def normalize(data: dict, goal: str, project=None) -> dict | None:
    """把模型给的分镜收拾成**可执行**结构（与 script.parse 同形）；不合格返回 None。

    校验为什么不省（与 LLMPlanner 同一个教训）：模型会给"看着像分镜、其实拍不出来"的东西——
    镜数不足 / 全部同一场戏却写了不同键 / camera 自造词 / cast 塞了不存在的人。
    每一处都落回**确定值**，而不是把脏数据带到生成端。
    """
    if not isinstance(data, dict):
        return None
    chars = list(project.characters) if project else []
    scenes = list(project.scenes) if project else []
    raw_shots = data.get("shots")
    if not isinstance(raw_shots, list):
        return None

    declared: dict[str, str] = {}     # 模型声明的场次键 → 落定后的键
    shots: list[dict] = []
    for i, s in enumerate(raw_shots, 1):
        if not isinstance(s, dict):
            continue
        prompt = str(s.get("prompt") or "").strip()
        if not prompt:
            continue
        want = str(s.get("scene") or "").strip()
        # 档案里已有场景键时优先沿用（否则每个 run 都在新建场景，参考图永远用不上）
        key = want
        if scenes and want not in scenes:
            key = next((x for x in scenes if x == want or x in want or want in x), scenes[0])
        key = key or (scenes[0] if scenes else "scene_1")
        declared[key] = str(s.get("name") or key)
        parsed = camera.parse(prompt)                     # 模型把运镜写进 prompt 也能救回来
        move = camera.normalize_move(s.get("camera")) or (parsed or {}).get("move") or camera.MOVE_STATIC
        speed = str(s.get("speed") or (parsed or {}).get("speed") or camera.DEFAULT_SPEED).lower()
        if speed not in camera.SPEEDS:
            speed = camera.DEFAULT_SPEED
        try:
            duration = max(3.0, min(10.0, float(s.get("duration") or SHOT_SECONDS)))
        except (TypeError, ValueError):
            duration = SHOT_SECONDS
        shots.append({"prompt": camera.strip(prompt) or prompt, "duration": round(duration, 1),
                      "camera": move, "speed": speed,
                      "cast": _clean_cast(s.get("cast"), chars),
                      "scene": key, "n": len(shots) + 1})
    if len(shots) < MIN_SHOTS:
        return None
    shots = shots[:MAX_SHOTS]
    # 场次表：只留真的被镜头用到的键（声明的孤儿场景不派活，省一次出图）
    used = {s["scene"] for s in shots}
    scene_list = [{"key": k, "name": declared.get(k, k)} for k in sorted(used)]
    total = round(sum(s["duration"] for s in shots), 1)
    return {"title": str(data.get("title") or "").strip() or goal[:16],
            "logline": str(data.get("logline") or "").strip()[:120],
            "scenes": scene_list,
            "shots": shots,
            "narration": str(data.get("narration") or "").strip(),
            "sfx": str(data.get("sfx") or "").strip(),
            "total_duration": total,
            "written_by": "llm"}


def fallback(goal: str, project=None) -> dict:
    """没 Key / 模型不听话时的**确定性兜底分镜**：起（远景）→ 承（中景主体）→ 合（特写）。

    它也是基准集与本地演示用的分镜（不花钱、每次一样，分数才可比）。如实标 written_by=fallback。
    """
    chars = list(project.characters) if project else []
    scenes = list(project.scenes) if project else []
    scene = scenes[0] if scenes else "scene_1"
    subject = (chars[0] if chars else "主角")
    base = re.sub(r"[，。,.；;].*$", "", str(goal or "")).strip() or "主角"
    shots = [
        {"n": 1, "prompt": f"全景：{base} 所处环境的整体样貌，交待地点与时间",
         "duration": SHOT_SECONDS, "camera": camera.MOVE_PUSH_IN, "speed": "slow",
         "cast": [], "scene": scene},
        {"n": 2, "prompt": f"中景：{subject} 出现在画面中央，正做与「{base}」相符的动作",
         "duration": SHOT_SECONDS, "camera": camera.MOVE_TRACK_RIGHT, "speed": "normal",
         "cast": [subject], "scene": scene},
        {"n": 3, "prompt": f"特写：{subject} 的面部与情绪，背景虚化",
         "duration": SHOT_SECONDS, "camera": camera.MOVE_STATIC, "speed": "normal",
         "cast": [subject], "scene": scene},
    ]
    return {"title": base[:16], "logline": f"{base}", "scenes": [{"key": scene, "name": scene}],
            "shots": shots, "narration": "", "sfx": "",
            "total_duration": round(sum(s["duration"] for s in shots), 1),
            "written_by": "fallback"}


def write(goal: str, project=None, brain=None) -> dict:
    """写一份分镜：真 LLM 优先，失败落兜底（永远返回可用结构）。

    返回结构与 `script.parse()` 同形 + `written_by`（llm/fallback）与 `logline`。
    """
    if llm.llm_available():
        blocks = []
        try:
            if project is not None:
                blocks.append(project.inject())
            if brain is not None:
                blocks.append(brain.inject(query=goal))
        except Exception:
            pass
        system = SYSTEM + ((" \n" + "\n\n".join(b for b in blocks if b)) if blocks else "")
        try:
            text = llm.chat([{"role": "system", "content": system},
                             {"role": "user", "content": str(goal)}], temperature=0.6)
            plan = normalize(_parse_json(text), goal, project)
            if plan is not None:
                return plan
        except Exception:
            pass      # 模型挂了不能中断生产：落兜底分镜（可跑性是底线）
    return fallback(goal, project)


def from_task(task, project=None) -> dict:
    """分镜任务 → 分镜（三种来源，一次判清）：

    - `input.plan` = 用户贴的剧本 → **直接用，不调模型**（剧本是规格，别再花钱让模型重写）；
    - `input.offline` = mock/基准集 → 确定性兜底分镜（每次一样，分数才可比）；
    - 其它 = 真编剧（LLM，失败落兜底）。
    """
    plan = task.input.get("plan")
    if isinstance(plan, dict) and plan.get("shots"):
        return {**plan, "written_by": plan.get("written_by") or "user"}
    goal = str(task.input.get("goal") or task.input.get("prompt") or "")
    if task.input.get("offline"):
        return fallback(goal, project)
    return write(goal, project)
