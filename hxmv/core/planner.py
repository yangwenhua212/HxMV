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

    def __init__(self, brain=None):
        self._queue: list[Task] = []
        self._built = False
        self._brain = brain

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
        return min(1.0, base + 0.05 * hits)  # 每次验证 +0.05

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

    def next_task(self, state: ExecutionState) -> Task | None:
        if not self._built:
            base = _first_scene_prompt(state.goal)
            s_char = self._learnt_strength("character_inconsistency", 0.4)
            s_scene = self._learnt_strength("scene_inconsistency", 0.4)
            seeded, notes = self._learnt_params()
            shot_constraints = {"character": "cat", "style": "cinematic", "scene": "s01",
                                "continuity": True, "reference_strength": s_char,
                                "scene_strength": s_scene}
            if s_char > 0.4 or s_scene > 0.4 or notes:
                state.log(f"🧠 记忆起手：reference_strength {s_char:.2f} / scene {s_scene:.2f}"
                          + (f" / {'、'.join(notes)}" if notes else "") + "（上次学到的）")
            shot1 = {"prompt": base, "duration": 5, "seed": 101}
            shot2 = {"prompt": base + "，特写", "duration": 5, "seed": 202}
            shot1.update(seeded)
            shot2.update(seeded)
            self._queue = [
                Task(ACTION_STORYBOARD,
                     input={"goal": state.goal},
                     quality={"min_score": 0.80}),
                Task(ACTION_GENERATE_CHARACTER,
                     input={"prompt": f"主角：小猫（{base} 的主角）"},
                     constraints={"style": "cinematic", "asset_key": "cat"}),
                Task(ACTION_GENERATE_SCENE,
                     input={"prompt": base},
                     constraints={"style": "cinematic", "scene_key": "s01"}),
                Task(ACTION_GENERATE_SHOT,
                     input=shot1,
                     constraints=dict(shot_constraints)),
                Task(ACTION_GENERATE_SHOT,
                     input=shot2,
                     constraints=dict(shot_constraints)),
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
        "不要输出任何 JSON 以外的文字。动作全部大写。"
    )

    def __init__(self, brain=None):
        self._brain = brain

    def next_task(self, state: ExecutionState) -> Task | None:
        if state.phase == "PLAN" and not state.tasks:
            try:
                memory_block = self._brain.inject(query=state.goal) if self._brain else ""
                system = self.SYSTEM + ("\n" + memory_block if memory_block else "")
                text = llm.chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": state.goal},
                ], temperature=0.2)
                array = json.loads(re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M))
                state.tasks = [Task(**{k: v for k, v in t.items() if k in
                                       ("action", "input", "constraints", "quality")})
                               for t in array]
                state.phase = "RUN"
            except Exception:
                return None  # 降级由工厂处理
        for t in state.tasks:
            if t.status.value == "PENDING":
                return t
        return None


def make_planner(state: ExecutionState, brain=None) -> Planner:
    """工厂：能接 LLM 就接，失败/未配置落 MockPlanner（可跑性是底线）。"""
    if llm.llm_available():
        p = LLMPlanner(brain)
        probe = p.next_task(state)
        if probe is not None:
            return p
        state.log("⚠ LLM 规划失败，降级 MockPlanner")
    return MockPlanner(brain)
