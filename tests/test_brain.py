"""Tests for hxmv.core.brain — 相似度 / 记忆写入 / 召回 / 注入 / 修剪 / 遗忘。"""
import pytest

from hxmv.core.brain import Brain, MAX_ENTRIES, similarity


def _brain(tmp_path):
    return Brain(path=str(tmp_path / "brain.json"))


class TestSimilarity:
    def test_identical_is_one(self):
        assert similarity("一只小猫追蝴蝶", "一只小猫追蝴蝶") == 1.0

    def test_disjoint_is_zero(self):
        assert similarity("abcde", "vwxyz") == 0.0

    def test_empty_is_zero(self):
        assert similarity("", "abc") == 0.0
        assert similarity("abc", "") == 0.0

    def test_partial_between(self):
        s = similarity("红色柯基在雪地打滚", "柯基在雪地打滚")
        assert 0.0 < s < 1.0


class TestRemember:
    def test_new_entry(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("模型爱加片头黑场", kind="FACT")
        assert b.size == 1
        assert b.entries[0].kind == "FACT"

    def test_duplicate_bumps_importance(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("低音量", importance=0.5)
        b.remember("低音量", importance=0.5)
        e = b.entries[0]
        assert e.importance == pytest.approx(0.55)
        assert e.hit_count == 1

    def test_hits_3_sublimation(self, tmp_path):
        b = _brain(tmp_path)
        for _ in range(4):  # 初次创建 hit=0，再 3 次同内容 → hit_count>=3
            b.remember("三连客")
        assert b.entries[0].hits_3 is True


class TestRememberLesson:
    def test_new_lesson(self, tmp_path):
        b = _brain(tmp_path)
        b.remember_lesson("低清晰度", "increase_resolution", 0.9, context="c")
        e = b.entries[0]
        assert e.kind == "LESSON"
        assert e.meta["failure"] == "低清晰度"
        assert e.meta["times"] == 1
        assert "increase_resolution" in e.content

    def test_same_source_stacks_times(self, tmp_path):
        b = _brain(tmp_path)
        b.remember_lesson("f", "s", 0.8)
        b.remember_lesson("f", "s", 0.95)
        e = b.entries[0]
        assert e.meta["times"] == 2
        assert e.meta["score"] == pytest.approx(0.95)  # 取最高分
        assert e.importance == pytest.approx(0.55 + 0.12)


class TestRecallAndInject:
    def test_recall_kinds_filter(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("因子A", kind="FACT")
        b.remember_lesson("失败X", "修正Y", 0.9)
        lessons = b.recall("", kinds=("LESSON",))
        assert all(e.kind == "LESSON" for e in lessons)

    def test_recall_top_k(self, tmp_path):
        b = _brain(tmp_path)
        for i in range(10):
            b.remember(f"记忆条目编号{i}", importance=0.5 + i / 100)
        assert len(b.recall("记忆条目", top_k=3)) == 3

    def test_inject_empty_brain(self, tmp_path):
        assert _brain(tmp_path).inject("q") == ""

    def test_inject_respects_budget(self, tmp_path):
        b = _brain(tmp_path)
        for i in range(10):
            b.remember("很长的一段记忆内容用来测试字符预算对注入长度的限制边界情况" + str(i))
        block = b.inject("测试", char_budget=60)
        assert block != ""
        assert len(block) <= 60 * 1.5  # 预算内（允许轻微超一条）
        assert "【HxMV 记忆" in block


class TestTrimAndDecay:
    def test_trim_caps_size(self, tmp_path):
        b = _brain(tmp_path)
        for i in range(MAX_ENTRIES + 50):
            b.remember(f"条目{i}", importance=0.1)
        assert b.size <= MAX_ENTRIES

    def test_decay_reduces_stale_importance(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("古早经验", importance=0.8)
        e = b.entries[0]
        e.last_hit = 1.0  # 很久以前
        b._decay_all()
        assert e.importance < 0.8

    def test_decay_floor(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("陈旧", importance=0.9)
        e = b.entries[0]
        e.last_hit = 1.0
        for _ in range(20):
            b._decay_all()
        assert e.importance >= 0.1


class TestPersistence:
    def test_save_load_roundtrip(self, tmp_path):
        b = _brain(tmp_path)
        b.remember("持久化测试条目", kind="FACT")
        b.remember_lesson("f", "s", 0.8)
        b2 = Brain(path=str(tmp_path / "brain.json"))
        assert b2.size == 2
        contents = {e.content for e in b2.entries}
        assert "持久化测试条目" in contents

    def test_load_corrupt_is_empty(self, tmp_path):
        p = tmp_path / "brain.json"
        p.write_text("{not json", encoding="utf-8")
        assert Brain(path=str(p)).size == 0