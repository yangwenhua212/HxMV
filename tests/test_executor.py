"""Tests for hxmv.core.executor — 确定性 mock 执行器 / 参数耦合 / 指纹复用。"""
from hxmv.core import project as pmod
from hxmv.core.executor import COST_PER_ACTION, MockVideoExecutor
from hxmv.core.project import Project
from hxmv.core.state import (ACTION_COMPOSE, ACTION_GENERATE_SCENE,
                             ACTION_GENERATE_SHOT, ACTION_STORYBOARD, Task)


def _shot(seed=101, resolution=None, trim_black=None,
          constraints=None, prompt="柯基打滚"):
    task = Task(ACTION_GENERATE_SHOT,
                input={"prompt": prompt, "duration": 5, "seed": seed},
                constraints=constraints or {})
    if resolution is not None:
        task.input["resolution"] = resolution
    if trim_black is not None:
        task.input["trim_black"] = trim_black
    return task


class TestDeterminism:
    def test_same_task_same_defects(self):
        ex = MockVideoExecutor()
        t = _shot()
        r1, r2 = ex.execute(t), ex.execute(t)
        assert r1["defects"] == r2["defects"]

    def test_storyboard_cost(self):
        ex = MockVideoExecutor()
        t = Task(ACTION_STORYBOARD, input={"goal": "g"})
        assert ex.execute(t)["cost_units"] == COST_PER_ACTION[ACTION_STORYBOARD]


class TestParameterCoupling:
    def test_720p_never_low_clarity(self):
        # p_clarity=0.0 when height>=720，与 rng 无关 → 断言恒不注入
        for seed in range(5):
            defs = MockVideoExecutor().execute(_shot(seed=seed, resolution="720p"))["defects"]
            assert "low_clarity" not in defs

    def test_trim_black_never_black_frame(self):
        for seed in range(5):
            defs = MockVideoExecutor().execute(
                _shot(seed=seed, resolution="720p", trim_black=True))["defects"]
            assert "black_frame" not in defs

    def test_scene_shot_reuses_saved_fingerprint(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pmod, "PROJECTS_DIR", str(tmp_path))
        proj = Project("film")
        ex = MockVideoExecutor(project=proj)
        t = _shot(seed=42)
        fp = ex._fp(t)
        # 预登记一个真实存在的镜头文件（mock 的 media 是占位串，未落盘不会命中）
        rfile = tmp_path / "shot.mp4"
        rfile.write_bytes(b"x")
        proj.register_shot(fp, str(rfile),
                           result={"media": str(rfile), "defects": [], "duration": 5})
        out = ex.execute(t)
        assert out["reused"] is True
        assert out["cost_units"] == 0.0
        assert out["media"] == str(rfile)


class TestComposeCost:
    def test_compose_cost_units(self):
        t = Task(ACTION_COMPOSE, input={"output": "final.mp4"})
        assert MockVideoExecutor().execute(t)["cost_units"] == COST_PER_ACTION[ACTION_COMPOSE]

    def test_generate_scene_asset(self):
        t = Task(ACTION_GENERATE_SCENE, constraints={"scene_key": "s01"})
        r = MockVideoExecutor().execute(t)
        assert r["asset"] == "s01"