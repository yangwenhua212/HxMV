"""Tests for hxmv.core.controller — 判定 PASS / RETRY / FAIL 与记忆回写。"""

from hxmv.core.brain import Brain
from hxmv.core.controller import Controller
from hxmv.core.critic import PipelineCritic
from hxmv.core.state import (ACTION_GENERATE_SHOT, ExecutionState, Task,
                             TaskStatus)


def _state():
    return ExecutionState(goal="g")


def _shot_task(max_attempts=4, attempts=0, fixes=None):
    t = Task(ACTION_GENERATE_SHOT, input={"prompt": "x", "duration": 5})
    t.retry_policy.update({"max_attempts": max_attempts, "attempts": attempts})
    t.fixes_applied = fixes or []
    return t


def _pass_report():
    c = PipelineCritic()
    return c.evaluate(_shot_task(), {"defects": [], "media": "mock_video.mp4"})


def _fail_report():
    c = PipelineCritic()
    return c.evaluate(_shot_task(), {"defects": ["low_clarity"], "media": "mock_video.mp4"})


class TestPass:
    def test_pass_archives_and_budgets(self):
        st = _state()
        c = Controller()
        task = _shot_task(fixes=[{"failure": "low_volume", "suggestion": "boost_audio_gain"}])
        out = c.update(st, task, {"defects": [], "cost_units": 3.0}, _pass_report())
        assert out == "PASS"
        assert task.status == TaskStatus.PASS
        assert task in st.completed
        assert st.budget.attempts == 1
        assert st.budget.used == 3.0

    def test_pass_writes_quality_memory(self):
        st = _state()
        Controller().update(st, _shot_task(fixes=[{"failure": "low_volume",
                                                    "suggestion": "boost_audio_gain"}]),
                            {"cost_units": 0}, _pass_report())
        mem = st.memory["quality"]["low_volume"]
        assert mem[0]["success"] is True

    def test_pass_writes_to_brain(self, tmp_path):
        brain = Brain(path=str(tmp_path / "b.json"))
        st = _state()
        Controller(brain=brain).update(
            st, _shot_task(fixes=[{"failure": "low_volume", "suggestion": "boost_audio_gain"}]),
            {"cost_units": 0}, _pass_report())
        lessons = [e for e in brain.entries if e.kind == "LESSON"]
        assert len(lessons) == 1
        assert lessons[0].meta["failure"] == "low_volume"


class TestRetry:
    def test_retry_enqueues(self):
        st = _state()
        c = Controller()
        task = _shot_task()
        out = c.update(st, task, {"defects": ["low_clarity"], "cost_units": 3.0}, _fail_report())
        assert out == "RETRY"
        assert len(st.retry_queue) == 1
        assert task.status == TaskStatus.PENDING
        assert task not in st.completed


class TestFail:
    def test_fail_when_attempts_exhausted(self):
        st = _state()
        c = Controller()
        task = _shot_task(attempts=4)  # = max_attempts
        out = c.update(st, task, {"defects": ["low_clarity"], "cost_units": 0}, _fail_report())
        assert out == "FAIL"
        assert task.status == TaskStatus.FAIL
        assert task in st.failed