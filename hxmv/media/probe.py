"""真眼睛：用系统 FFmpeg/ffprobe 从**真实媒体文件**里量出质量指标。

为什么必须有这一层（v0.4 的核心升级）：

- mock 世界的缺陷是 executor 按概率\"贴标签\"的，Critic 读标签——那不叫观察，叫复述。
- 真实生成服务不会自带质标签：清晰度/帧率/音量/黑帧/一致性只能从像素和音轨里量出来。
- 量出来的指标还能**回喂给 Refiner**：同一份 metrics 既是判据也是证据，闭环才不是自说自话。

设计约束：

- 零 Python 第三方依赖：调系统 `ffmpeg`/`ffprobe`，解析它们的输出。
  ffmpeg 属**可选运行时依赖**：没装 → `has_ffmpeg()=False`，L1/L2 自动回落读注入标签，
  闭环照跑（可跑性是底线），只是\"眼睛\"变回 mock。
- 所有阈值集中在 `THRESHOLDS`：调松紧只改一张表，别散落在各处。
- 一律用 `-hide_banner` 且不吞 stderr——ffmpeg 的测量结果就打在 stderr 上。
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess

# ---------- 判据阈值（真实世界的经验值；按需调这一张表） ----------
THRESHOLDS = {
    "min_height": 720,            # 输出高度低于此 → low_clarity（清晰度不足）
    "min_fps": 24.0,             # 帧率低于此 → fps_too_low
    "min_mean_volume_db": -40.0,  # 平均音量低于此 → low_volume（音轨偏轻）
    "black_seconds": 0.10,       # 累计黑屏时长超过此 → black_frame
    "freeze_seconds": 0.80,      # 累计静止时长超过此 → frozen_frame
    "min_consistency": 0.90,     # 与参考图外观一致度低于此 → character/scene_inconsistency
    "duration_ratio_low": 0.75,  # 实际/期望时长比值低于此 → too_short
    "duration_ratio_high": 1.30,  # 高于此 → too_long
    "min_bitrate_per_pixel": 0.02,  # 只作参考指标记录，不参与判缺陷（见 detect_defects 注释）
}

# 缺陷键必须与 core/critic.py 的 DEFECT_FIXES 对齐（那里定义修正方向）
DEFECT_KEYS = ("black_frame", "low_clarity", "fps_too_low", "low_volume",
               "frozen_frame", "too_short", "too_long")

_FFMPEG = None


def ffmpeg_path(name: str = "ffmpeg") -> str | None:
    """系统里的 ffmpeg/ffprobe 路径（找不到返回 None）。"""
    return shutil.which(name)


def has_ffmpeg() -> bool:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = bool(ffmpeg_path("ffmpeg") and ffmpeg_path("ffprobe"))
    return _FFMPEG


def _run(args: list[str], timeout: int = 120) -> tuple[int, str]:
    """跑一条命令，合并 stdout/stderr 返回（ffmpeg 的测量结果在 stderr）。"""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    out = (p.stdout or b"").decode("utf-8", "ignore") + (p.stderr or b"").decode("utf-8", "ignore")
    return p.returncode, out


def is_media_file(path: str | None) -> bool:
    """result 里的 media/output 是不是**真文件**（mock 世界给的是占位字符串，不落盘）。"""
    return bool(path) and os.path.isfile(str(path))


def _fps(text: str) -> float | None:
    """'30000/1001' → 29.97；'15/1' → 15.0；'30' → 30.0。

    ffprobe 对多数 muxer 报 ``num/den``，但部分源直接给整数（'30'）——
    旧实现只认 num/den，遇到整数帧率返回 None，导致 fps 全链路失效
    （detect_defects 判 fps_too_low 与 COMPOSE 取 fps 都会错误回落默认值）。
    """
    text = (text or "").strip()
    if not text:
        return None
    if "/" in text:
        try:
            num, den = (float(x) for x in text.split("/", 1))
        except ValueError:
            return None
        return num / den if den else None
    try:
        return float(text)
    except ValueError:
        return None


# ---------- 单指标探针 ----------

def probe_container(path: str) -> dict | None:
    """ffprobe 拿容器/流的基本事实：时长、分辨率、帧率、码率、有无音轨。"""
    if not has_ffmpeg() or not is_media_file(path):
        return None
    rc, out = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,width,height,avg_frame_rate,r_frame_rate:format=duration,bit_rate",
        "-of", "json", path,
    ])
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    info = {"width": None, "height": None, "fps": None, "has_audio": False}
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and info["width"] is None:
            info["width"] = int(st.get("width") or 0) or None
            info["height"] = int(st.get("height") or 0) or None
            info["fps"] = _fps(st.get("avg_frame_rate", "")) or _fps(st.get("r_frame_rate", ""))
        elif st.get("codec_type") == "audio":
            info["has_audio"] = True
    fmt = data.get("format", {})
    try:
        info["duration"] = float(fmt.get("duration") or 0.0) or None
    except (TypeError, ValueError):
        info["duration"] = None
    try:
        info["bitrate_kbps"] = round(float(fmt.get("bit_rate") or 0) / 1000, 1) or None
    except (TypeError, ValueError):
        info["bitrate_kbps"] = None
    return info


def measure_loudness(path: str) -> float | None:
    """平均音量（dBFS）。音轨太轻是真实世界里最常见的\"废片\"原因之一。"""
    if not has_ffmpeg() or not is_media_file(path):
        return None
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", out)
    return float(m.group(1)) if m else None


