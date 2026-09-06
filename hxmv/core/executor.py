"""Executor：代码负责"到底怎么做"的执行层。

V0.1 = MockVideoExecutor：不调任何视频 API，在 mock 世界里产生带
真实感缺陷的结果（按参数概率注入），让 Critic/Refiner/Controller 有活可干。
缺陷注入规则刻意与参数挂钩——reference_strength 越高，一致性缺陷越少，
这样 Refiner 的"调参修正"是真实有效的，闭环才有意义。

后续接真实生成器时，只需实现同一 Executor 接口，把 action 映射到 API 调用。
"""
from __future__ import annotations

import hashlib
import random

from .state import (
    ACTION_COMPOSE, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
    ACTION_GENERATE_SHOT, ACTION_STORYBOARD, Task,
)

# mock 世界里各动作的单次成本（单位：元），接真实 API 时替换为计价映射
COST_PER_ACTION = {
    ACTION_STORYBOARD: 0.2,
    ACTION_GENERATE_CHARACTER: 0.5,
    ACTION_GENERATE_SCENE: 0.5,
    ACTION_GENERATE_SHOT: 3.0,
    ACTION_COMPOSE: 0.8,
}


class Executor:
    def execute(self, task: Task) -> dict:
        raise NotImplementedError


class MockVideoExecutor(Executor):
    """确定性 mock 执行器：同一 (task, attempt) 永远产出同一缺陷集。"""

    def _rng(self, task: Task) -> random.Random:
        h = hashlib.md5(f"{task.task_id}:{task.retry_policy.get('attempts', 0)}:{task.input}".encode()).hexdigest()
        return random.Random(int(h[:8], 16))

    def _physics_defects(self, rng: random.Random) -> list[str]:
        """L1 物理缺陷：与参数基本无关，纯随机（真实世界也一样——物理缺陷看命）。"""
        table = [
            ("black_frame", 0.04), ("low_clarity", 0.07),
            ("motion_blur", 0.10), ("fps_too_low", 0.04),
            ("low_volume", 0.04),
        ]
        return [d for d, p in table if rng.random() < p]

    def _consistency_defects(self, rng: random.Random, task: Task) -> list[str]:
        """L2 一致性缺陷：与 reference_strength 强相关（Refiner 能修）。"""
        strength = float(task.constraints.get("reference_strength", 0.4))
        scene_strength = float(task.constraints.get("scene_strength", strength))
        p_char = max(0.05, 0.42 - strength * 0.32)
        p_scene = max(0.04, 0.30 - scene_strength * 0.26)
        out = []
        if "character" in task.constraints and rng.random() < p_char:
            out.append("character_inconsistency")
        if "scene" in task.constraints and rng.random() < p_scene:
            out.append("scene_inconsistency")
        return out

    def _semantic_defects(self, rng: random.Random, task: Task) -> list[str]:
        """L3 语义缺陷：剧本/情绪不符合（mock：低概率 + prompt 不含目标词时惩罚）。"""
        if "prompt" not in task.input:
            return []
        return ["semantic_mismatch"] if rng.random() < 0.08 else []

    def execute(self, task: Task) -> dict:
        rng = self._rng(task)
        cost = COST_PER_ACTION.get(task.action, 1.0)

        if task.action == ACTION_STORYBOARD:
            return {"storyboard": [f"镜头 {i}: {task.input.get('goal', '')[:12]}…" for i in (1, 2)],
                    "cost_units": cost}
        if task.action in (ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE):
            return {"asset": task.constraints.get("asset_key") or task.constraints.get("scene_key"),
                    "cost_units": cost}

        if task.action == ACTION_GENERATE_SHOT:
            defects = (self._physics_defects(rng)
                       + self._consistency_defects(rng, task)
                       + self._semantic_defects(rng, task))
            return {
                "media": "mock_video.mp4", "duration": task.input.get("duration", 5),
                "fps": 24, "defects": defects,
                "params": {"seed": task.input.get("seed"), "reference_strength": task.constraints.get("reference_strength")},
                "cost_units": cost,
            }

        if task.action == ACTION_COMPOSE:
            return {"output": task.input.get("output", "final.mp4"),
                    "shots": task.input.get("shots", []), "cost_units": cost}

        return {"cost_units": cost}
