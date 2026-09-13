"""Planner：把目标拆成结构化任务序列。LLM 负责"想做什么"的唯一入口。

V0.1 提供两个实现：
- MockPlanner：内置"小猫视频"模板（确定性，保证闭环可演示）；
- LLMPlanner：接 OpenAI 兼容端点，任意需求 → 结构化 JSON 任务数组；
  失败/未配置时由工厂自动降级 MockPlanner（系统仍然可跑，只是"想象力"受限）。
"""
from __future__ import annotations

import json
import re

from . import llm
from .state import (
    ACTION_COMPOSE, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
    ACTION_GENERATE_SHOT, ACTION_STORYBOARD, ExecutionState, Task,
)


class Planner:
    """接口：next_task(state) -> Task | None（None = 规划完毕）。"""

    def next_task(self, state: ExecutionState) -> Task | None:
        raise NotImplementedError


def _first_scene_prompt(goal: str) -> str:
    """从目标里抠出一个像样的镜头提示词（演示用，mock 世界够用）。"""
    m = re.search(r"[，。,.；;]?\s*([^，。,.；;]{4,24})$", goal)
    return m.group(1) if m else goal


class MockPlanner(Planner):
    """固定流水线：角色 → 场景 → 2 个镜头 → 成片。V0.1 演示闭环用。

    会"记得"：若 Brain 里有角色/场景一致性的成功经验，
    初始 reference_strength 自动抬高——同样的活第二次干得更好。
    """

    def __init__(self, brain=None, project=None):
        self._queue: list[Task] = []
        self._built = False
        self._brain = brain
        self.project = project

    def _learnt_strength(self, failure_key: str, base: float) -> float:
        """从大脑读取同类失败经验：**被验证的次数**越多，起手参数越稳。

        用 meta.times（该修正被验证成功的次数）而不是"记忆条数"——同源经验会累积成一条，
        按条数算会永远停在第一档（实测踩过：连跑几次起手强度一直 0.45）。
        """
        if not self._brain:
            return base
        hits = 0
        for e in self._brain.entries:
            if e.kind == "LESSON" and e.meta.get("failure") == failure_key:
                hits += int(e.meta.get("times", 1))
        return min(1.0, round(base + 0.05 * hits, 3))  # 每次验证 +0.05（必须 round：浮点会写出 0.6000000000000001）

    def _learnt_params(self) -> tuple[dict, list[str]]:
        """从大脑里学到"这个生成器的脾气"，直接按**验证过的参数**起手。

        这是 mock 版的"规划时带着记忆"（真 LLM 规划走 Brain.inject 注入同样的经验文本）：
        上次量出低清晰度/低帧率/音轨轻/黑场并修好了，这次就别再踩——第一步就该是对的。
        """
        if not self._brain:
            return {}, []
        learned = {e.meta.get("suggestion") for e in self._brain.entries if e.kind == "LESSON"}
        values = {"resolution": "720p", "fps": 30, "audio_gain_db": 10.0, "trim_black": True}
        pairs = (("resolution", "increase_resolution", "720p"),
                 ("fps", "increase_fps", "30fps"),
                 ("audio_gain_db", "boost_audio_gain", "+10dB"),
                 ("trim_black", "trim_black_frames", "去黑场"))
        inp, notes = {}, []
        for key, sug, label in pairs:
            if sug in learned:
                inp[key] = values[key]
                notes.append(label)
        return inp, notes

    def _apply_archive(self, inp: dict, cons: dict) -> str | None:
        """把项目档案里\"这个镜头上次用的参数\"盖到当前任务上（返回命中的镜头名，无命中返回 None）。"""
        if not self.project:
            return None
        hit = self.project.best_for(inp.get("prompt"), cons.get("character"),
                                    cons.get("scene"), cons.get("style"))
        if not hit:
            return None
        ti, tc = hit.get("task_input") or {}, hit.get("task_constraints") or {}
        for k in ("duration", "seed", "resolution", "fps", "audio_gain_db", "trim_black"):
            if ti.get(k) is not None:
                inp[k] = ti[k]
        for k in ("reference_strength", "scene_strength", "motion_scale"):
            if tc.get(k) is not None:
                cons[k] = tc[k]
        return str(inp.get("prompt"))[:14]

    def next_task(self, state: ExecutionState) -> Task | None:
        if not self._built:
            base = _first_scene_prompt(state.goal)
            s_char = self._learnt_strength("character_inconsistency", 0.4)
            s_scene = self._learnt_strength("scene_inconsistency", 0.4)
            seeded, notes = self._learnt_params()
            # 项目档案优先：角色/场景/风格必须沿用档案里的，否则每次跑都在"新建角色"
            char_key = next(iter(self.project.characters), "cat") if self.project else "cat"
            scene_key = next(iter(self.project.scenes), "s01") if self.project else "s01"
            style = self.project.style if self.project else "cinematic"
            shot_constraints = {"character": char_key, "style": style, "scene": scene_key,
                                "continuity": True, "reference_strength": s_char,
                                "scene_strength": s_scene}
            if s_char > 0.4 or s_scene > 0.4 or notes:
                state.log(f"🧠 记忆起手：reference_strength {s_char:.2f} / scene {s_scene:.2f}"
                          + (f" / {'、'.join(notes)}" if notes else "") + "（上次学到的）")
            shot1 = {"prompt": base, "duration": 5, "seed": 101}
            shot2 = {"prompt": base + "，特写", "duration": 5, "seed": 202}
            shot1.update(seeded)
            shot2.update(seeded)
            cons1, cons2 = dict(shot_constraints), dict(shot_constraints)
            # 项目档案（具体）优先于大脑泛化经验：这一集这个镜头做过 → 沿用上次那版参数，
            # 指纹随即命中 → 直接复用旧画面，一张都不重画。
            hits = [h for h in (self._apply_archive(shot1, cons1),
                                self._apply_archive(shot2, cons2)) if h]
            if hits:
                state.log(f"♻ 项目档案命中：{'、'.join(hits)}（本集镜头做过 → 沿用旧参数，不重新生成画面）")
            self._queue = [
                Task(ACTION_STORYBOARD,
                     input={"goal": state.goal},
                     quality={"min_score": 0.80}),
                Task(ACTION_GENERATE_CHARACTER,
                     input={"prompt": f"主角：（{base} 的主角）"},
                     constraints={"style": style, "asset_key": char_key}),
                Task(ACTION_GENERATE_SCENE,
                     input={"prompt": base},
                     constraints={"style": style, "scene_key": scene_key}),
                Task(ACTION_GENERATE_SHOT,
                     input=shot1,
                     constraints=cons1),
                Task(ACTION_GENERATE_SHOT,
                     input=shot2,
                     constraints=cons2),
                Task(ACTION_COMPOSE,
                     input={"shots": ["shot#1", "shot#2"], "output": "final.mp4"},
                     quality={"min_score": 0.85}),
            ]
            self._built = True
        return self._queue.pop(0) if self._queue else None


