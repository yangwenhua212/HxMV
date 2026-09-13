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

import json
import re

from ..media import probe
from ..quality import QualityReport
from .state import (
    ACTION_COMPOSE, ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SCENE,
    ACTION_GENERATE_SHOT, Task,
)
from . import llm

# 缺陷 → 可执行修正建议（Code 消费，非给人看的自由文本）
# 建议名必须是 refiner._ADJUST 里的键（否则 Refiner 调不动 → 任务直接终态 FAIL）
DEFECT_FIXES = {
    "black_frame":      ("trim_black_frames", "黑帧：裁掉/去掉片头黑场"),
    "low_clarity":      ("increase_resolution", "清晰度不足：提高分辨率或码率"),
    "motion_blur":      ("reduce_motion_scale", "运动模糊：降低运动幅度/提高快门"),
    "fps_too_low":      ("increase_fps", "帧率过低：重采样到 30fps"),
    "low_volume":       ("boost_audio_gain", "音量过低：增益 +10dB"),
    "no_audio":         ("enable_audio", "没有音轨：让生成模型带音频"),
    "frozen_frame":     ("increase_motion_scale", "画面静止：提高运动幅度"),
    "too_short":        ("extend_duration", "时长不足：补时长"),
    "too_long":         ("trim_duration", "超时长：裁时长"),
    "character_inconsistency": ("increase_reference_strength", "角色不一致：加强角色参考强度/换参考帧"),
    "scene_inconsistency":     ("increase_reference_strength", "场景不一致：锁定场景参考"),
    "semantic_mismatch": ("rewrite_prompt_closer", "与剧本不符：改写 prompt 使其贴合分镜描述"),
    # L3 视觉评审的四类语义失败（真看图之后才会出现；修正方向都落在既有旋钮上）
    "character_mismatch": ("increase_reference_strength", "画面里的人不是该角色：加强角色参考强度/换参考帧"),
    "action_mismatch":    ("rewrite_prompt_closer", "动作与分镜不符：改写 prompt 写明动作"),
    "emotion_wrong":      ("rewrite_prompt_closer", "情绪与分镜不符：改写 prompt 补情绪与氛围"),
    "plot_break":         ("rewrite_prompt_closer", "与前后镜头不连续：改写 prompt 补衔接"),
}

# 每层扣分权重（物理最致命——黑帧直接废片）
_LAYER_PENALTY = {"L1_PHYSICS": 0.35, "L2_VISUAL": 0.30, "L3_SEMANTIC": 0.25}
# 单层内每个缺陷的扣分（封顶该层权重）
_PENALTY_PER_DEFECT = 0.15


