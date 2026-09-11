"""Tests for hxmv.core.refiner — 参数调整 / 修辞派生 / 记忆提示。"""
from hxmv.core.refiner import Refiner, _apply
from hxmv.core.state import Task


class TestApply:
    def test_direct_set(self):
        t = Task("GENERATE_SHOT", input={}, constraints={})
        note = _apply(t, "increase_resolution")
        assert t.input["resolution"] == "720p"
        assert "increase_resolution" in note

    def test_delta_upper_cap(self):
        t = Task("GENERATE_SHOT", constraints={"reference_strength": 0.9})
        _apply(t, "increase_reference_strength")
        assert t.constraints["reference_strength"] == 1.0  # 封顶不越界

    def test_delta_lower_cap(self):
        t = Task("GENERATE_SHOT", constraints={"motion_scale": 0.1})
        _apply(t, "reduce_motion_scale")
        assert t.constraints["motion_scale"] == 0.2  # 下限 0.2

    def test_unknown_action_noop(self):
        t = Task("GENERATE_SHOT")
        assert _apply(t, "no_such_action") == ""


class TestRefine:
    def _report(self, failures, suggestions):
        class R:
            pass
        r = R()
        r.failures = list(failures)
        r.suggestions = list(suggestions)
        return r

    def test_applies_fixes_and_history(self):
        r = Refiner()
        rep = self._report(["low_clarity", "low_volume"],
                           ["increase_resolution", "boost_audio_gain"])
        nt = r.refine(Task("GENERATE_SHOT"), rep)
        assert nt is not None
        assert nt.input["resolution"] == "720p"
        assert nt.input["audio_gain_db"] == 10.0
        assert len(nt.refine_history) == 1
        assert len(nt.fixes_applied) == 2
        assert nt.retry_policy["attempts"] == 1

    def test_no_failures_returns_none(self):
        assert Refiner().refine(Task("GENERATE_SHOT"), self._report([], [])) is None

    def test_unknown_defect_gets_review_suggestion_and_applies(self):
        r = Refiner()
        rep = self._report(["content_policy"], ["review_content_policy"])
        nt = r.refine(Task("GENERATE_SHOT"), rep)
        assert nt is None  # review_* 无对应参数调整 → 无可修方向

    def test_memory_hint_wins_over_default(self):
        r = Refiner()
        memory = {"low_volume": [
            {"suggestion": "boost_audio_gain", "success": True, "score": 0.9},
            {"suggestion": "reduce_motion_scale", "success": True, "score": 0.99},
        ]}
        rep = self._report(["low_volume"], ["boost_audio_gain"])
        nt = r.refine(Task("GENERATE_SHOT"), rep, memory)
        # 记忆里最高分成功修正方向是 reduce_motion_scale → 优先用之
        assert nt.constraints["motion_scale"] < 0.4  # 被 reduce 过

    def test_memory_hint_selects_highest_score(self):
        memory = {"f": [
            {"suggestion": "a", "success": False, "score": 0.9},   # 失败的不算
            {"suggestion": "b", "success": True, "score": 0.8},
            {"suggestion": "c", "success": True, "score": 0.7},
        ]}
        assert Refiner._memory_hint("f", memory) == "b"