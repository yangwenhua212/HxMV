"""Refiner：根据 QualityReport 的 failures/suggestions 调整任务后重投。

不自己瞎猜参数——优先查质量记忆（state.memory）里同类失败的历史成功修正；
没有历史才用 Critic 建议的默认修正。refine 永远派生新 Task（血缘保留），
并记录本次应用了哪些 (failure → suggestion) 对（fixes_applied），
这样任务最终 PASS 时 Controller 能回写"该修正被验证有效"——系统越用越懂。
"""
from __future__ import annotations

from .state import Task

# suggestion → 参数调整（V0.1 的调参表；接真实生成器后按工具能力扩展）
_ADJUST = {
    # (目标位置, 参数名, 步进量 or None=直接设值, 封顶/目标值)
    "increase_reference_strength": ("constraints", "reference_strength", 0.2, 1.0),
    "reduce_motion_scale":         ("constraints", "motion_scale", -0.25, 0.2),
    "increase_resolution":         ("input", "resolution", None, "1080p"),
    "increase_fps":                ("input", "fps", None, 30),
    "boost_audio_gain":            ("input", "audio_gain_db", None, 6.0),
    "rewrite_prompt_closer":       ("input", "_semantic_guard", None, True),
}

_HUMAN_HINT = {
    "increase_reference_strength": "角色/场景一致性差 → 提高参考强度",
    "reduce_motion_scale": "运动模糊 → 降低运动幅度",
    "increase_resolution": "清晰度不足 → 升分辨率",
    "increase_fps": "帧率不足 → 提到 30fps",
    "boost_audio_gain": "音量低 → 增益 +6dB",
    "rewrite_prompt_closer": "不符剧本 → 加强语义贴合约束",
}


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

        # 历史成功修正优先于默认建议：找出记忆里被验证过的 (failure, suggestion)
        chosen: list[tuple[str, str]] = []
        for f, s in pairs:
            hint = self._memory_hint(f, memory or {})
            chosen.append((f, hint if hint else s))
        chosen = chosen[:2]  # 一次最多应用两条修正，避免参数打架

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