def _parse_json(text: str) -> dict | None:
    """宽容解析评审模型返回的 JSON：容忍 ```json 围栏与前后多余文字。

    解析不出来返回 None —— 调用方据此判定「这次评审没有可信结论」，退回兜底路径，
    绝不用残缺结果冒充判定。
    """
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


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
                 "low_volume", "no_audio", "too_long", "too_short", "frozen_frame")

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
    """视觉层：一致性（角色/场景）。

    **有视觉模型 → 真帧 vs 参考图的身份判定**：抽 3 帧真画面 + 参考图一起交给视觉模型，
    问的是「是不是同一个角色/同一个场景」而不是「像素差多少」——对运镜、光照、压缩不敏感，
    这正是像素距离做不到的那一步。
    **没有视觉模型 → 退回已标定的 RGB 外观一致度阈值**（旧行为），detail 里如实标明「未做身份检查」。
    """

    layer = "L2_VISUAL"
    FRAMES = 3

    SYSTEM = (
        "你是 HxMV 的 L2 一致性评审。第一张图是基准参考，后面几张来自同一镜头的实际画面。\n"
        "只判断两件事：① 后面每帧里的主体是不是参考里的**同一个角色**；"
        "② 是不是**同一个场景**。\n"
        "换个角度、远近、光照、压缩画质都不算不一致；换人/换景/换服装才算。\n"
        '只输出 JSON：{"same_character": true, "same_scene": true, "reason": "一句话"}。'
    )

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        if task.action != ACTION_GENERATE_SHOT:
            return QualityReport(layer=self.layer)

        media = result.get("media")
        # 优先用\"零漂移基线帧\"（同编码管线）而不是参考图原图——消掉编码差异带来的假距离
        ref = result.get("reference_baseline") or result.get("reference")
        at_ratio = result.get("consistency_at_ratio")
        consistency = probe.appearance_consistency(
            media, ref, at_ratio=0.5 if at_ratio is None else float(at_ratio)) if (media and ref) else None
        if consistency is not None:
            result["consistency"] = consistency

        verdict = self._identity_review(task, result, media, ref)
        if verdict is not None:
            return verdict

        if consistency is not None:
            thr = probe.THRESHOLDS["min_consistency"]
            detail = f"与参考图外观一致度 {consistency:.3f}（阈值 {thr}）· 未做身份检查（无视觉模型）"
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

    def _identity_review(self, task: Task, result: dict, media: str, ref: str) -> QualityReport | None:
        """真帧 + 参考图交给视觉模型判身份。不可用/失败返回 None（调用方退回像素路径）。"""
        if not llm.vision_available():
            return None
        if not (probe.is_media_file(media) and probe.is_media_file(ref)):
            return None
        tmp, frames = probe.extract_frames(media, count=self.FRAMES)
        try:
            if not frames:
                return None
            # 只问"给了参考的那一类"：只有角色参考图时去问场景，会得到"场景不一致"——
            # 而它根本没有场景参考，修无可修（真 AI 视频实测：换成下雪背景就被判死）。
            kind = str(result.get("reference_kind") or "").lower()
            ask_char = kind != "scene"
            ask_scene = (kind == "scene") or not kind
            text = f"第一张图 = 基准参考；后面 {len(frames)} 张 = 本镜头实际画面（按时间顺序）。"
            if ask_char and not ask_scene:
                text += "参考是**角色**基准：只判断主体是不是同一个角色（场景是否相同不用管）。"
            elif ask_scene and not ask_char:
                text += "参考是**场景**基准：只判断是不是同一个场景（主体是谁不用管）。"
            else:
                text += "判断主体角色与场景是否与参考一致。"
            who = task.constraints.get("character") or task.constraints.get("characters")
            if who and ask_char:
                text += f"参考角色：{who}。"
            data = _parse_json(llm.chat_vision(self.SYSTEM, text, [ref] + frames))
            if data is None:
                return None
            same_char = bool(data.get("same_character", True))
            same_scene = bool(data.get("same_scene", True))
            result["identity_review"] = {
                "model": llm.vision_model(), "frames": len(frames),
                "same_character": same_char, "same_scene": same_scene,
                "reason": str(data.get("reason", ""))[:200],
                "pixel_consistency": result.get("consistency"),
            }
            defects: list[str] = []
            if ask_char and not same_char:
                defects.append("character_inconsistency")
            if ask_scene and not same_scene:
                defects.append("scene_inconsistency")
            char_txt = ("一致" if same_char else "不一致") if ask_char else "未提供参考·不判定"
            scene_txt = ("一致" if same_scene else "不一致") if ask_scene else "未提供参考·不判定"
            detail = f"视觉身份判定（参考 + {len(frames)} 帧）：角色{char_txt} / 场景{scene_txt}"
            if result.get("consistency") is not None:
                detail += f" · 像素一致度 {result['consistency']:.3f}（遥测）"
            return self._report(defects, detail=detail)
        except Exception as exc:  # 视觉模型挂了不能拖垮闭环：退回像素路径
            result["identity_review"] = {"error": str(exc)[:160]}
            return None
        finally:
            probe.cleanup_frames(tmp)


