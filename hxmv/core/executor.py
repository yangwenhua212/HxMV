"""Executor：代码负责"到底怎么做"的执行层。

两个实现：
- MockVideoExecutor：不调任何视频 API，在 mock 世界里产生带真实感缺陷的结果
  （按参数概率注入），让 Critic/Refiner/Controller 有活可干。零依赖可跑。
- ProviderExecutor：接真实生成服务（VideoProvider 适配器）。Task 在此投影成
  API 请求参数（constraints → cfg_scale 等），基础设施错误抛 ProviderError 由
  Loop 按"服务重试"处理，与质量 FAIL 分道。

缺陷注入规则刻意与参数挂钩——reference_strength 越高，一致性缺陷越少，
这样 Refiner 的"调参修正"是真实有效的，闭环才有意义。
"""
from __future__ import annotations

import hashlib
import os
import random
from dataclasses import replace

from ..providers.base import ProviderError
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
    """确定性 mock 执行器：同一 (task, attempt) 永远产出同一缺陷集。

    project 传入时同样走**指纹复用**：同一个画面参数不重复"生成"（mock 世界里
    表现为不重新抽缺陷），演示"接着做、不重新出画面"的行为。
    """

    def __init__(self, project=None):
        self.project = project

    def _rng(self, task: Task) -> random.Random:
        h = hashlib.md5(f"{task.task_id}:{task.retry_policy.get('attempts', 0)}:{task.input}".encode()).hexdigest()
        return random.Random(int(h[:8], 16))

    def _fp(self, task: Task) -> str:
        from .project import fingerprint
        return fingerprint({**task.input, **task.constraints,
                            "prompt": task.input.get("prompt") or task.input.get("goal")})

    def _physics_defects(self, rng: random.Random, task: Task) -> list[str]:
        """L1 物理缺陷：**必须与参数挂钩**，否则 Refiner 的调参永远修不好它
        （v0.4 修正：旧版纯随机 → 末次尝试随机中一个就终态 FAIL，闭环白跑）。

        概率按\"低端生成器\"的脾气标定：分辨率/帧率/增益/trim_black 都是真能改好的旋钮。
        """
        inp = task.input
        res = str(inp.get("resolution") or "")
        height = {"480p": 480, "720p": 720, "1080p": 1080}.get(res, 360)
        p_clarity = 0.07 if height < 480 else 0.04 if height < 720 else 0.0

        fps = float(inp.get("fps") or 0)
        p_fps = 0.05 if not fps else (0.02 if fps < 24 else 0.0)

        gain = inp.get("audio_gain_db")
        p_volume = 0.05 if gain is None else (0.02 if float(gain) < 6 else 0.0)

        p_black = 0.0 if inp.get("trim_black") else 0.05

        motion = float(task.constraints.get("motion_scale", 0.4))
        p_blur = max(0.0, 0.05 + motion * 0.13)      # 运动越大越容易糊
        p_freeze = 0.05 if motion <= 0.05 else 0.0   # 不给运动 → 画面静止

        table = [("black_frame", p_black), ("low_clarity", p_clarity),
                 ("motion_blur", p_blur), ("fps_too_low", p_fps),
                 ("low_volume", p_volume), ("frozen_frame", p_freeze)]
        return [d for d, p in table if p and rng.random() < p]

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
            fp = self._fp(task)
            hit = self.project.shot(fp) if self.project else None
            if hit:      # 档案里已有同参数的画面 → 直接复用，不重新"生成"（也不花钱）
                saved = dict(hit["result"])
                saved.update({"reused": True, "fingerprint": fp, "cost_units": 0.0})
                return saved
            defects = (self._physics_defects(rng, task)
                       + self._consistency_defects(rng, task)
                       + self._semantic_defects(rng, task))
            result = {
                "media": "mock_video.mp4", "duration": task.input.get("duration", 5),
                "fps": 24, "defects": defects, "reused": False, "fingerprint": fp,
                "params": {"seed": task.input.get("seed"), "reference_strength": task.constraints.get("reference_strength")},
                "cost_units": cost,
            }
            if self.project:
                self.project.register_shot(fp, result["media"], result=result,
                                           prompt=task.input.get("prompt"))
            return result

        if task.action == ACTION_COMPOSE:
            return {"output": task.input.get("output", "final.mp4"),
                    "shots": task.input.get("shots", []), "cost_units": cost}

        return {"cost_units": cost}


# ---- 工程参数 → API 请求参数的投影表（ProviderExecutor 用） ----
# 左侧是闭环内部语义（Refiner 调的就是这些），右侧是生成服务的消费格式。
# 真实 API 的键名不同就在这里改，别动 Refiner/Controller。
_PROJECTION = {
    ("constraints", "reference_strength"): ("input", "cfg_scale", lambda v: round(float(v) * 10, 1)),
    ("constraints", "scene_strength"):     ("input", "scene_cfg", lambda v: round(float(v) * 10, 1)),
    ("constraints", "motion_scale"):        ("input", "motion_strength", lambda v: float(v)),
    ("input", "audio_gain_db"):             ("input", "audio_gain_db", lambda v: float(v)),
    ("input", "fps"):                       ("input", "fps", lambda v: int(v)),
    ("input", "resolution"):                ("input", "resolution", lambda v: v),
}


class ProviderExecutor(Executor):
    """把 Task 投影成 API 请求参数后交给 VideoProvider 执行。"""

    def __init__(self, provider):
        self.provider = provider

    def execute(self, task: Task) -> dict:
        api_task = replace(task)
        api_task.input = dict(task.input)          # 浅拷贝，不污染原 Task（审计干净）
        api_task.constraints = dict(task.constraints)
        for (src_where, src_key), (dst_where, dst_key, fn) in _PROJECTION.items():
            src = task.constraints if src_where == "constraints" else task.input
            if src_key in src:
                (api_task.input if dst_where == "input" else api_task.constraints)[dst_key] = fn(src[src_key])
        # 记录投影，便于查"哪个内部参数驱动了 API 的哪个参数"
        api_task.input["_projected"] = {
            k: (api_task.input if w == "input" else api_task.constraints).get(k)
            for (w, k), *_ in _PROJECTION.items()}
        return self.provider.generate(api_task)


def make_executor(project=None, episode: int | None = None):
    """工厂：HXMV_PROVIDER=local/fake/kling → ProviderExecutor；否则/失败落 Mock。

    project = 项目档案（跨 run 记忆风格/角色/已生成画面）：local provider 会先查档，
    命中就复用已有画面，不重新生成。
    """
    name = os.environ.get("HXMV_PROVIDER", "").lower()
    if name:
        try:
            if name == "local":
                from ..providers.local_render import LocalRenderProvider
                return ProviderExecutor(LocalRenderProvider(project=project))
            if name == "fake":
                from ..providers.fake_api import FakeApiProvider
                return ProviderExecutor(FakeApiProvider(project=project))
            if name in ("zhipu", "bigmodel", "cogvideo"):
                from ..providers.zhipu_video import ZhipuVideoProvider
                return ProviderExecutor(ZhipuVideoProvider(project=project))
            if name in ("kling", "klingai"):
                from ..providers.kling_example import KlingStyleProvider
                return ProviderExecutor(KlingStyleProvider(project=project))
            raise ProviderError(f"未知 provider: {name}", retryable=False)
        except ProviderError as e:
            print(f"⚠ {e} → 降级 MockVideoExecutor")
    return MockVideoExecutor(project=project)