def measure_black_seconds(path: str) -> float | None:
    """累计黑屏时长（秒）——片头/片尾黑帧、生成器抽风全黑都能抓到。"""
    if not has_ffmpeg() or not is_media_file(path):
        return None
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path,
                    "-vf", f"blackdetect=d={THRESHOLDS['black_seconds']}:pix_th=0.10",
                    "-an", "-f", "null", "-"])
    total = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", out))
    return round(total, 3)


def measure_freeze_seconds(path: str) -> float | None:
    """累计静止时长（秒）——画面卡住/无运动，视频\"看着像坏图\"。"""
    if not has_ffmpeg() or not is_media_file(path):
        return None
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path,
                    "-vf", "freezedetect=n=0.002:d=1.2", "-an", "-f", "null", "-"])
    total = 0.0
    last = None
    for line in out.splitlines():
        m = re.search(r"freeze_(start|end):\s*([\d.]+)", line)
        if not m:
            continue
        if m.group(1) == "start":
            last = float(m.group(2))
        elif last is not None:
            total += max(0.0, float(m.group(2)) - last)
            last = None
    return round(total, 3)


def measure_black_freeze_loudness(path: str,
                                  duration: float | None = None
                                  ) -> tuple[float | None, float | None, float | None]:
    """**一次解码**同时量出：黑屏时长 / 静止时长 / 平均音量。

    三次探针各解一遍视频是纯浪费（1080p 上很贵）——三个都是元数据类 filter，
    合成一条链跑一遍即可。返回 (black_seconds, freeze_seconds, mean_volume_db)。

    n=0.002 是静止检测的噪声容差，按实测标定：真静止（帧完全重复，差值≈0）必抓，
    慢速推镜（差值 ~0.0015）不误报；调大就会漏掉真静止。
    真静止（帧完全重复）差值为 0，任何容差都抓得到。
    duration 用于收尾：片段\"一直静止到结尾\"时 freezedetect 只报 freeze_start，
    没有 freeze_end——不补上就会漏掉这类（最常见的）静止缺陷。
    """
    if not has_ffmpeg() or not is_media_file(path):
        return None, None, None
    vf = (f"blackdetect=d={THRESHOLDS['black_seconds']}:pix_th=0.10,"
          f"freezedetect=n=0.002:d=1.2")
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path, "-vf", vf,
                    "-af", "volumedetect", "-f", "null", "-"])

    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", out))
    if re.search(r"black_start:([\d.]+)\s*$", out, flags=re.M) and duration:
        last_black = float(re.findall(r"black_start:([\d.]+)", out)[-1])
        if last_black and "black_end" not in out.split(f"black_start:{last_black}")[-1]:
            black += max(0.0, duration - last_black)

    freeze, last = 0.0, None
    for line in out.splitlines():
        m = re.search(r"freeze_(start|end):\s*([\d.]+)", line)
        if not m:
            continue
        if m.group(1) == "start":
            last = float(m.group(2))
        elif last is not None:
            freeze += max(0.0, float(m.group(2)) - last)
            last = None
    if last is not None and duration:      # 静止一直到片尾
        freeze += max(0.0, duration - last)

    mv = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", out)
    return round(black, 3), round(freeze, 3), (float(mv.group(1)) if mv else None)


def leading_black_seconds(path: str) -> float:
    """片头黑场时长（秒）——第一个黑段从 0 开始才算。

    COMPOSE 层的\"去掉黑场\"修正要真的可用：拼接时按这个值把片头黑场裁掉。
    """
    if not has_ffmpeg() or not is_media_file(path):
        return 0.0
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path,
                    "-vf", f"blackdetect=d={THRESHOLDS['black_seconds']}:pix_th=0.10",
                    "-an", "-f", "null", "-"])
    for start, end in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", out):
        if float(start) <= 0.05:
            return round(float(end) - float(start), 3)
    return 0.0


