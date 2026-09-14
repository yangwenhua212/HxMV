"""Refiner：根据 QualityReport 的 failures/suggestions 调整任务后重投。

不自己瞎猜参数——优先查质量记忆（state.memory）里同类失败的历史成功修正；
没有历史才用 Critic 建议的默认修正。refine 永远派生新 Task（血缘保留），
并记录本次应用了哪些 (failure → suggestion) 对（fixes_applied），
这样任务最终 PASS 时 Controller 能回写"该修正被验证有效"——系统越用越懂。
"""
from __future__ import annotations

from .state import Task

# suggestion → 参数调整（v0.4 起这些调整对**真实渲染**同样成立：参数真改变量出来的指标）
_ADJUST = {
    # (目标位置, 参数名, 步进量 or None=直接设值, 封顶/目标值)
    "increase_reference_strength": ("constraints", "reference_strength", 0.2, 1.0),
    "reduce_motion_scale":         ("constraints", "motion_scale", -0.25, 0.2),
    "increase_motion_scale":       ("constraints", "motion_scale", 0.3, 1.5),
    "increase_resolution":         ("input", "resolution", None, "720p"),
    "increase_fps":                ("input", "fps", None, 30),
    "boost_audio_gain":            ("input", "audio_gain_db", None, 10.0),
    "enable_audio":                ("input", "with_audio", None, True),
    "trim_black_frames":           ("input", "trim_black", None, True),
    "extend_duration":             ("input", "duration", 1.0, 30.0),
    "trim_duration":               ("input", "duration", -1.0, 1.0),
    # 语义守卫：critic 点了名就补**那一条**（动作/情绪/衔接），没点名才用笼统的贴剧本
    "rewrite_prompt_closer":       ("input", "_guard_closer", None, True),
    "rewrite_prompt_action":       ("input", "_guard_action", None, True),
    "rewrite_prompt_emotion":      ("input", "_guard_emotion", None, True),
    "rewrite_prompt_continuity":   ("input", "_guard_continuity", None, True),
    # v0.8：blurdetect 判出的糊 / scdet 判出的"模型自己剪了片"，都得靠提示词锁
    # （分辨率与帧率已经分别由 low_clarity / fps_too_low 负责，别混）
    "rewrite_prompt_sharp":        ("input", "_guard_sharp", None, True),
    "rewrite_prompt_single_shot":  ("input", "_guard_single_shot", None, True),
}

_HUMAN_HINT = {
    "increase_reference_strength": "角色/场景一致性差 → 提高参考强度",
    "reduce_motion_scale": "运动模糊 → 降低运动幅度",
    "increase_motion_scale": "画面静止 → 提高运动幅度",
    "increase_resolution": "清晰度不足 → 升到 720p",
    "increase_fps": "帧率不足 → 提到 30fps",
    "boost_audio_gain": "音量低 → 增益 +10dB",
    "enable_audio": "没有音轨 → 让生成模型带音频",
    "trim_black_frames": "片头黑帧 → 去掉黑场",
    "extend_duration": "时长不足 → 补时长",
    "trim_duration": "超时长 → 裁时长",
    "rewrite_prompt_closer": "不符剧本（没点名）→ 加强语义贴合约束",
    "rewrite_prompt_action": "动作与分镜不符 → 提示词锁死动作",
    "rewrite_prompt_emotion": "情绪与分镜不符 → 提示词锁死情绪/氛围",
    "rewrite_prompt_continuity": "与前后镜不连续 → 提示词补衔接约束",
    "rewrite_prompt_sharp": "画面模糊 → 提示词锁清晰度（锐利对焦/细节清晰）",
    "rewrite_prompt_single_shot": "模型自己剪了镜头 → 提示词锁「一个连续镜头、无剪辑」",
}

# 哪些修正的效果**会**出现在实测值里（ffprobe/blackdetect/freezedetect/音量）。
# 只有这些才允许用"实测值一模一样"推断"修正没落到产物上"——提示词类（`rewrite_prompt_*`）、
# 参考强度、运动幅度改的是画面内容，物理实测值本来就不变，拿它判定会误杀重试。
# 实测踩过：L3 判语义不符 → 提示词修正 → 被收手守卫当"没落地"提前收手。
MEASURABLE_FIXES = {
    "increase_resolution": "resolution",
    "increase_fps": "fps",
    "boost_audio_gain": "audio",
    "enable_audio": "audio",
    "trim_black_frames": "black_seconds",
    "extend_duration": "duration",
    "trim_duration": "duration",
    "increase_motion_scale": "freeze_seconds",
}

