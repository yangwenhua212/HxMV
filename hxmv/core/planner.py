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
    """固定流水线：角色 → 场景 → 2 个镜头 → 成片。V0.1 演示闭环用。"""

    def __init__(self):
        self._queue: list[Task] = []
        self._built = False

    def next_task(self, state: ExecutionState) -> Task | None:
        if not self._built:
            base = _first_scene_prompt(state.goal)
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
                     input={"prompt": base, "duration": 5, "seed": 101},
                     constraints={"character": "cat", "style": "cinematic", "scene": "s01",
                                  "continuity": True, "reference_strength": 0.4}),
                Task(ACTION_GENERATE_SHOT,
                     input={"prompt": base + "，特写", "duration": 5, "seed": 202},
                     constraints={"character": "cat", "style": "cinematic", "scene": "s01",
                                  "continuity": True, "reference_strength": 0.4}),
                Task(ACTION_COMPOSE,
                     input={"shots": ["shot#1", "shot#2"], "output": "final.mp4"},
                     quality={"min_score": 0.85}),
            ]
            self._built = True
        return self._queue.pop(0) if self._queue else None


class LLMPlanner(Planner):
    """真 LLM 规划：要求模型只输出 JSON 任务数组（喂 schema 例子）。"""

    SYSTEM = (
        "你是 HxMV 的 Planner。用户给你一个内容生产目标，你把它拆成结构化任务数组。\n"
        "只能输出合法 JSON 数组，每个元素形如：\n"
        '{"action": "GENERATE_SHOT|GENERATE_CHARACTER|GENERATE_SCENE|STORYBOARD|COMPOSE", '
        '"input": {"prompt": "...", "duration": 5}, "constraints": {"style": "cinematic", '
        '"character": "主角键"}, "quality": {"min_score": 0.82}}。\n'
        "不要输出任何 JSON 以外的文字。动作全部大写。"
    )

    def next_task(self, state: ExecutionState) -> Task | None:
        if state.phase == "PLAN" and not state.tasks:
            try:
                text = llm.chat([
                    {"role": "system", "content": self.SYSTEM},
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


def make_planner(state: ExecutionState) -> Planner:
    """工厂：能接 LLM 就接，失败/未配置落 MockPlanner（可跑性是底线）。"""
    if llm.llm_available():
        p = LLMPlanner()
        probe = p.next_task(state)
        if probe is not None:
            return p
        state.log("⚠ LLM 规划失败，降级 MockPlanner")
    return MockPlanner()