def _thumbnail_bytes(path: str, n: int = 16, at: float | None = None,
                     yuv_first: bool = False) -> bytes:
    """把(某个时间点的)画面压成 n×n 的原始 RGB 缩略图——零依赖的\"感知指纹\"。"""
    vf = f"scale={n}:{n}"
    if yuv_first:  # 参考图走和视频同一条 yuv 色彩通路，才能公平比对
        vf += ",format=yuv420p,format=rgb24"
    args = ["ffmpeg", "-v", "error"]
    if at is not None:
        args += ["-ss", f"{at:.3f}"]
    args += ["-i", path, "-frames:v", "1", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    p = subprocess.run(args, capture_output=True)
    return p.stdout or b""


def appearance_consistency(media: str, reference: str, at_ratio: float = 0.5,
                           n: int = 16) -> float | None:
    """与参考图的外观一致度 ∈ [0,1]（1 = 完全一致）。

    做法：把镜头第 at_ratio 处的一帧和参考图都压成 16×16 缩略图，算归一化 RGB 欧氏距离。
    这是**真实像素**上的比对（hue/饱和度/亮度漂移=一致性差，能被量出来），
    阈值 `min_consistency` 由此判定 character/scene_inconsistency。
    采样点取 50% 处：避开片头黑帧，且正好落在运镜平移量的过零点上（几何与基线对齐）。
    """
    if not has_ffmpeg() or not is_media_file(media) or not is_media_file(reference):
        return None
    container = probe_container(media)
    duration = (container or {}).get("duration")
    at = (duration * at_ratio) if duration else None
    a = _thumbnail_bytes(media, n=n, at=at)
    b = _thumbnail_bytes(reference, n=n, yuv_first=True)
    if not a or len(a) != len(b):
        return None
    diff = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    return round(1.0 - diff / math.sqrt(len(a) * 255 * 255), 4)


# ---------- 汇总：一次测量 → 指标 + 缺陷 ----------

def detect_defects(m: dict, expect_duration: float | None = None) -> list[str]:
    """从量出来的指标推缺陷（纯判据，可单测）。"""
    t = THRESHOLDS
    defects: list[str] = []
    if (m.get("height") or 0) < t["min_height"]:
        defects.append("low_clarity")
    # 每像素码率只作**参考指标**记录，不单独判缺陷：
    # 静态/低细节内容码率天然低（实测干净的 1080p 静止镜头 < 0.02 bpp 也完全清晰），
    # 拿它当\"清晰度\"会把正常片子判死。真正的模糊要靠帧内高频能量，属于后续升级。
    if m.get("bitrate_kbps") and m.get("width") and m.get("height") and m.get("fps"):
        bpp = (m["bitrate_kbps"] * 1000) / (m["width"] * m["height"] * max(m["fps"], 1.0))
        m["bitrate_per_pixel"] = round(bpp, 4)
    else:
        m["bitrate_per_pixel"] = None
    if m.get("fps") and m["fps"] < t["min_fps"]:
        defects.append("fps_too_low")
    vol = m.get("mean_volume_db")
    if not m.get("has_audio") or (vol is not None and vol < t["min_mean_volume_db"]):
        defects.append("low_volume")
    if (m.get("black_seconds") or 0) > t["black_seconds"]:
        defects.append("black_frame")
    if (m.get("freeze_seconds") or 0) > t["freeze_seconds"]:
        defects.append("frozen_frame")
    dur = m.get("duration")
    if dur and expect_duration:
        ratio = dur / float(expect_duration)
        if ratio < t["duration_ratio_low"]:
            defects.append("too_short")
        elif ratio > t["duration_ratio_high"]:
            defects.append("too_long")
    return defects


def inspect(path: str | None, expect_duration: float | None = None) -> dict | None:
    """量一个媒体文件：返回指标 + defects；不是真文件/没 ffmpeg → None（调用方回落 mock 标签）。"""
    if not is_media_file(path):
        return None
    container = probe_container(path)
    if not container:
        return None
    m = dict(container)
    m["path"] = str(path)
    m["size_bytes"] = os.path.getsize(str(path))
    black, freeze, vol = measure_black_freeze_loudness(str(path), container.get("duration"))
    m["mean_volume_db"] = vol
    m["black_seconds"] = black
    m["freeze_seconds"] = freeze
    m["defects"] = detect_defects(m, expect_duration)
    return m


def describe(m: dict) -> str:
    """指标 → 一行中文摘要（终端/面板展示用）。"""
    fps = f"{m['fps']:.0f}" if m.get("fps") else "?"
    vol = (f"{m['mean_volume_db']:.1f}dB" if m.get("mean_volume_db") is not None
           else "无音轨")
    return (f"实测 {m.get('width')}x{m.get('height')}@{fps}fps "
            f"{m.get('duration') or 0:.1f}s 音量{vol} "
            f"黑帧{m.get('black_seconds') or 0:.2f}s 静止{m.get('freeze_seconds') or 0:.2f}s")
