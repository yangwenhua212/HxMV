"""项目档案：让 HxMV 记住"我们在做哪部片子"——风格、角色、场景、已生成的画面。

为什么需要它（老大 2026-09 提的需求原话）：
    "记得一系列的生成，比如做一个动画短剧风格以及角色，只要后续还是要做这个动画
     就可以接着继续做，不希望生成新画面。"

三件事：
1. **风格与角色落档**：`style` + `characters{}` + `scenes{}` 持久化到
   `~/.hxmv/projects/<id>/project.json`，下一集/下一次开跑先读档案，
   角色参考图**直接复用**（不重新生成 → 角色长相、画风天然一致）。
2. **指纹复用**：镜头参数（prompt/时长/分辨率/帧率/参考强度/种子/角色/场景/风格）
   算一个指纹；命中档案里已有的指纹 → **直接引用那个文件，跳过生成**。
   重复跑同一集 = 0 次生成。
3. **分集延续**：每次跑完记 `episodes[{n, goal, outputs, date}]`，
   新一集只增量生成新内容，已有画面全部复用。

边界（别对外吹）：档案保证的是"每次都用同一张角色参考图 + 同一套风格参数"，
生成端最终能不能守住一致性取决于生成服务本身（参考图/首尾帧能力）。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

PROJECTS_DIR = os.environ.get("HXMV_PROJECTS", os.path.expanduser("~/.hxmv/projects"))

# 参与指纹的参数（决定"这一帧画面长什么样"的全部输入）
_FP_KEYS = ("prompt", "duration", "resolution", "fps", "seed", "reference_strength",
            "motion_scale", "audio_gain_db", "trim_black", "character", "scene", "style")


def fingerprint(params: dict) -> str:
    """画面指纹：同一套参数 → 同一指纹 → 复用已有文件，不重新生成。"""
    payload = json.dumps({k: params.get(k) for k in _FP_KEYS},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.md5(payload.encode()).hexdigest()[:12]


class Project:
    def __init__(self, pid: str, data: dict | None = None):
        self.id = pid
        d = data or {}
        self.title: str = d.get("title") or pid
        self.style: str = d.get("style") or "cinematic"
        self.characters: dict = d.get("characters", {})   # key → {name,path,params,runs}
        self.scenes: dict = d.get("scenes", {})
        self.shots: dict = d.get("shots", {})             # 指纹 → {path,params,episodes[]}
        self.episodes: list = d.get("episodes", [])
        self.created: float = d.get("created") or time.time()
        self.updated: float = d.get("updated") or self.created
        self.reused = 0        # 本次运行统计（不落盘）
        self.regenerated = 0

    # ---------- 持久化 ----------
    @property
    def dir(self) -> str:
        return os.path.join(PROJECTS_DIR, self.id)

    @property
    def path(self) -> str:
        return os.path.join(self.dir, "project.json")

    @classmethod
    def load(cls, pid: str) -> "Project":
        try:
            with open(os.path.join(PROJECTS_DIR, pid, "project.json"), encoding="utf-8") as f:
                return cls(pid, json.load(f))
        except (OSError, json.JSONDecodeError):
            return cls(pid)

    @classmethod
    def list_all(cls) -> list[dict]:
        out = []
        if not os.path.isdir(PROJECTS_DIR):
            return out
        for name in sorted(os.listdir(PROJECTS_DIR)):
            p = cls.load(name)
            out.append({"id": p.id, "title": p.title, "style": p.style,
                        "characters": len(p.characters), "shots": len(p.shots),
                        "episodes": len(p.episodes), "updated": p.updated})
        return out

    def save(self) -> None:
        os.makedirs(self.dir, exist_ok=True)
        self.updated = time.time()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"id": self.id, "title": self.title, "style": self.style,
                       "characters": self.characters, "scenes": self.scenes,
                       "shots": self.shots, "episodes": self.episodes,
                       "created": self.created, "updated": self.updated},
                      f, ensure_ascii=False, indent=1)

    # ---------- 资产（角色/场景）：先查档，命中就不重新生成 ----------
    def asset(self, kind: str, key: str) -> dict | None:
        book = self.characters if kind == "character" else self.scenes
        hit = book.get(key)
        if hit and os.path.isfile(hit.get("path", "")):
            return hit
        return None

    def prune(self) -> None:
        """清掉指向已不存在文件的档案项（用户删了产物目录也不至于一直命中空气）。"""
        for book in (self.characters, self.scenes):
            for k in [k for k, v in book.items() if not os.path.isfile(v.get("path", ""))]:
                book.pop(k, None)
        for fp in [k for k, v in self.shots.items() if not os.path.isfile(v.get("path", ""))]:
            self.shots.pop(fp, None)

    def register_asset(self, kind: str, key: str, path: str, **meta) -> None:
        book = self.characters if kind == "character" else self.scenes
        other = self.scenes if kind == "character" else self.characters
        other.pop(key, None)      # 同名 key 只归一类（早期版本把场景登记成角色的坑）
        old = book.get(key, {})
        book[key] = {"path": path, "runs": int(old.get("runs", 0)) + 1,
                     "updated": time.time(), **meta}
        self.save()

    # ---------- 镜头：指纹命中 = 复用文件，跳过生成 ----------
    def shot(self, fp: str) -> dict | None:
        hit = self.shots.get(fp)
        if hit and os.path.isfile(hit.get("path", "")):
            return hit
        return None

    def register_shot(self, fp: str, path: str, episode: int | None = None, **meta) -> None:
        entry = self.shots.setdefault(fp, {"episodes": []})
        entry.update({"path": path, "updated": time.time(), **meta})
        if episode is not None and episode not in entry["episodes"]:
            entry["episodes"].append(episode)
        self.save()

    def best_for(self, prompt: str, character: str | None = None,
                 scene: str | None = None, style: str | None = None) -> dict | None:
        """这一集这个镜头以前做过吗？按 **剧情身份**（prompt+角色+场景+风格）找已存档画面。

        为什么要它：精确指纹依赖"参数完全一致"，而大脑的泛化经验会让起手参数慢慢变化
        （实测：0.8 → 0.7 → 0.9），指纹就永远对不上、每次都在重画。
        剧情身份才是"这是不是同一集同一个镜头"的正确判据——命中就沿用上次那版的参数，
        指纹随即命中，**一张新画面都不生成**。具体档案优先于泛化经验。
        """
        if not (character or scene):
            return None
        for entry in sorted(self.shots.values(), key=lambda e: e.get("updated", 0), reverse=True):
            ti = entry.get("task_input") or {}
            tc = entry.get("task_constraints") or {}
            if ti.get("prompt") != prompt:
                continue
            if character and tc.get("character") != character:
                continue
            if scene and tc.get("scene") != scene:
                continue
            if style and tc.get("style") != style:
                continue
            return entry
        return None

    # ---------- 分集 ----------
    def add_episode(self, goal: str, outputs: list[str], episode: int | None = None) -> None:
        n = episode if episode is not None else len(self.episodes) + 1
        self.episodes = [e for e in self.episodes if e.get("n") != n]
        self.episodes.append({"n": n, "goal": goal, "date": time.time(),
                              "outputs": [o for o in outputs if o]})
        self.episodes.sort(key=lambda e: e.get("n") or 0)
        self.save()

    def last_episode(self) -> dict | None:
        return self.episodes[-1] if self.episodes else None

    # ---------- 规划上下文 / 摘要 ----------
    def inject(self) -> str:
        """给 Planner 的项目上下文：这是\"接着做\"的关键——风格和角色必须是档案里的。"""
        if not (self.characters or self.scenes or self.episodes):
            return ""
        lines = [f"【项目：{self.title}】风格={self.style}"]
        for key, c in self.characters.items():
            lines.append(f"- 角色「{c.get('name') or key}」(key={key})：沿用已有参考图，"
                         f"不要新建角色、不要改设定")
        for key, s in self.scenes.items():
            lines.append(f"- 场景「{s.get('name') or key}」(key={key})：沿用已有参考图")
        if self.episodes:
            done = "、".join(f"第{e['n']}集：{str(e.get('goal'))[:24]}" for e in self.episodes[-3:])
            lines.append(f"- 已完成：{done}")
        lines.append("续做要求：角色/场景/风格一律复用档案中的设定，只产出本集的新镜头。")
        return "\n".join(lines)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "style": self.style,
                "characters": list(self.characters), "scenes": list(self.scenes),
                "shots": len(self.shots), "episodes": [e.get("n") for e in self.episodes]}

    def describe(self) -> str:
        s = self.summary()
        return (f"项目「{s['title']}」 风格={s['style']} | 角色 {len(s['characters'])} "
                f"({','.join(s['characters']) or '—'}) | 场景 {len(s['scenes'])} | "
                f"已存档画面 {s['shots']} | 分集 {s['episodes'] or '—'}")
