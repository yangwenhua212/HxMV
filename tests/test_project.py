"""Tests for hxmv.core.project — 画面指纹 / 存读 / 复用 / 分集。"""
import pytest

from hxmv.core import project as pmod
from hxmv.core.project import Project, fingerprint


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(pmod, "PROJECTS_DIR", str(tmp_path))
    p = Project("short-film")
    return p


class TestFingerprint:
    def test_deterministic(self):
        params = {"prompt": "柯基打滚", "duration": 5, "resolution": "720p", "seed": 1}
        assert fingerprint(params) == fingerprint(params)

    def test_sensitive_to_input(self):
        assert fingerprint({"prompt": "A"}) != fingerprint({"prompt": "B"})


class TestPersistence:
    def test_save_load_roundtrip(self, proj):
        proj.style = "cinematic"
        proj.save()
        loaded = Project.load("short-film")
        assert loaded.style == "cinematic"

    def test_load_missing_returns_default(self):
        p = Project.load("does-not-exist")
        assert p.style == "cinematic"
        assert p.characters == {}


class TestAssetsAndShots:
    def test_register_asset_cross_kind_same_key(self, proj, tmp_path):
        f = tmp_path / "c.png"
        f.write_bytes(b"x")
        proj.register_asset("character", "hero", str(f))
        proj.register_asset("scene", "hero", str(f))  # 同名 key 只归一类
        assert "hero" in proj.scenes
        assert "hero" not in proj.characters

    def test_shot_reuse_and_prune(self, proj, tmp_path):
        f = tmp_path / "shot.mp4"
        f.write_bytes(b"x")
        proj.register_shot("fp123", str(f), result={"media": str(f)})
        assert proj.shot("fp123") is not None
        assert proj.shot("nope") is None
        # prune 掉指向已删除文件的项
        f.unlink()
        proj.prune()
        assert proj.shot("fp123") is None

    def test_best_for_returns_latest_match(self, proj, tmp_path):
        base = {"duration": 5, "seed": 1, "resolution": "720p",
                "task_constraints": {"character": "cat", "scene": "s01", "style": "cinematic"}}
        for i, prompt in enumerate(["P1", "P1", "P2"]):
            f = tmp_path / f"s{i}.mp4"
            f.write_bytes(b"x")
            proj.register_shot(f"fp{i}", str(f),
                               task_input={"prompt": prompt}, **base)
        hit = proj.best_for("P1", "cat", "s01", "cinematic")
        assert hit is not None
        # 同一剧情身份里返回 updated 最新的一条
        assert hit["task_input"]["prompt"] == "P1"
        # 剧情身份不匹配不命中
        assert proj.best_for("P3", "cat", "s01", "cinematic") is None


class TestEpisodes:
    def test_add_episode_dedup_same_n(self, proj):
        proj.add_episode("第1集", ["a.mp4"], episode=1)
        proj.add_episode("第1集重写", ["b.mp4"], episode=1)  # 同一集覆盖
        assert len(proj.episodes) == 1
        assert proj.episodes[0]["outputs"] == ["b.mp4"]

    def test_episodes_auto_increment(self, proj):
        proj.add_episode("第1集", ["a.mp4"])
        proj.add_episode("第2集", ["b.mp4"])
        assert [e["n"] for e in proj.episodes] == [1, 2]


class TestListAll:
    def test_list_returns_rows(self, proj, tmp_path, monkeypatch):
        monkeypatch.setattr(pmod, "PROJECTS_DIR", str(tmp_path))
        proj.save()
        rows = Project.list_all()
        assert len(rows) == 1
        assert rows[0]["id"] == "short-film"