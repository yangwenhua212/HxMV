"""Critic：系统的"眼睛"。三层结构，统一产出 QualityReport。

L1 物理检测  —— 纯算法：清晰度/黑帧/FPS/音量/时长/运动模糊
L2 视觉检测  —— 视觉模型（V0.1 mock：按一致性参数推断；接真模型后抽关键帧比对）
L3 语义检测  —— LLM/VLM：剧本符合度/情绪/连续性（可配 LLM，失败降级 mock 规则）

边界：Critic 永远不改任何东西——它只观察并报告。改东西是 Refiner/Controller 的事。
"""
from __future__ import annotations

from ..quality import QualityReport
from .state import (
    ACTION_COMPOSE, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
    ACTION_GENERATE_SHOT, Task,
)
from . import llm

# 缺陷 → 可执行修正建议（Code 消费，非给人看的自由文本）
DEFECT_FIXES = {
    "black_frame":      ("reduce_motion_scale", "首尾黑帧：裁掉或加转场"),
    "low_clarity":      ("increase_resolution", "清晰度不足：提高分辨率或降噪"),
    "motion_blur":      ("reduce_motion_scale", "运动模糊：降低运动幅度/提高快门"),
    "fps_too_low":      ("increase_fps", "帧率过低：重采样到 30fps"),
    "low_volume":       ("boost_audio_gain", "音量过低：增益 +6dB"),
    "character_inconsistency": ("increase_reference_strength", "角色不一致：加强角色参考强度/换参考帧"),
    "scene_inconsistency":     ("increase_reference_strength", "场景不一致：锁定场景参考"),
    "semantic_mismatch": ("rewrite_prompt_closer", "与剧本不符：改写 prompt 使其贴合分镜描述"),
    "too_long":         ("trim_duration", "超时长：裁剪"),
    "too_short":        ("extend_duration", "时长不足：补帧"),
}

# 每层扣分权重（物理最致命——黑帧直接废片）
_LAYER_PENALTY = {"L1_PHYSICS": 0.35, "L2_VISUAL": 0.30, "L3_SEMANTIC": 0.25}
# 单层内每个缺陷的扣分（封顶该层权重）
_PENALTY_PER_DEFECT = 0.15


class Critic:
    layer = "?"

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        raise NotImplementedError

    def _report(self, defects: list[str]) -> QualityReport:
        w = _LAYER_PENALTY[self.layer]
        score = max(0.0, 1.0 - min(w, len(defects) * _PENALTY_PER_DEFECT))
        failures = list(defects)
        suggestions = [DEFECT_FIXES[d][0] for d in defects]
        return QualityReport(layer=self.layer, score=score,
                             failures=failures, suggestions=suggestions)


class L1PhysicsCritic(Critic):
    """物理层：读 result 元数据里的物理缺陷（mock 世界已注入；真世界由 OpenCV/FFmpeg 探出）。"""

    layer = "L1_PHYSICS"

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        if task.action != ACTION_GENERATE_SHOT:
            return QualityReport(layer=self.layer)
        physics = [d for d in result.get("defects", [])
                   if d in ("black_frame", "low_clarity", "motion_blur",
                            "fps_too_low", "low_volume", "too_long", "too_short")]
        return self._report(physics)


class L2VisualCritic(Critic):
    """视觉层：一致性/构图。V0.1 mock 按注入缺陷判；真模型版本抽 3-5 关键帧做参考比对。"""

    layer = "L2_VISUAL"

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        if task.action != ACTION_GENERATE_SHOT:
            return QualityReport(layer=self.layer)
        visual = [d for d in result.get("defects", [])
                  if d in ("character_inconsistency", "scene_inconsistency")]
        return self._report(visual)


class L3SemanticCritic(Critic):
    """语义层：剧本符合度/情绪/连续性。有 LLM 用 LLM 看，没有就规则兜底。"""

    layer = "L3_SEMANTIC"

    SYSTEM = (
        "你是 HxMV 的 L3 语义评审。给出一段分镜描述和生成结果的说明，判断是否符合剧本意图。\n"
        '只输出 JSON：{"failures": [], "score": 0.9}，score 0-1。'
        "仅在明显不符合（主角/场景/情绪/动作对不上剧本）时填 failures，如 character_mismatch / action_mismatch / emotion_wrong / plot_break。"
    )

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        defects = result.get("defects", [])
        semantic = [d for d in defects if d == "semantic_mismatch"]
        if not semantic and llm.llm_available() and task.action == ACTION_GENERATE_SHOT:
            try:
                prompt = task.input.get("prompt", "")
                text = llm.chat([
                    {"role": "system", "content": self.SYSTEM},
                    {"role": "user", "content": f"分镜: {prompt}\n结果说明: {result}"},
                ], temperature=0.0, max_tokens=200)
                import json
                import re
                data = json.loads(re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M))
                fail = data.get("failures", [])
                score = max(0.0, min(1.0, float(data.get("score", 1.0))))
                return QualityReport(layer=self.layer, score=score, failures=fail,
                                     suggestions=[f"rewrite_prompt_closer:{f}" for f in fail])
            except Exception:
                pass  # LLM 不稳时用规则兜底，保证 Critic 永远可跑
        return self._report(semantic)


class PipelineCritic:
    """依次跑三层并合并成一份报告。"""

    def __init__(self):
        self.layers: list[Critic] = [L1PhysicsCritic(), L2VisualCritic(), L3SemanticCritic()]

    def evaluate_layers(self, task: Task, result: dict) -> list[QualityReport]:
        """逐层评估，返回每层独立报告（客户端/面板展示分层分数用）。"""
        return [layer.evaluate(task, result) for layer in self.layers]

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        merged = QualityReport(layer="PIPELINE", score=1.0)
        for r in self.evaluate_layers(task, result):
            merged = merged.merge(r)
        return merged
