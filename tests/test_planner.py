"""Tests for hxmv.core.planner — MockPlanner 流水线与记忆起手。"""
from hxmv.core.planner import MockPlanner, _first_scene_prompt
from hxmv.core.state import (ACTION_COMPOSE, ACTION_GENERATE_CHARACTER,
                             ACTION_GENERATE_SCENE, ACTION_GENERATE_SHOT,
                             ACTION_STORYBOARD, ExecutionState)


class TestFirstScenePrompt:
    def test_splits_on_trailing_punct(self):
        # 取末尾 ≥4 字的无标点片段
        goal = "做一只小猫追蝴蝶。然后小黄鸭游过湖面"
        assert _first_scene_prompt(goal) == "然后小黄鸭游过湖面"

    def test_short_tail_returns_whole(self):
        goal = "做一只小猫追蝴蝶。喵"
        assert _first_scene_prompt(goal) == goal


class TestMockPlanner:
    def test_builds_full_pipeline(self):
        state = ExecutionState(goal="做一只柯基雪地打滚的短片")
        p = MockPlanner()
        actions = []
        while True:
            t = p.next_task(state)
            if t is None:
                break
            actions.append(t.action)
        assert actions == [ACTION_STORYBOARD, ACTION_GENERATE_CHARACTER,
                           ACTION_GENERATE_SCENE, ACTION_GENERATE_SHOT,
                           ACTION_GENERATE_SHOT, ACTION_COMPOSE]

    def test_queue_exhausts_to_none(self):
        p = MockPlanner()
        state = ExecutionState(goal="g")
        for _ in range(6):
            assert p.next_task(state) is not None
        assert p.next_task(state) is None


class TestLearntStrength:
    def test_base_without_brain(self):
        assert MockPlanner()._learnt_strength("character_inconsistency", 0.4) == 0.4

    def test_lesson_raises_strength(self, tmp_path):
        from hxmv.core.brain import Brain
        b = Brain(path=str(tmp_path / "b.json"))
        b.remember_lesson("character_inconsistency", "increase_reference_strength", 0.9)
        got = MockPlanner(brain=b)._learnt_strength("character_inconsistency", 0.4)
        assert got > 0.4

    def test_learnt_params(self, tmp_path):
        from hxmv.core.brain import Brain
        b = Brain(path=str(tmp_path / "b.json"))
        b.remember_lesson("低清晰度", "increase_resolution", 0.9)
        inp, notes = MockPlanner(brain=b)._learnt_params()
        assert inp["resolution"] == "720p"
        assert "720p" in notes