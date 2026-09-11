"""Tests for hxmv.providers — Provider 抽象 / FakeApi / Kling 示例。"""
import pytest

from hxmv.core.state import Task
from hxmv.providers.base import ProviderError, VideoProvider
from hxmv.providers.fake_api import FakeApiProvider
from hxmv.providers.kling_example import KlingStyleProvider


class TestProviderError:
    def test_defaults(self):
        e = ProviderError("boom")
        assert e.retryable is True
        assert e.cost_units == 0.0

    def test_custom(self):
        e = ProviderError("denied", retryable=False, cost_units=2.0)
        assert e.retryable is False
        assert e.cost_units == 2.0


class _ConcreteProvider(VideoProvider):
    action_map = {"GENERATE_SHOT": "x"}

    def generate(self, task):
        return {"cost_units": 0.0}


class TestVideoProviderBase:
    def test_supports_all_without_map(self):
        p = _ConcreteProvider()
        p.action_map = {}
        assert p.supports("anything")

    def test_supports_membership(self):
        p = _ConcreteProvider()
        assert p.supports("GENERATE_SHOT")
        assert not p.supports("COMPOSE")

    def test_default_estimate_cost(self):
        assert _ConcreteProvider().estimate_cost("x") == 1.0


class TestFakeApi:
    def test_returns_cost_for_supported_and_zero_else(self):
        p = FakeApiProvider(seed=1)
        assert p.estimate_cost("GENERATE_SHOT") == 3.0
        out = p.generate(Task("STORYBOARD", input={"goal": "g"}))
        assert out["cost_units"] == 0.0

    def test_shot_has_defects_list_and_cost(self):
        p = FakeApiProvider(seed=1)
        t = Task("GENERATE_SHOT", input={"prompt": "x", "duration": 5, "cfg_scale": 4.0},
                 constraints={"character": "cat", "scene": "s01"})
        out = p.generate(t)
        assert isinstance(out["defects"], list)
        assert out["cost_units"] == 3.0


class TestKling:
    def test_raises_without_key(self, monkeypatch):
        monkeypatch.delenv("HXMV_KLING_KEY", raising=False)
        with pytest.raises(ProviderError):
            KlingStyleProvider()