class LLMPlanner(Planner):
    """真 LLM 规划：要求模型只输出 JSON 任务数组（喂 schema 例子）。
    系统提示自动注入 Brain 记忆——模型想问题时天然带着历史经验（直接用）。"""

    SYSTEM = (
        "你是 HxMV 的 Planner。用户给你一个内容生产目标，你把它拆成结构化任务数组。\n"
        "只能输出合法 JSON 数组，每个元素形如：\n"
        '{"action": "GENERATE_SHOT|GENERATE_CHARACTER|GENERATE_SCENE|STORYBOARD|COMPOSE", '
        '"input": {"prompt": "...", "duration": 5}, "constraints": {"style": "cinematic", '
        '"character": "主角键"}, "quality": {"min_score": 0.82}}。\n'
        "硬要求（缺了计划就作废）：\n"
        "① 必须至少有一个 GENERATE_SHOT（成片的画面只能来自镜头，没有镜头就出不了片）；\n"
        "② 每个 GENERATE_SHOT 的 constraints 必须带 character 和 scene（执行器靠它们找参考图）；\n"
        "③ 结尾必须有 COMPOSE 把镜头拼成 final.mp4。\n"
        "不要输出任何 JSON 以外的文字。动作全部大写。"
    )

    # 模型输出的约束键别名 → 内部键。LLM 规划是自由的，字段名/漏字段都是常态。
    _CONS_ALIASES = {
        "character_name": "character", "character_key": "character", "asset_key": "character",
        "主角": "character", "角色": "character", "人物": "character",
        "scene_name": "scene", "scene_key": "scene", "场景": "scene", "背景": "scene",
    }
    _ACTIONS = (ACTION_STORYBOARD, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
                ACTION_GENERATE_SHOT, ACTION_COMPOSE)

    def __init__(self, brain=None, project=None):
        self._brain = brain
        self.project = project

    def _normalize(self, raw) -> list[Task]:
        """把模型给的 JSON 收拾成**可执行**的任务数组。

        实测坑（配上真 Key 后才暴露）：模型会漏掉 `constraints.character`，
        而执行器拿不到参考资产就抛**不可重试**错误 → 整条镜头直接终态 FAIL。
        所以这里做三件事：① 只保留已知动作；② 约束键别名归一；
        ③ 镜头缺 character/scene 时，用同一份计划里的角色/场景任务补齐，
        补不上再用项目档案里已有的键；④ 有镜头没成片时补一个 COMPOSE。
        """
        items = [t for t in (raw if isinstance(raw, list) else [raw])
                 if isinstance(t, dict) and t.get("action") in self._ACTIONS]

        char_key = scene_key = None
        for t in items:
            cons = t.get("constraints") if isinstance(t.get("constraints"), dict) else {}
            inp = t.get("input") if isinstance(t.get("input"), dict) else {}
            if t["action"] == ACTION_GENERATE_CHARACTER:
                char_key = cons.get("asset_key") or cons.get("character") or inp.get("prompt")
            elif t["action"] == ACTION_GENERATE_SCENE:
                scene_key = cons.get("scene_key") or cons.get("scene") or inp.get("prompt")
        if self.project:
            # 项目档案优先（与 MockPlanner 同一原则）：档案里已有角色/场景键就沿用。
            # 不这么做的话，模型每轮 run 给的**新**键都会让系统去新建一个角色/场景
            # ——"同一个角色跨镜头"就崩了，人工登记的真参考图（图生视频的首帧）也用不上。
            archived_char = next(iter(self.project.characters), None)
            archived_scene = next(iter(self.project.scenes), None)
            if archived_char and char_key not in self.project.characters:
                char_key = archived_char      # 模型给的新键不在档案里 → 落到档案里的那个
            if archived_scene and scene_key not in self.project.scenes:
                scene_key = archived_scene
            char_key = char_key or archived_char
            scene_key = scene_key or archived_scene

        tasks: list[Task] = []
        for t in items:
            raw_cons = t.get("constraints") if isinstance(t.get("constraints"), dict) else {}
            cons = {self._CONS_ALIASES.get(k, k): v for k, v in raw_cons.items()}
            if t["action"] == ACTION_GENERATE_SHOT:
                cons.setdefault("character", char_key)
                cons.setdefault("scene", scene_key)
                if self.project:
                    # 档案优先：模型自编的键（哪怕是"模型自己编的新键"这种）落到档案里的真键，
                    # 否则镜头拿不到那张人工登记的真参考图，跨镜头锁脸也就无从谈起。
                    if char_key and cons.get("character") not in self.project.characters:
                        cons["character"] = char_key
                    if scene_key and cons.get("scene") not in self.project.scenes:
                        cons["scene"] = scene_key
                cons = {k: v for k, v in cons.items() if v}
            tasks.append(Task(
                action=t["action"],
                input=t.get("input") if isinstance(t.get("input"), dict) else {},
                constraints=cons,
                quality=t.get("quality") if isinstance(t.get("quality"), dict) else {},
            ))

        # 有镜头没成片 → 补一个 COMPOSE（shots 留空 = 拼接本次全部镜头）
        if tasks and not any(t.action == ACTION_COMPOSE for t in tasks) \
                and any(t.action == ACTION_GENERATE_SHOT for t in tasks):
            tasks.append(Task(action=ACTION_COMPOSE, input={"shots": [], "output": "final.mp4"}))
        return tasks

    def _plan(self, state: ExecutionState) -> list[Task] | None:
        """问一次模型，返回**校验过**的任务数组；不合格返回 None（由工厂降级 MockPlanner）。

        校验为什么不省：模型会给出"看着像计划、其实出不了片"的数组——实测两次踩到
        （漏 constraints.character → 镜头终态 FAIL；干脆没排 GENERATE_SHOT → COMPOSE 没料可拼）。
        真 AI 路径必须**要么出片、要么老实降级**，不能把一份空计划跑成红。
        """
        memory_block = self._brain.inject(query=state.goal) if self._brain else ""
        project_block = self.project.inject() if self.project else ""
        blocks = "\n\n".join(b for b in (project_block, memory_block) if b)
        system = self.SYSTEM + ("\n" + blocks if blocks else "")
        text = llm.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": state.goal},
        ], temperature=0.2)
        array = json.loads(re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M))
        tasks = self._normalize(array)
        if not any(t.action == ACTION_GENERATE_SHOT for t in tasks):
            return None
        return tasks

    def next_task(self, state: ExecutionState) -> Task | None:
        if state.phase == "PLAN" and not state.tasks:
            try:
                tasks = self._plan(state)
                if tasks is None:
                    return None
                state.tasks, state.phase = tasks, "RUN"
            except Exception:
                return None  # 降级由工厂处理
        for t in state.tasks:
            if t.status.value == "PENDING":
                return t
        return None


def make_planner(state: ExecutionState, brain=None, project=None) -> Planner:
    """工厂：能接 LLM 就接，失败/未配置落 MockPlanner（可跑性是底线）。

    project：项目档案——两种规划都必须**沿用档案里的角色/场景/风格**，
    否则每次跑都会"新建角色"，前一次的画面就白做了。
    """
    if llm.llm_available():
        p = LLMPlanner(brain, project)
        probe = p.next_task(state)
        if probe is not None:
            return p
        state.log("⚠ LLM 规划失败，降级 MockPlanner")
    return MockPlanner(brain, project)
