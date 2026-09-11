"""Tests for hxmv.quality — QualityReport 与层级合并。"""
from hxmv.quality import QualityReport


def _r(score=1.0, failures=(), suggestions=(), detail=""):
    return QualityReport(layer="L1_PHYSICS", score=score,
                         failures=list(failures), suggestions=list(suggestions),
                         detail=detail)


class TestPassed:
    def test_passed_when_no_failures(self):
        assert _r().passed

    def test_failed_with_failures(self):
        assert not _r(failures=["black_frame"]).passed


class TestMerge:
    def test_takes_min_score(self):
        merged = _r(score=0.9).merge(_r(score=0.6))
        assert merged.score == 0.6
        assert merged.layer == "PIPELINE"

    def test_uniquifies_failures_keeping_order(self):
        m = _r(failures=["a", "b"]).merge(_r(failures=["b", "c"]))
        assert m.failures == ["a", "b", "c"]

    def test_uniquifies_suggestions(self):
        m = _r(failures=["a"], suggestions=["s1"]).merge(_r(failures=["b"], suggestions=["s1"]))
        assert m.suggestions == ["s1"]

    def test_detail_joined(self):
        m = _r(detail="x").merge(_r(detail="y"))
        assert m.detail == "x / y"


class TestStr:
    def test_pass_line(self):
        assert "通过" in str(_r())

    def test_fail_line_mentions_failures(self):
        assert "black_frame" in str(_r(score=0.8, failures=["black_frame"],
                                      suggestions=["trim_black_frames"]))