class L3SemanticCritic(Critic):
    """语义层：剧本符合度/情绪/连续性。

    **有视觉模型 → 真抽帧喂视觉模型**（从产物里抽 4 帧真画面，连同分镜描述一起交给它看）——
    这才是语义闭环成立的前提。
    **没有视觉模型 → 退回文字判断**（旧行为），并在 detail 里写明「未做视觉检查」，
    绝不冒充看过画面。
    """

    layer = "L3_SEMANTIC"
    FRAMES = 4

    SYSTEM = (
        "你是 HxMV 的 L3 语义评审，会拿到**真实抽帧**和分镜描述。\n"
        "逐条判断画面是否符合分镜：角色对不对、动作对不对、情绪对不对、与前后镜头连不连续。\n"
        "必须给出明确结论 ok（true=符合分镜，false=不符），score 只作参考。\n"
        '只输出 JSON：{"ok": true, "failures": [], "score": 0.9, "reason": "一句话"}，score 0-1。\n'
        "failures 只填**确实对不上**的项，取值限定：character_mismatch / action_mismatch / "
        "emotion_wrong / plot_break；都对得上就留空数组。"
    )

    # 没配视觉模型时的兜底提示词：**没有图**，只能看文字说明（别让它以为看过画面）
    SYSTEM_TEXT = (
        "你是 HxMV 的 L3 语义评审。只给你分镜描述和生成结果的说明文字（**没有画面**），"
        "判断是否符合剧本意图。\n"
        '只输出 JSON：{"failures": [], "score": 0.9}，score 0-1。\n'
        "仅在明显不符合（主角/场景/情绪/动作对不上剧本）时填 failures，取值："
        "character_mismatch / action_mismatch / emotion_wrong / plot_break。"
    )

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        defects = result.get("defects", [])
        semantic = [d for d in defects if d == "semantic_mismatch"]
        if task.action != ACTION_GENERATE_SHOT:
            return self._report(semantic)

        prompt = task.input.get("prompt", "")
        visual = self._visual_review(task, result, prompt)
        if visual is not None:
            return visual
        return self._text_review(task, result, prompt)

    def _visual_review(self, task: Task, result: dict, prompt: str) -> QualityReport | None:
        """真抽帧 → 视觉模型。不可用/失败返回 None（调用方退回文字路径）。"""
        if not llm.vision_available():
            return None
        media = result.get("media")
        if not probe.is_media_file(media):
            return None
        tmp, frames = probe.extract_frames(media, count=self.FRAMES)
        try:
            if not frames:
                return None
            text = (f"分镜描述：{prompt}\n"
                    f"附 {len(frames)} 张按时间顺序抽取的真实画面帧，请据此判断是否符合分镜。")
            data = _parse_json(llm.chat_vision(self.SYSTEM, text, frames))
            if data is None:
                return None
            raw_fail = data.get("failures", [])
            fail = [f for f in raw_fail if isinstance(f, str) and f in DEFECT_FIXES][:4]
            score = data.get("score")
            ok = data.get("ok")
            # 判定口径：以模型**明确表态**的 ok 为准，score 只当参考——把模糊自评分当硬门槛，
            # 严模型会把真片也判死（实测 GLM-4V-Flash 对"柯基在雪地奔跑"给 0.1 分）。
            # 但说了 ok=false 却一个缺陷都不点名 = 不许当通过（那是最阴的假通过），
            # 落一个能驱动修正的键；老格式（只给 score）沿用低分兜底。
            try:
                low = float(score) < float(task.quality.get("min_score", 0.8))
            except (TypeError, ValueError):
                low = False
            if fail:
                pass
            elif ok is False:
                fail = ["semantic_mismatch"]
            elif ok is None and low:
                fail = ["semantic_mismatch"]
            result["vision_review"] = {
                "model": llm.vision_model(), "frames": len(frames),
                "failures": fail, "score": score,
                "reason": str(data.get("reason", ""))[:200],
            }
            detail = f"视觉评审（{len(frames)} 帧真画面）· 结论 {'符合' if ok else '不符' if ok is False else '未表态'}"
            detail += f" · 自评 {score}"
            if fail == ["semantic_mismatch"]:
                detail += "（未点名缺陷 → 记语义不符）"
            return self._report(fail, detail=detail)
        except Exception as exc:  # 视觉模型挂了不能拖垮闭环：退回文字路径
            result["vision_review"] = {"error": str(exc)[:160]}
            return None
        finally:
            probe.cleanup_frames(tmp)

    def _text_review(self, task: Task, result: dict, prompt: str) -> QualityReport:
        """旧行为：把 result 里的**文字**交给 LLM（没看图）。detail 里如实标注。"""
        defects = result.get("defects", [])
        semantic = [d for d in defects if d == "semantic_mismatch"]
        if not semantic and llm.llm_available():
            try:
                text = llm.chat([
                    {"role": "system", "content": self.SYSTEM_TEXT},
                    {"role": "user", "content": f"分镜: {prompt}\n结果说明: {result}"},
                ], temperature=0.0, max_tokens=200)
                data = _parse_json(text)
                if data is not None:
                    fail = [f for f in data.get("failures", [])
                            if isinstance(f, str) and f in DEFECT_FIXES][:4]
                    return self._report(
                        fail, detail=f"文字判断（未配视觉模型，没看图）· 自评 {data.get('score', '?')}")
            except Exception:
                pass  # LLM 不稳时用规则兜底，保证 Critic 永远可跑
        return self._report(semantic, detail="未做视觉检查（无视觉模型）")


class PipelineCritic:
    """依次跑三层并合并成一份报告。"""

    def __init__(self):
        self.layers: list[Critic] = [L1PhysicsCritic(), L2VisualCritic(), L3SemanticCritic()]
        self._cache_key: tuple | None = None
        self._cache: list[QualityReport] = []

    def evaluate_layers(self, task: Task, result: dict) -> list[QualityReport]:
        """逐层评估，返回每层独立报告（客户端/面板展示分层分数用）。

        **同一轮检测只跑一次**：闭环先调 `evaluate()` 拿判定、紧接着调 `evaluate_layers()`
        拿分层展示——两层各跑一遍会让视觉模型被请求两次（费用/延迟翻倍，且随机的
        VLM 可能给出与判定不一致的分层分数）。这里按 (任务, attempt, result 实例) 记住结果。
        """
        key = (task.task_id, task.retry_policy.get("attempts"), id(result))
        if key != self._cache_key:
            self._cache = [layer.evaluate(task, result) for layer in self.layers]
            self._cache_key = key
        return self._cache

    def evaluate(self, task: Task, result: dict) -> QualityReport:
        merged = QualityReport(layer="PIPELINE", score=1.0)
        for r in self.evaluate_layers(task, result):
            merged = merged.merge(r)
        return merged
