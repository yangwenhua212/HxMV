"""Tests for hxmv.media.probe —纯判据 + _fps 解析（无需 ffmpeg）。"""
import pytest

from hxmv.media import probe


class TestFps:
    @pytest.mark.parametrize("text,expected", [
        ("30000/1001", 30000 / 1001),
        ("15/1", 15.0),
        (" 50/2 ", 25.0),
        # ffprobe 对部分源直接报整数帧率——回归：旧实现只认 num/den，这里返回 None
        ("30", 30.0),
        ("15", 15.0),
        ("29.97", 29.97),
    ])
    def test_parses(self, text, expected):
        got = probe._fps(text)
        assert got is not None
        assert got == pytest.approx(expected, abs=1e-6)

    @pytest.mark.parametrize("bad", ["", "   ", None, "abc", "a/b", "15/0", "/15"])
    def test_invalid_returns_none(self, bad):
        assert probe._fps(bad) is None


def _metrics(**kw):
    m = {"width": 1280, "height": 720, "fps": 30.0, "has_audio": True,
         "mean_volume_db": -30.0, "black_seconds": 0.0, "freeze_seconds": 0.0,
         "duration": 5.0, "bitrate_kbps": 500.0}
    m.update(kw)
    return m


class TestDetectDefects:
    def test_clean(self):
        assert probe.detect_defects(_metrics()) == []

    def test_low_height_low_clarity(self):
        assert "low_clarity" in probe.detect_defects(_metrics(height=480))

    def test_low_fps(self):
        assert "fps_too_low" in probe.detect_defects(_metrics(fps=15.0))

    def test_no_audio_is_low_volume(self):
        assert "low_volume" in probe.detect_defects(_metrics(has_audio=False))

    def test_low_volume(self):
        assert "low_volume" in probe.detect_defects(_metrics(mean_volume_db=-45.0))

    def test_black_frame(self):
        assert "black_frame" in probe.detect_defects(_metrics(black_seconds=1.5))

    def test_frozen_frame(self):
        assert "frozen_frame" in probe.detect_defects(_metrics(freeze_seconds=2.0))

    def test_too_short(self):
        assert "too_short" in probe.detect_defects(_metrics(duration=2.0), expect_duration=5.0)

    def test_too_long(self):
        assert "too_long" in probe.detect_defects(_metrics(duration=8.0), expect_duration=5.0)

    def test_duration_ignore_without_expect(self):
        assert "too_short" not in probe.detect_defects(_metrics(duration=2.0))

    def test_bitrate_per_pixel_recorded(self):
        m = _metrics()
        probe.detect_defects(m)
        # 500kbps, 1280x720@30fps → 四舍五入到 4 位
        expected = round(500000 / (1280 * 720 * 30.0), 4)
        assert m["bitrate_per_pixel"] == expected