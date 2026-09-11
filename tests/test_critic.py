"""Tests for hxmv.core.critic — 三层 Critic（注入缺陷路径，无需真媒体/ffmpeg）。"""
from hxmv.core import critic as cmod
from hxmv.core.critic import (L1PhysicsCritic, L2VisualCritic, L3SemanticCritic,
                              PipelineCritic)
from hxmv.core.state import (ACTION_GENERATE_CHARACTER, ACTION_GENERATE_SHOT,
                             Task)


def _task(action=ACTION_GENERATE_SHOT, constraints=None):
    return Task(action, input={"prompt": "x", "duration": 5},
                constraints=constraints or {})


def _result(defects=(), **kw):
    r = {"defects": list(defects), "media": "mock_video.mp4"}  # 非真文件 → 走注入路径
    r.update(kw)
    return r


class TestReportScoring:
    def test_report_no_defects_score_one(self):
        rep = cmod.Critic._report(L1PhysicsCritic(), [], detail="")
        assert rep.score == 1.0 and rep.passed

    def test_single_defect_deducts(self):
        rep = cmod.Critic._report(L1PhysicsCritic(), ["black_frame"])
        assert rep.score == pytest_rAlmost(1.0 - 0.15)

    def test_multi_defect_capped_by_layer_weight(self):
        rep = cmod.Critic._report(L1PhysicsCritic(), ["a", "b", "c", "d", "e"])
        assert rep.score == pytest_rAlmost(1.0 - 0.35)  # 封顶 L1 权重 0.35

    def test_suggestion_index_alignment(self):
        rep = cmod.Critic._report(L1PhysicsCritic(), ["black_frame", "unknown_thing"])
        assert rep.failures == ["black_frame", "unknown_thing"]
        assert rep.suggestions == ["trim_black_frames", "review_unknown_thing"]

    def test_unknown_defect_gets_review_suggestion(self):
        rep = cmod.Critic._report(L2VisualCritic(), ["character_inconsistency"])
        assert rep.suggestions == ["increase_reference_strength"]


class TestL1:
    def test_ignores_non_media_actions(self):
        rep = L1PhysicsCritic().evaluate(_task(ACTION_GENERATE_CHARACTER), _result())
        assert rep.passed and rep.failures == []

    def test_injected_defects(self):
        rep = L1PhysicsCritic().evaluate(_task(), _result(["black_frame", "low_volume"]))
        assert rep.failures == ["black_frame", "low_volume"]
        assert rep.suggestions == ["trim_black_frames", "boost_audio_gain"]


class TestL2:
    def test_injected_character_inconsistency(self):
        rep = L2VisualCritic().evaluate(
            _task(constraints={"character": "cat"}),
            _result(["character_inconsistency"]))
        assert rep.failures == ["character_inconsistency"]

    def test_no_injected_consistency_ok(self):
        rep = L2VisualCritic().evaluate(_task(), _result([]))
        assert rep.passed


class TestL3:
    def test_injected_semantic(self):
        rep = L3SemanticCritic().evaluate(_task(), _result(["semantic_mismatch"]))
        assert "semantic_mismatch" in rep.failures


class TestPipeline:
    def test_merges_and_caches_layers(self):
        # 回归：evaluate 只跑一次各层，结果缓存在 report.layers 供循环复用
        calls = {"n": 0}
        class Spy(PipelineCritic):
            def evaluate_layers(self, task, result):
                calls["n"] += 1
                return super().evaluate_layers(task, result)
        p = Spy()
        rep = p.evaluate(_task(), _result(["black_frame"]))
        assert calls["n"] == 1
        assert len(rep.layers) == 3
        assert [l.layer for l in rep.layers] == ["L1_PHYSICS", "L2_VISUAL", "L3_SEMANTIC"]

    def test_merge_takes_min_score(self):
        p = PipelineCritic()
        # L1 注入黑帧 → 0.85；L2/L3 干净 → 1.0；合并取 0.85
        rep = p.evaluate(_task(), _result(["black_frame"]))
        assert rep.score == pytest_rAlmost(0.85)
        assert "black_frame" in rep.failures


def pytest_rAlmost(v):
    return round(v, 10)