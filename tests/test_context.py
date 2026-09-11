"""Tests for hxmv.core.context — ContextManager 动态压缩。"""
from hxmv.core.context import ContextManager
from hxmv.core.state import ExecutionState


def _state_with_observations(n, chinese=True):
    s = ExecutionState(goal="g")
    word = "中文观测记录条目内容，用于制造超过预算的上下文" if chinese else "plain ascii observation text"
    s.observations = [{"task_id": f"t{i}", "report": None, "note": word} for i in range(n)]
    return s


class TestEstimateTokens:
    def test_ascii_about_quarter(self):
        assert ContextManager._estimate_tokens([{"note": "x" * 40}]) == 10

    def test_chinese_counts_near_one(self):
        s = _state_with_observations(1)
        text = s.observations[0]["note"]
        assert ContextManager._estimate_tokens([{"note": text}]) == len(text)


class TestMaybeCompress:
    def test_below_budget_no_compress(self):
        cm = ContextManager(token_budget=4000)
        s = _state_with_observations(2)
        assert cm.maybe_compress(s) is False
        assert cm.compression_count == 0

    def test_above_budget_compresses(self):
        cm = ContextManager(token_budget=10, compress_ratio=0.5)
        s = _state_with_observations(20)
        assert cm.maybe_compress(s) is True
        assert cm.compression_count == 1
        # 保留了最近一半 + 1 条折叠摘要
        assert len(s.observations) == 10 + 1
        assert s.observations[0]["task_id"] == "sys"
        assert "_compressions" in s.memory
        assert len(s.memory["_compressions"]) == 1