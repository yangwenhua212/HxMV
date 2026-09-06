"""FakeApiProvider：模拟"真实 API 服务"的端到端演示 provider。

行为刻意仿真真实服务（区别于 MockVideoExecutor 的纯内存随机）：
- 有提交/轮询两段式（0.2s 延迟），验证 ProviderExecutor 的轮询桥接
- 按请求里的 cfg_scale（= reference_strength 映射）决定一致性缺陷概率——证明
  Task.constraints 经请求参数真实影响了生成结果（与可灵示例同一映射逻辑）
- 偶发一次"服务端失败"（ProviderError retryable），演示基础设施重试与质量 FAIL 分道

用途：HXMV_PROVIDER=fake 跑通"Provider 化"闭环，验证接口，不花一分钱。
"""
from __future__ import annotations

import random
import time

from .base import ProviderError, VideoProvider


class FakeApiProvider(VideoProvider):
    name = "fake"
    action_map = {"GENERATE_SHOT": "text2video", "GENERATE_SCENE": "image",
                  "GENERATE_CHARACTER": "image"}

    def __init__(self, seed: int = 7):
        self._rng = random.Random(seed)
        self._submitted = 0

    def generate(self, task) -> dict:
        time.sleep(0.2)  # 模拟网络往返
        # 偶发服务端故障（约 12%），且只在非重试尝试时——演示基础设施重试
        attempts = task.retry_policy.get("attempts", 0)
        if attempts == 0 and self._rng.random() < 0.12:
            raise ProviderError("上游 503：服务过载，稍后重试", retryable=True, cost_units=0.0)

        if task.action not in self.action_map:
            return {"cost_units": 0.0}

        self._submitted += 1
        cfg = float(task.input.get("cfg_scale", 4.0)) / 10.0  # 0.4 默认

        defects: list[str] = []
        if task.action == "GENERATE_SHOT":
            r = self._rng
            physics = [("black_frame", 0.05), ("low_clarity", 0.06),
                       ("motion_blur", 0.08), ("low_volume", 0.05)]
            for d, p in physics:
                if r.random() < p:
                    defects.append(d)
            if "character" in task.constraints and r.random() < max(0.05, 0.45 - cfg * 0.35):
                defects.append("character_inconsistency")
            if "scene" in task.constraints and r.random() < max(0.04, 0.32 - cfg * 0.28):
                defects.append("scene_inconsistency")

        return {
            "media": f"fake://video/{task.task_id}.mp4", "duration": task.input.get("duration", 5),
            "defects": defects,
            "params": {"provider": self.name, "task_no": self._submitted,
                       "cfg_scale": cfg},
            "cost_units": self.estimate_cost(task.action),
        }

    def estimate_cost(self, action: str) -> float:
        return 3.0 if action == "GENERATE_SHOT" else 0.5
