"""ContextManager：动态上下文管理。

原则：不是"每 N 个镜头固定压缩一次"，而是上下文"值得压缩"时才压缩——
按观测历史的估算 token 是否超阈值决定。V0.1 用字符数/4 粗估；
接 LLM 后可让 LLM 做语义摘要，未接则做丢弃式压缩（保留最近、折叠最旧）。
"""
from __future__ import annotations

from ..core.state import ExecutionState


class ContextManager:
    def __init__(self, token_budget: int = 4000, compress_ratio: float = 0.5):
        self.token_budget = token_budget
        self.compress_ratio = compress_ratio
        self.compression_count = 0

    @staticmethod
    def _estimate_tokens(obs: list[dict]) -> int:
        """粗略估算：中文约 1 字 ≈ 1 token，其余按 4 字符/token。"""
        total = 0
        for o in obs:
            note = str(o.get("note", ""))
            total += len(note) // 1 if any("\u4e00" <= c <= "\u9fff" for c in note) else len(note) // 4
        return total

    def maybe_compress(self, state: ExecutionState) -> bool:
        """超阈值则压缩 observations，返回是否发生了压缩。"""
        if self._estimate_tokens(state.observations) <= self.token_budget:
            return False
        obs = state.observations
        keep = max(1, int(len(obs) * self.compress_ratio))
        newest, oldest = obs[-keep:], obs[:-keep]
        summary_note = (
            f"[上下文压缩 #{self.compression_count + 1}] "
            f"折叠 {len(oldest)} 条历史观测（含失败 {sum(1 for o in oldest if o.get('report') and not o['report'].passed)} 条）"
        )
        state.observations = [{"task_id": "sys", "report": None, "note": summary_note}] + newest
        state.memory.setdefault("_compressions", []).append(summary_note)
        self.compression_count += 1
        return True
