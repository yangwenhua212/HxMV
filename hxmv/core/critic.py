"""Critic：系统的\"眼睛\"。三层结构，统一产出 QualityReport。

L1 物理检测  —— 纯算法：清晰度/黑帧/FPS/音量/时长/静止
L2 视觉检测  —— 一致性/构图：与参考图的**外观距离**（真像素比对）
L3 语义检测  —— LLM/VLM：剧本符合度/情绪/连续性（可配 LLM，失败降级规则）

v0.4 升级：\"眼睛\"分两条路，自动选路——

- **有真媒体文件**（`result["media"]/["output"]` 是磁盘上真实存在的文件）→ 走 `media/probe`：
  用 ffmpeg/ffprobe 从像素和音轨里**量**指标再判缺陷。这是真观察。
- **没有真文件**（MockVideoExecutor / FakeApiProvider 的世界）→ 读 executor 注入的缺陷标签。
  这不是真观察，是\"服务端已知问题\"的加速通道；保留它是为了让闭环在没有媒体时仍可跑。

两条路产出同一个 QualityReport，Controller/Refiner 完全不用知道区别——边界不变。

边界：Critic 永远不改任何东西——它只观察并报告。改东西是 Refiner/Controller 的事。
"""
from __future__ import annotations

from ..media import probe
from ..quality import QualityReport
from .state import (
    ACTION_COMPOSE, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
    ACTION_GENERATE_SHOT, Task,
)
from . import llm

# 缺陷 → 可执行修正建议（Code 消费，非给人看的自由文本）
DEFECT_FIXES = {
    "black_frame":      ("trim_black_frames", "黑帧：裁掉/去掉片头黑场"),
    "low_clarity":      ("increase_resolution", "清晰度不足：提高分辨率或码率"),
    "motion_blur":      ("reduce_motion_scale", "运动模糊：降低运动幅度/提高快门"),
    "fps_too_low":      ("increase_fps", "帧率过低：重采样到 30fps"),
    "low_volume":       ("boost_audio_gain", "音量过低：增益 +10dB"),
    "frozen_frame":     ("increase_motion_scale", "画面静止：提高运动幅度"),
    "too_short":        ("extend_duration", "时长不足：补时长"),
    "too_long":         ("trim_duration", "超时长：裁时长"),
    "character_inconsistency": ("increase_reference_strength", "角色不一致：加强角色参考强度/换参考帧"),
    "scene_inconsistency":     ("increase_reference_strength", "场景不一致：锁定场景参考"),
    "semantic_mismatch": ("rewrite_prompt_closer", "与剧本不符：改写 prompt 使其贴合分镜描述"),
}

# 每层扣分权重（物理最致命——黑帧直接废片）
_LAYER_PENALTY = {"L1_PHYSICS": 0.35, "L2_VISUAL": 0.30, "L3_SEMANTIC": 0.25}
# 单层内每个缺陷的扣分（封顶该层权重）
_PENALTY_PER_DEFECT = 0.15


class Critic:
    layer = "?"

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        raise NotImplementedError

    def _report(self, defects: list[str], detail: str = "") -> QualityReport:
        w = _LAYER_PENALTY[self.layer]
        score = max(0.0, 1.0 - min(w, len(defects) * _PENALTY_PER_DEFECT))
        failures = list(defects)
        # failures[i] ↔ suggestions[i] 必须索引对齐（Refiner 靠 zip 配对）：
        # 未知缺陷也要给一个显式建议，绝不留空位。
        suggestions = [DEFECT_FIXES[d][0] if d in DEFECT_FIXES else f"review_{d}"
                       for d in defects]
        return QualityReport(layer=self.layer, score=score,
                             failures=failures, suggestions=suggestions, detail=detail)


class L1PhysicsCritic(Critic):
    """物理层：**先想办法量**。

    真媒体 → ffprobe/ffmpeg 实测（分辨率/帧率/时长/音量/黑帧/静止）；
    非真媒体 → 读 result 里注入的物理缺陷标签（mock 世界加速通道）。
    """

    layer = "L1_PHYSICS"
    _INJECTED = ("black_frame", "low_clarity", "motion_blur", "fps_too_low",
                 "low_volume", "too_long", "too_short", "frozen_frame")

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        if task.action not in (ACTION_GENERATE_SHOT, ACTION_COMPOSE):
            return QualityReport(layer=self.layer)

        target = result.get("media") if task.action == ACTION_GENERATE_SHOT else result.get("output")
        metrics = probe.inspect(target, expect_duration=task.input.get("duration"))
        if metrics is not None:
            result["metrics"] = metrics                    # 量出来的原始指标，随事件流给面板/审计
            return self._report(metrics["defects"], detail=probe.describe(metrics))

        physics = [d for d in result.get("defects", []) if d in self._INJECTED]
        return self._report(physics)


class L2VisualCritic(Critic):
    """视觉层：一致性。

    真媒体 + 参考图 → 与参考图的**外观一致度**（缩略图 RGB 距离，真像素）；
    否则 → 读注入的一致性标签（V0.1 mock 路径）。
    """

    layer = "L2_VISUAL"

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        if task.action != ACTION_GENERATE_SHOT:
            return QualityReport(layer=self.layer)

        media = result.get("media")
        # 优先用\"零漂移基线帧\"（同编码管线）而不是参考图原图——消掉编码差异带来的假距离
        ref = result.get("reference_baseline") or result.get("reference")
        consistency = probe.appearance_consistency(media, ref) if (media and ref) else None
        if consistency is not None:
            result["consistency"] = consistency            # 供面板/审计：与参考图的一致度
            thr = probe.THRESHOLDS["min_consistency"]
            detail = f"与参考图外观一致度 {consistency:.3f}（阈值 {thr}）"
            if consistency >= thr:
                return QualityReport(layer=self.layer, score=consistency, detail=detail)
            defect = ("character_inconsistency" if "character" in task.constraints
                      else "scene_inconsistency")
            return QualityReport(layer=self.layer, score=consistency,
                                 failures=[defect],
                                 suggestions=[DEFECT_FIXES[defect][0]], detail=detail)

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
