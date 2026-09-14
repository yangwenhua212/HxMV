"""分镜剧本解析：把用户写的剧本**照做**成镜头列表。

为什么要有它（实测）：把整段剧本当 goal 丢给 LLM 规划器，它会按自己的理解重新编排 ——
用户写的「全景大海 → 穿过云层 → 山顶仙石」被换成它自己想的样子，镜数也不对。
剧本是**规格**，不是灵感：能解析就按剧本走，解析不出来才交给 LLM 自由规划。

支持的写法（中文剧本常见形态，宽容匹配）：
    二、分镜剧本
    【0:00-0:20】开场：天地之间
    画面：全景。浩瀚的大海尽头……
    音效：远处海浪声……
    旁白：“很久很久以前……”
    镜头：山顶特写。一块巨大的仙石……

规则：
- 时间块（`【…】` / `#` / `第N场`）切开；
- 块内的 `画面：` 与 `镜头：` 各算**一个镜头**（一条块里两个视觉节拍就是两个镜头）；
- `旁白：` / `音效：` 属于整段（按出现顺序拼起来）；
- 块时长按【时间】跨度算，平均分给块内镜头；没有时间就按 5 秒/镜头。

返回 None = 不是剧本（交给 LLM 规划器）。
"""
from __future__ import annotations

import re

# 【0:00-0:20】 / 【00:00～00:20】 / 「0:00-0:20」
_TIME_BLOCK = re.compile(r"[【\[]\s*(\d{1,2}:\d{2})\s*[-~～—－]\s*(\d{1,2}:\d{2})\s*[】\]](.*)")
# 镜头一 / 镜头1 / 1. / ① 
_SHOT_HEAD = re.compile(r"^\s*(?:镜头\s*[一二三四五六七八九十\d]+|[①②③④⑤⑥⑦⑧⑨⑩]|\d+[.、])\s*[:：]?\s*(.*)$")
_FIELD = re.compile(r"^\s*(画面|镜头|旁白|音效|配乐|解说|台词|字幕)\s*[:：]\s*(.*)$")
# 章节标题（"二、分镜剧本" / "# 分镜" …）：只影响标题，不当镜头
_CHAPTER = re.compile(r"^\s*(?:[一二三四五六七八九十]+[、.]|#{1,3}\s*|\*)")
_QUOTES = "“”\"'「」『』 \t"


def _clean(s: str) -> str:
    return s.strip().strip(_QUOTES).strip()


def _mmss(s: str) -> float:
    m, sec = s.split(":")
    return int(m) * 60 + int(sec)


def parse(text: str) -> dict | None:
    """把剧本文本解析成 {title, shots[], narration, sfx, total_duration}；不是剧本返回 None。"""
    if not text or len(text) < 40:
        return None
    lines = [ln.rstrip() for ln in str(text).replace("\r", "").split("\n")]

    blocks: list[dict] = []          # [{title, span, shots:[str], }]
    cur: dict | None = None
    narration: list[str] = []
    sfx: list[str] = []
    pending: str | None = None        # 上一条是「旁白：」但内容在下一行（中文剧本常见写法）

    for ln in lines:
        if not ln.strip():
            continue
        m = _TIME_BLOCK.search(ln)
        if m:
            cur = {"title": _clean(m.group(3)) or "开场",
                   "span": _mmss(m.group(2)) - _mmss(m.group(1)),
                   "shots": []}
            blocks.append(cur)
            continue
        fm = _FIELD.match(ln)
        if fm:
            kind, body = fm.group(1), _clean(fm.group(2))
            if not body:
                # 字段名给了、内容在下一行/下一段：挂起，交给后面的行补上
                pending = kind
                continue
            pending = None
            if kind in ("旁白", "解说", "台词"):
                narration.append(body)
            elif kind in ("音效", "配乐"):
                sfx.append(body)
            else:                     # 画面 / 镜头 → 一个镜头
                if cur is None:
                    cur = {"title": "开场", "span": 0.0, "shots": []}
                    blocks.append(cur)
                cur["shots"].append(body)
            continue
        if pending:
            # 补上挂起的「旁白/音效/画面」内容
            body = _clean(ln)
            if pending in ("旁白", "解说", "台词"):
                narration.append(body)
            elif pending in ("音效", "配乐"):
                sfx.append(body)
            elif cur is not None:
                cur["shots"].append(body)
            pending = None
            continue
        # 没有字段名的行：当成上一块的继续（多行画面描述）
        if cur is not None and cur["shots"] and not _CHAPTER.match(ln):
            cur["shots"][-1] = (cur["shots"][-1] + " " + _clean(ln)).strip()

    shots: list[dict] = []
    for blk in blocks:
        texts = blk["shots"] or ([blk["title"]] if blk["title"] else [])
        if not texts:
            continue
        per = (blk["span"] / len(texts)) if blk["span"] else 5.0
        for t in texts:
            shots.append({"prompt": f"{blk['title']}。{t}".strip("。 "),
                          "duration": max(2.0, round(per, 1))})

    if len(shots) < 2:                # 拆不出多镜头 → 不当剧本处理，交给 LLM
        return None
    total = sum(s["duration"] for s in shots)
    return {"title": blocks[0]["title"] if blocks else "",
            "shots": shots,
            "narration": " ".join(narration),
            "sfx": "，".join(sfx),
            "total_duration": round(total, 1)}


# 判定"这个镜头里有没有主角"的关键词：只认**活物**，不认"仙山/主人/王子"这类词
# （实测：把"仙"当关键词 → 「一座仙山」被判成有主角 → 空镜被反复判角色不符，白跑 30+ 次）
_CAST_WORDS = ("石猴", "猴子", "猴", "人物", "主角", "女孩", "男孩", "女子", "男子", "老人",
               "少年", "少女", "儿童", "孩子", "妖怪", "妖精", "和尚", "僧人", "书生",
               "武士", "士兵", "皇帝", "国王", "妇人", "老妇", "道士", "智者")


def guess_cast(prompt: str, character_keys=()) -> bool:
    """粗判镜头里有没有主角（剧本解析用）。

    优先级：① 项目里登记的角色键名出现 → 有；② 活物词出现 → 有；③ 否则 → 无。
    默认偏"无"：判错成"有"的代价是空镜被反复判不符（实测白跑 30+ 次），
    判错成"无"只是少查一次角色一致性 —— 前者是死循环，后者只是检查少一层。
    """
    text = str(prompt or "")
    for k in character_keys or ():
        if k and str(k) in text:
            return True
    return any(w in text for w in _CAST_WORDS)