# 一次最多应用几条修正。所有现有旋钮彼此正交（分辨率/帧率/音量/黑场/参考强度/运动/时长），
# 上限只是「防未来出现互相打架的旋钮」的护栏——**别拿它省钱**：一次修到位比来回烧预算好。
MAX_FIXES = 8

# 修正优先级：**先修「内容对不对」，再修「物理好不好」**。角色错了/不符合分镜的镜头，
# 再清晰也没用；而物理微调（升分辨率、加增益）成本低、可以下一轮再修。
# 数字越小越先修；表里没有的排最后。同优先级保持 Critic 给的原顺序（稳定排序）。
_FIX_PRIORITY = {
    "character_mismatch": 0,
    "character_inconsistency": 1,
    "scene_inconsistency": 1,
    "action_mismatch": 2,
    "emotion_wrong": 2,
    "plot_break": 2,
    "multi_shot": 2,      # 模型自己剪了片 = 内容不符，跟"动作/情绪不对"同档，优先于物理项
    "semantic_mismatch": 3,
    "low_clarity": 4,
    "blurry": 4,          # 与清晰度不足同档（都是"画面不够精致"）
    "fps_too_low": 5,
    "motion_blur": 5,
    "frozen_frame": 5,
    "no_audio": 6,
    "low_volume": 6,
    "silent_audio": 6,    # 与"没音轨/音量低"同档，但修正方向不同（见 DEFECT_FIXES）
    "black_frame": 7,
    "too_short": 8,
    "too_long": 8,
}
_FIX_PRIORITY_DEFAULT = 99


def _apply(task: Task, action: str) -> str:
    """执行参数调整，返回人话说明。"""
    spec = _ADJUST.get(action)
    if spec is None:
        return ""
    where, key, delta, cap = spec
    target = task.input if where == "input" else task.constraints
    old = target.get(key)
    if delta is None:
        target[key] = cap          # 直接设值
        return f"{action}: {key}={cap}（{_HUMAN_HINT.get(action,'')}）"
    old_num = float(old) if isinstance(old, (int, float)) else 0.3
    if delta > 0:
        new_num = min(cap, round(old_num + delta, 3))
    else:
        new_num = max(cap, round(old_num + delta, 3))  # cap 在此处是下限
    target[key] = new_num
    return f"{action}: {key} {old_num}→{new_num}（{_HUMAN_HINT.get(action,'')}）"


def _pair_failures_suggestions(report) -> list[tuple[str, str]]:
    """failures[i] 与 suggestions[i] 索引对齐（Critic 保证）。"""
    return list(zip(report.failures, report.suggestions))


class Refiner:
    def refine(self, task: Task, report, memory: dict | None = None) -> Task | None:
        """产出一个重试 Task；没有任何可修方向时返回 None（由 Controller 判终态 FAIL）。
        memory = 质量记忆 {"failure": [{"suggestion","success","score"}...]}，跨任务复用。"""
        pairs = _pair_failures_suggestions(report)
        if not pairs:
            return None
        # 先修内容（角色/分镜），再修物理（清晰度/音量/黑帧）；同优先级保持原顺序
        pairs.sort(key=lambda fs: _FIX_PRIORITY.get(fs[0], _FIX_PRIORITY_DEFAULT))

        # 历史成功修正优先于默认建议：找出记忆里被验证过的 (failure, suggestion)
        chosen: list[tuple[str, str]] = []
        for f, s in pairs:
            hint = self._memory_hint(f, memory or {})
            chosen.append((f, hint if hint else s))
        chosen = chosen[:MAX_FIXES]

        # 一条都调不动 = 这个失败已经修无可修（例如 reference_strength 已在 1.0）。
        # 返回 None 让 Controller 提前收手——实测不然会拿同一套参数烧满 30 次预算。
        if not chosen:
            return None

        new_task = task.child()
        notes, applied = [], []
        for f, s in chosen:
            action = s.split(":", 1)[0]
            note = _apply(new_task, action)
            if note:
                notes.append(note)
                applied.append({"failure": f, "suggestion": s.split(":", 1)[0]})
        if not notes:
            return None
        new_task.fixes_applied = task.fixes_applied + applied  # 血缘累计，PASS 时统一回写
        new_task.refine_history.append("；".join(notes))
        return new_task

    @staticmethod
    def _memory_hint(failure: str, memory: dict) -> str | None:
        """质量记忆查询：该失败历史上验证成功的 suggestion（取最近最高分）。"""
        best: tuple[str, float] | None = None
        for entry in memory.get(failure, []):
            if entry.get("success") and entry["score"] > (best[1] if best else -1):
                best = (entry["suggestion"], entry["score"])
        return best[0] if best else None
