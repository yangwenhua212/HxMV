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
import tempfile

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

    # ---------- 画面模糊（blurdetect，拉普拉斯方差类度量）----------
    # 标定（本机 local 渲染基准，1280x720）：清晰原片均值 5.13 → gblur sigma=1.5 时 8.93
    # → sigma=4 时 11.95。取 12.0 = "明显糊成一团"才判，轻微发软不误杀。
    # 注意：这是**内容相关**的绝对量（纹理少的画面天然低），接真实 AI 视频后应重新标定。
    "max_blur_mean": 12.0,
    # 极模糊时 blurdetect 输出 `nan`（梯度分母为 0）——检测到就按"糊到量不出"处理
    "blur_unmeasurable": 999.0,

    # ---------- 镜头切换（scdet）----------
    # 标定：红→蓝硬切换单帧 score=15.6，静止/连续画面 ≈0（实测 local 成片两镜头拼接最大仅 2.5）。
    # 超过 10 记一次切换；单镜头任务允许多少次切换由 max_scene_cuts 定。
    "scene_cut_score": 10.0,
    "max_scene_cuts": 0,

    # ---------- 响度与静音（loudnorm / silencedetect）----------
    "min_lufs": -40.0,           # EBU R128 综合响度低于此 → low_volume（比 mean_volume 更贴近人耳）
    "silence_db": -50.0,         # silencedetect 的静音门限
    "silence_min_seconds": 0.5,  # 短于此时长的静音不算一段
    "silence_ratio": 0.90,       # 静音累计占比超过此 → silent_audio（有音轨但等于没有）
    # ---------- 运镜（v0.9）：抽帧估全局位移/缩放，判「是不是真按规格动了」 ----------
    # 门限按 local 真渲染标定（见 measure_camera 的注释）：方向必须对，幅度必须够。
    "camera_min_zoom_delta": 0.015,   # |缩放比 - 1| 小于此 = 没推拉
    "camera_min_pan_ratio": 0.010,    # 位移占画面宽/高的比例，小于此 = 没平移
    # ---------- 接缝（同场戏两镜之间的跳变）----------
    # 标定（local 真渲染，两镜 4s，16×16 像素相似度，接缝前后各取 0.08s）：
    #   同场戏正常接缝 0.891 / 色调大幅漂移的坏接缝 0.772 / 坏接缝**加淡入淡出后 0.905**。
    # 门限取 0.84 = 正常接缝放行、明显跳变抓得住，且"加转场"这条修正真的能把它拉回阈值以上。
    # 接真实 AI 视频后应重新标定（AI 镜头之间的自然差异比本地渲染大）。
    "min_seam_similarity": 0.84,
    "seam_sample_seconds": 0.08,      # 接缝前后各取多少秒的帧来比（要落在转场混合窗口内）
}

# 缺陷键必须与 core/critic.py 的 DEFECT_FIXES 对齐（那里定义修正方向）
DEFECT_KEYS = ("black_frame", "low_clarity", "fps_too_low", "low_volume", "no_audio",
               "frozen_frame", "too_short", "too_long",
               "blurry", "multi_shot", "silent_audio",
               "camera_mismatch", "seam_jump")

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


_ENCODER: tuple[str, str] | None = None


def drawtext_status(fontfile: str | None = None) -> tuple[bool, str]:
    """drawtext（往画面上写字）能不能真的跑通——local provider 渲染参考图全靠它。

    为什么单独查这一项（实测踩过）：ffmpeg 二进制在、libx264 也在，
    唯独系统缺 fontconfig 配置（Windows/精简镜像常见）→ drawtext 报
    "Fontconfig error: Cannot load default config file"，整条渲染链全挂；
    而只检查"ffmpeg 存在"的自检会给出绿灯，用户看到的就是"能跑"却一直失败。

    fontfile 显式给字体路径时可以绕开 fontconfig——传它进来等于测"修复后能不能用"。
    """
    ff = ffmpeg_path("ffmpeg")
    if not ff:
        return False, "无 ffmpeg"
    vf = "drawtext=text='x':fontsize=16:fontcolor=white:x=0:y=0"
    if fontfile:
        escaped = fontfile.replace("\\", "/").replace(":", r"\:")
        vf = f"drawtext=fontfile='{escaped}':text='x':fontsize=16:fontcolor=white:x=0:y=0"
    tmp = os.path.join(tempfile.gettempdir(), f"_hxmv_drawtext_{os.getpid()}.png")
    code, out = _run([ff, "-v", "error", "-y", "-f", "lavfi", "-i",
                      "color=c=black:s=64x64:d=1", "-vf", vf, "-frames:v", "1", tmp],
                     timeout=60)
    if code == 0 and os.path.isfile(tmp):
        return True, "可用"
    hint = (out or "").strip().splitlines()
    detail = hint[-1][:160] if hint else "未知原因"
    try:
        if os.path.isfile(tmp):
            os.remove(tmp)
    except OSError:
        pass
    return False, detail


def encoder_args(quality: int = 26) -> list[str]:
    """可用的视频编码参数：优先 libx264（质量/体积最好），**没有就退 mpeg4**。

    为什么必须有这层退让：Android/Termux 上的 ffmpeg 构建不一定编进 libx264，
    而"手机上直接跑不起来"比"编码次一点"糟糕得多。跑不了 x264 就退到 ffmpeg
    自带的 mpeg4（任何构建都有），代价只是同样的 crf 换成 qscale。
    `HXMV_ENCODER=mpeg4` 可强制指定（老设备/异常构建上排查用）。

    末尾固定带 `-movflags +faststart`：把索引（moov）挪到文件头。
    实测：不带这个参数，mp4 是「mdat 在前、moov 在尾」，手机浏览器/微信/飞书 webview
    里播这种文件会**一直转圈**（真机反馈「生成的视频看不了」）。本地下载看没事，
    但页面里嵌播放器/发给别人看就不行。
    """
    global _ENCODER
    forced = os.environ.get("HXMV_ENCODER", "").strip().lower()
    if _ENCODER is None and forced in ("libx264", "x264", "mpeg4"):
        _ENCODER = ("libx264", "x264") if forced in ("libx264", "x264") else ("mpeg4", "mpeg4")
    if _ENCODER is None:
        rc, out = _run(["ffmpeg", "-hide_banner", "-encoders"])
        _ENCODER = ("libx264", "x264") if rc == 0 and "libx264" in out else ("mpeg4", "mpeg4")
    name, _ = _ENCODER
    if name == "libx264":
        base = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]
    else:
        # mpeg4 不吃 -crf：按 crf 粗略折算 qscale（1 最好 / 31 最差）
        q = max(2, min(12, int(quality / 4)))
        base = ["-c:v", "mpeg4", "-qscale:v", str(q)]
    return base + ["-movflags", "+faststart"]


def encoder_name() -> str:
    encoder_args()
    return _ENCODER[0] if _ENCODER else "unknown"


def is_media_file(path: str | None) -> bool:
    """result 里的 media/output 是不是**真文件**（mock 世界给的是占位字符串，不落盘）。"""
    return bool(path) and os.path.isfile(str(path))


def _fps(text: str) -> float | None:
    """'30000/1001' → 29.97；'15/1' → 15.0。"""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*$", text or "")
    if not m:
        return None
    num, den = float(m.group(1)), float(m.group(2))
    return num / den if den else None


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
    """只取黑帧 / 静止 / 平均音量三个指标——**一次解码量全部**的实现在 `measure_all`。

    返回 (black_seconds, freeze_seconds, mean_volume_db)；不是真媒体 / 没有 ffmpeg 时全为 None。

    n=0.002 是静止检测的噪声容差，按实测标定：真静止（帧完全重复，差值≈0）必抓，
    慢速推镜（差值 ~0.0015）不误报；调大就会漏掉真静止。
    真静止（帧完全重复）差值为 0，任何容差都抓得到。
    duration 用于收尾：片段\"一直静止到结尾\"时 freezedetect 只报 freeze_start，
    没有 freeze_end——不补上就会漏掉这类（最常见的）静止缺陷。
    """
    m = measure_all(path, duration)
    if not m:
        return None, None, None
    return m["black_seconds"], m["freeze_seconds"], m["mean_volume_db"]


def measure_all(path: str, duration: float | None = None) -> dict:
    """**一次解码**量出全部指标：黑帧 / 静止 / 平均音量 / 模糊度 / 镜头切换 / 响度 / 静音段。

    为什么全部合成一条链：这些都是**分析型**滤镜——blurdetect / freezedetect / blackdetect /
    scdet 只读画面、不改画面；volumedetect / silencedetect 排在 loudnorm 之前，看到的是原始信号。

    **别指望它明显提速（实测数字）**：1080p/5s 素材两趟 2.45s → 一趟 2.34s，只省约 5%。
    瓶颈是滤镜本身的计算（blurdetect 逐帧算梯度），不是解码；合并省下的仅仅是
    "多起一次 ffmpeg"的固定开销。真正的价值是**测量与解析只有一份**（原来两处各写一遍，
    阈值含义还分散在两处注释里）。

    四条实测约束（**别改顺序**）：
    1. `metadata=print` 必须紧跟**产生它**的滤镜——blurdetect 与 scdet 各自后面放一个；
       放在 scdet 之前时，那一帧的 lavfi.scd.score 还没写上去（实测样本数 0）。
    2. loudnorm 放音频链**最后**：它会归一化响度，排前面会让后面的检测看到被改过的信号。
    3. 片尾"未闭合"的段要用 duration 收尾（一直黑/一直静止到结尾时只有 start，没有 end）。
    4. 极度模糊时 blurdetect 输出 `nan`（梯度分母为 0）。只抓数字的正则会把它当成
       "没测到"→ 漏判最该抓的那类糊；这里显式转成 blur_unmeasurable。
    """
    if not has_ffmpeg() or not is_media_file(path):
        return {}
    t = THRESHOLDS
    vf = (f"blackdetect=d={t['black_seconds']}:pix_th=0.10,"
          f"freezedetect=n=0.002:d=1.2,"
          f"blurdetect=low=0.1:high=0.5,"
          f"metadata=mode=print:key=lavfi.blur,"
          f"scdet=threshold={t['scene_cut_score']},"
          f"metadata=mode=print:key=lavfi.scd.score")
    # 顺序有讲究：volumedetect/silencedetect 要先看**原始**信号，loudnorm 放最后
    af = (f"volumedetect,"
          f"silencedetect=n={t['silence_db']}dB:d={t['silence_min_seconds']},"
          f"loudnorm=print_format=json")
    rc, out = _run(["ffmpeg", "-hide_banner", "-i", path, "-vf", vf, "-af", af, "-f", "null", "-"])
    if rc != 0 and not out:
        return {}

    # ---- 模糊度 ----
    blurs: list[float] = []
    unmeasurable = False
    for token in re.findall(r"lavfi\.blur=(\S+)", out):
        try:
            val = float(token)
        except ValueError:
            unmeasurable = True       # nan / inf 等非数字写法
            continue
        if math.isnan(val) or math.isinf(val):
            unmeasurable = True
        else:
            blurs.append(val)
    blur_mean = round(sum(blurs) / len(blurs), 3) if blurs else None
    blur_max = (t["blur_unmeasurable"] if unmeasurable
                else (round(max(blurs), 3) if blurs else None))

    # ---- 镜头切换：得分超过阈值的帧数就是切换次数 ----
    scores = [float(x) for x in re.findall(r"lavfi\.scd\.score=([\d.]+)", out)]
    cuts = sum(1 for s in scores if s > t["scene_cut_score"])

    # ---- 响度（LUFS）与静音段 ----
    lufs: float | None = None
    m_i = re.search(r'"input_i"\s*:\s*"([^"]+)"', out)
    if m_i:
        try:
            val = float(m_i.group(1))
            lufs = None if (math.isnan(val) or math.isinf(val)) else val
        except ValueError:
            lufs = None

    silence, last = 0.0, None
    for line in out.splitlines():
        m = re.search(r"silence_(start|end):\s*([\d.]+)", line)
        if not m:
            continue
        if m.group(1) == "start":
            last = float(m.group(2))
        elif last is not None:
            silence += max(0.0, float(m.group(2)) - last)
            last = None
    if last is not None and duration:      # 一直静音到片尾
        silence += max(0.0, duration - last)

    # ---- 黑帧：累计时长（片尾未闭合要补上） ----
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", out))
    if re.search(r"black_start:([\d.]+)\s*$", out, flags=re.M) and duration:
        last_black = float(re.findall(r"black_start:([\d.]+)", out)[-1])
        if last_black and "black_end" not in out.split(f"black_start:{last_black}")[-1]:
            black += max(0.0, duration - last_black)

    # ---- 静止：同样可能在片尾未闭合（原来这条在另一趟解码里，现在合并） ----
    freeze, last_freeze = 0.0, None
    for line in out.splitlines():
        m = re.search(r"freeze_(start|end):\s*([\d.]+)", line)
        if not m:
            continue
        if m.group(1) == "start":
            last_freeze = float(m.group(2))
        elif last_freeze is not None:
            freeze += max(0.0, float(m.group(2)) - last_freeze)
            last_freeze = None
    if last_freeze is not None and duration:   # 静止一直到片尾
        freeze += max(0.0, duration - last_freeze)

    mv = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", out)
    return {
        "black_seconds": round(black, 3),
        "freeze_seconds": round(freeze, 3),
        "mean_volume_db": float(mv.group(1)) if mv else None,
        "blur_mean": blur_mean,
        "blur_max": blur_max,
        "scene_cuts": cuts,
        "lufs": round(lufs, 2) if lufs is not None else None,
        "silence_seconds": round(silence, 3),
        "silence_ratio": round(silence / duration, 3) if duration else None,
    }


def measure_extended(path: str, duration: float | None = None) -> dict:
    """（兼容包装）只取扩展指标——真正的测量在 `measure_all`（一次解码量全部）。"""
    m = measure_all(path, duration)
    return ({k: m.get(k) for k in ("blur_mean", "blur_max", "scene_cuts", "lufs",
                                   "silence_seconds", "silence_ratio")} if m else {})


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


# ---------- 真抽帧：给「会看图的眼睛」用（L2 身份 / L3 语义） ----------

def extract_frames(path: str, count: int = 4, max_edge: int = 512,
                   out_dir: str | None = None) -> tuple[str, list[str]]:
    """从媒体里等距抽 count 张 JPEG 帧，返回 `(临时目录, 帧路径列表)`。

    给 L2（参考图 vs 真帧的身份比对）和 L3（把真画面喂视觉模型）用。
    纯读操作：不改产物、不写进产物目录；默认落系统临时目录，用完调 `cleanup_frames`。
    采样点落在 [5%, 95%] 区间：避开片头黑场与片尾收尾帧。
    """
    if count < 1 or not has_ffmpeg() or not is_media_file(path):
        return "", []
    container = probe_container(path) or {}
    duration = container.get("duration") or 0.0
    tmp = out_dir or tempfile.mkdtemp(prefix="hxmv_frames_")
    os.makedirs(tmp, exist_ok=True)
    if duration and duration > 0.2:
        step = (0.95 - 0.05) / max(count - 1, 1)
        stamps = [duration * (0.05 + step * i) for i in range(count)]
    else:
        stamps = [0.0]
    frames: list[str] = []
    for i, at in enumerate(stamps):
        dest = os.path.join(tmp, f"f{i}.jpg")
        rc, _ = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", path,
                      "-frames:v", "1", "-vf", f"scale={max_edge}:-2", "-q:v", "4", dest])
        if rc == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
            frames.append(dest)
    return tmp, frames


def cleanup_frames(tmp: str) -> None:
    """删掉 extract_frames 造的临时目录（失败也不抛）。"""
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 运镜测量：抽帧 → 估计全局位移/缩放（纯 stdlib，零依赖） ----------

def _gray_frame(path: str, at: float, w: int = 64, h: int = 36) -> bytes | None:
    """取某时刻的一帧 → w×h 灰度原始字节（不做彩色转换，测量只要亮度结构）。"""
    if not has_ffmpeg() or not is_media_file(path):
        return None
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", path,
                        "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo",
                        "-pix_fmt", "gray", "-"], capture_output=True)
    data = p.stdout or b""
    return data if len(data) == w * h else None


def _view(img: bytes, w: int, h: int, scale: float, dx: float, dy: float) -> list[int]:
    """把 A 帧按「相机参数」取一个视野：以中心偏移 (dx,dy)、缩放 scale 裁窗后重采样到 w×h。

    scale>1 = 视野更小（画面被放大）；dx>0 = 视野向右偏（= 镜头右移）；dy>0 = 视野向下偏。

    **双线性**采样（不是最近邻）：最近邻会让同一个静态画面在候选尺度之间跳来跳去，
    实测静止镜头的缩放估计能在 0.94~1.04 之间抖——那比判据门限还大，等于判不了。
    """
    full_w, full_h = w * scale, h * scale
    left, top = (w - full_w) / 2 + dx, (h - full_h) / 2 + dy
    out = bytearray(w * h)
    for j in range(h):
        fy = top + full_h * (j + 0.5) / h - 0.5
        y0 = int(fy // 1)
        ty = fy - y0
        y1 = min(h - 1, y0 + 1)
        y0 = 0 if y0 < 0 else (h - 1 if y0 >= h else y0)
        for i in range(w):
            fx = left + full_w * (i + 0.5) / w - 0.5
            x0 = int(fx // 1)
            tx = fx - x0
            x1 = min(w - 1, x0 + 1)
            x0 = 0 if x0 < 0 else (w - 1 if x0 >= w else x0)
            v = (img[y0 * w + x0] * (1 - tx) * (1 - ty) + img[y0 * w + x1] * tx * (1 - ty)
                 + img[y1 * w + x0] * (1 - tx) * ty + img[y1 * w + x1] * tx * ty)
            out[j * w + i] = int(v + 0.5)
    return out


def _halve(img: bytes, w: int, h: int) -> tuple[bytes, int, int]:
    """2×2 盒式降采样（粗匹配用；纯 Python，一帧也就几百次加法）。"""
    hw, hh = w // 2, h // 2
    out = bytearray(hw * hh)
    for j in range(hh):
        row = (2 * j) * w
        for i in range(hw):
            o = row + 2 * i
            out[j * hw + i] = (img[o] + img[o + 1] + img[o + w] + img[o + w + 1]) // 4
    return bytes(out), hw, hh


def _cost(a: bytes, b: bytes, w: int, h: int, scale: float, dx: float, dy: float) -> float:
    return sum(abs(x - y) for x, y in zip(_view(a, w, h, scale, dx, dy), b)) / len(b)


def _match(a: bytes, b: bytes, w: int, h: int, span: int = 8) -> tuple[float, float, float]:
    """找最能解释「A → B」的相机动作：返回 (scale, dx, dy)，最小化平均绝对差。

    两级搜索（**逐帧测量要快**：这个函数每个镜头都要跑，粗搜 729 个候选在纯 Python 里要 1~2 秒）：
      粗搜：半分辨率（2×2 盒式）+ 步长 2（半分辨率像素 = 2 个原像素）→ 只有 1/8 的候选量；
      精修：回到原分辨率，在粗解附近 ±2px / ±0.02 缩放。

    候选尺度要**覆盖真实运镜幅度**（推镜一整段能到 1.25+）：只在 1.0 附近搜会量出"没推"，
    于是好镜头被判 camera_mismatch（踩过）。
    """
    hw, hh = w // 2, h // 2
    ha, hb = _halve(a, w, h), _halve(b, w, h)
    best, best_cost = (1.0, 0.0, 0.0), None
    for scale in (0.80, 0.92, 1.0, 1.10, 1.25, 1.40):
        for dx in range(-span // 2, span // 2 + 1, 2):
            for dy in range(-span // 2, span // 2 + 1, 2):
                cost = _cost(ha[0], hb[0], hw, hh, scale, dx, dy)
                if best_cost is None or cost < best_cost:
                    best_cost, best = cost, (scale, float(dx) * 2, float(dy) * 2)
    scale, dx, dy = best
    for s2 in (scale - 0.03, scale - 0.015, scale, scale + 0.015, scale + 0.03):
        for x2 in (dx - 2, dx - 1, dx, dx + 1, dx + 2):
            for y2 in (dy - 2, dy - 1, dy, dy + 1, dy + 2):
                if s2 <= 0:
                    continue
                cost = _cost(a, b, w, h, s2, x2, y2)
                if cost < best_cost:
                    best_cost, best = cost, (s2, x2, y2)
    return round(best[0], 4), round(best[1], 2), round(best[2], 2)


def measure_camera(path: str, w: int = 64, h: int = 36) -> dict | None:
    """量一个镜头的运镜：`{zoom, pan, tilt, pan_ratio, tilt_ratio, move}`。

    做法：抽两帧（10% 与 90%，跨整段），在候选 (缩放, 位移) 里找最能解释"从 A 到 B"的那组 ——
    最近邻重采样 + 平均绝对差，纯 stdlib（没有 OpenCV/numpy 也能跑）。

    为什么采样 10%/90%：既避开片头黑场，又拿到接近整段的最大位移（10%/50% 只有一半，1px 级别的
    位移根本量不准）。判据只信**方向 + 幅度够不够**，不做亚像素拟合。

    标定（local 真渲染，5s）：push_in ≈ 1.2~1.3、pan_right ≈ +8~10px(64宽) → 门限取
    `camera_min_zoom_delta 0.015` / `camera_min_pan_ratio 0.010`，静止镜头 ≈ 1.0 / 0px。
    """
    if not has_ffmpeg() or not is_media_file(path):
        return None
    duration = (probe_container(path) or {}).get("duration") or 0.0
    if duration < 0.5:
        return None
    a = _gray_frame(path, duration * 0.10, w, h)
    b = _gray_frame(path, duration * 0.90, w, h)
    if not a or not b:
        return None
    zoom, dx, dy = _match(a, b, w, h)
    # `_match` 的 scale 是"匹配 B 所需的**取景窗口**大小"：窗口更小（scale<1）= 画面被放大。
    # 对外统一成放大倍数（zoom>1 = 推近），与 camera 规格、判据、人话都一致。
    return {"zoom": round(1.0 / zoom, 4) if zoom else None, "pan": dx, "tilt": dy,
            "pan_ratio": round(dx / w, 4), "tilt_ratio": round(dy / h, 4)}


def camera_defects(cam: dict | None, expect_move: str | None, amount=None) -> list[str]:
    """按**镜头规格**判运镜对不对：方向必须一致、幅度必须够。返回缺陷键列表。

    只在**该镜头有明确运镜规格**时才判（没规格 = 不查，别拿默认值冤枉镜头）；
    static 不判（静止是合法的艺术选择，多查一层只会带来假阳）。
    """
    if not cam or not expect_move:
        return []
    from ..core import camera as cam_mod            # 反向导入：本模块只做测量，规格表在 camera
    move = cam_mod.normalize_move(expect_move)
    if move in (None, cam_mod.MOVE_STATIC):
        return []
    zoom = cam.get("zoom") or 1.0
    pan_ratio = cam.get("pan_ratio", 0.0) or 0.0
    tilt_ratio = cam.get("tilt_ratio", 0.0) or 0.0
    t = THRESHOLDS
    zoom_d = zoom - 1.0
    want = cam_mod.DIRECTIONAL.get(move)
    if want is None:
        # 环绕/跟拍/手持：只要求"确实在动"（方向由整段轨迹决定，单对帧判不出）
        moving = (abs(zoom_d) > t["camera_min_zoom_delta"]
                  or abs(pan_ratio) > t["camera_min_pan_ratio"]
                  or abs(tilt_ratio) > t["camera_min_pan_ratio"])
        return [] if moving else ["camera_mismatch"]
    axis, sign = want
    got = zoom_d if axis == "zoom" else (pan_ratio if axis == "pan" else tilt_ratio)
    if axis == "zoom":
        return [] if got * sign > t["camera_min_zoom_delta"] else ["camera_mismatch"]
    return [] if got * sign > t["camera_min_pan_ratio"] else ["camera_mismatch"]


# ---------- 接缝：同场戏两镜之间的跳变（成片层"看着怪"的主要来源之一） ----------

def seam_similarity(prev: str, nxt: str, n: int = 16) -> float | None:
    """前一镜**末帧** vs 后一镜**首帧**的外观相似度 ∈ [0,1]（同一套 16×16 像素距离）。

    为什么看这两帧：观众感知到的"跳"就发生在剪辑点上；同场戏（同一个场景、同一个角色）
    的两镜之间突然变色调/变构图，就是"看着怪"，这和"换个场景硬切"是两回事——所以调用方
    必须**按场次分流**（不同场次之间硬切是正常的）。
    """
    if not (is_media_file(prev) and is_media_file(nxt)):
        return None
    d_prev = (probe_container(prev) or {}).get("duration") or 0.0
    a = _thumbnail_bytes(prev, n=n, at=max(0.0, d_prev - 0.12))
    b = _thumbnail_bytes(nxt, n=n, at=0.12, yuv_first=True)
    if not a or len(a) != len(b):
        return None
    diff = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
    return round(1.0 - diff / math.sqrt(len(a) * 255 * 255), 4)


def seam_defects(film: str | None, files: list[str], scenes: list[str] | None = None,
                 transition: str | None = None) -> tuple[list[str], list[dict]]:
    """成片接缝检查：只对**同一场戏**的相邻镜头判跳变（换场硬切不判）。

    **量的是成片本身**（在剪辑点前后各取一帧），不是两个原始镜头文件——否则
    "同一场戏接缝跳变 → 加淡入淡出"这条修正根本不会改变实测值（成片变了、镜头文件没变），
    会被收手守卫当成"修正没落地"（踩过这一类"报了缺陷但修不动"的坑）。

    剪辑点位置由各镜头时长推出来（交叉淡化时淡化的中心就是接缝中心）。
    返回 `(defects, seams)`；seams 是逐条遥测（进 metrics 给面板/审计看）。
    """
    defects: list[str] = []
    seams: list[dict] = []
    if not is_media_file(film) or not (files and len(files) > 1):
        return defects, seams
    film_duration = float((probe_container(film) or {}).get("duration") or 0.0)
    durations = [float((probe_container(f) or {}).get("duration") or 0.0) for f in files]
    if not film_duration or any(d <= 0 for d in durations):
        return defects, seams
    fade = 0.4 if str(transition or "") == "fade" else 0.0
    cum = 0.0
    for i in range(1, len(files)):
        cum += durations[i - 1]
        join = cum - fade * i + fade / 2
        same_scene = True
        if scenes and i < len(scenes) and i - 1 < len(scenes):
            same_scene = str(scenes[i]) == str(scenes[i - 1])
        if not same_scene:
            continue                      # 换场硬切是正常剪辑手法，不判
        before, after = join - THRESHOLDS["seam_sample_seconds"], join + THRESHOLDS["seam_sample_seconds"]
        if before < 0 or after > film_duration:
            continue
        a = _thumbnail_bytes(film, n=16, at=before)
        b = _thumbnail_bytes(film, n=16, at=after, yuv_first=True)
        if not a or len(a) != len(b):
            continue
        diff = math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
        sim = round(1.0 - diff / math.sqrt(len(a) * 255 * 255), 4)
        seams.append({"at": i, "similarity": sim, "join": round(join, 2)})
        if sim < THRESHOLDS["min_seam_similarity"]:
            defects.append("seam_jump")
    return defects, seams


# ---------- 汇总：一次测量 → 指标 + 缺陷 ----------

def detect_defects(m: dict, expect_duration: float | None = None,
                   expect_audio: bool = False,
                   expect_single_shot: bool = False,
                   expect_camera: str | None = None) -> list[str]:
    """从量出来的指标推缺陷（纯判据，可单测）。

    expect_audio：**默认无声**——AI 视频本来就不带音轨，用户没要音频时"没音轨"是正常状态，
    不是缺陷（判它就会每条都废片，还会派生一个修不动的 enable_audio）。
    只有任务明确要音频（with_audio）时，缺音轨/音量低才算缺陷。

    expect_single_shot：**只有单镜头任务**（GENERATE_SHOT）才谈"镜头切换是缺陷"。
    成片（COMPOSE）本来就是多镜头拼的，拿它判 multi_shot 会把每一部成片都判死、
    还会派生一个永远修不好的 rewrite_prompt_single_shot。
    """
    t = THRESHOLDS
    defects: list[str] = []
    if (m.get("height") or 0) < t["min_height"]:
        defects.append("low_clarity")
    # 模糊与"分辨率不足"是两种病，必须分开判：升分辨率修不好对焦糊，改提示词也修不好像素不够。
    # 混成一个 low_clarity 时，糊片会被反复升分辨率、永远修不好（修正方向从一开始就是错的）。
    if (m.get("blur_mean") or 0) > t["max_blur_mean"] or (m.get("blur_max") or 0) >= t["blur_unmeasurable"]:
        defects.append("blurry")
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
    # 单镜头任务里出现镜头切换 = 模型自己剪了片，画面内容已经不是"一个连续镜头"。
    # 只对单镜头任务判：成片（COMPOSE）本来就是多镜头拼的，判它就是每条都废片。
    if expect_single_shot and (m.get("scene_cuts") or 0) > t["max_scene_cuts"]:
        defects.append("multi_shot")
    vol = m.get("mean_volume_db")
    if expect_audio:
        # 要了音频才谈音频缺陷；三种病分开治（修正方向完全不同）：
        # ① 没音轨——对不存在的音轨做增益是空操作，得让模型真的带音频；
        # ② 有音轨但几乎全程静音——增益同样是空操作，也得让模型真的出声；
        # ③ 只是偏轻——增益有用。
        # 用 `"lufs" in m` 而不是 `m.get("lufs") is None`：**没测量**与**测出来是静音**
        # （loudnorm 对全静音给 -inf）必须区分，否则没有该指标的调用路径会被误判成静音。
        if not m.get("has_audio"):
            defects.append("no_audio")
        elif m.get("silence_ratio") is not None and m["silence_ratio"] >= t["silence_ratio"]:
            defects.append("silent_audio")
        elif "lufs" in m and m.get("lufs") is None:
            defects.append("silent_audio")
        elif ((vol is not None and vol < t["min_mean_volume_db"])
              or (m.get("lufs") is not None and m["lufs"] < t["min_lufs"])):
            defects.append("low_volume")
    # 没要音频 → 有声无声都不判缺陷，只作遥测（默认无声）
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
    # 运镜：规格要求的方向/幅度对不上 → camera_mismatch（只在有规格时判；static 不判）
    defects.extend(camera_defects(m.get("camera"), expect_camera))
    for d in (m.get("seam_defects") or []):
        if d not in defects:
            defects.append(d)
    return defects


def inspect(path: str | None, expect_duration: float | None = None,
            expect_audio: bool = False,
            expect_single_shot: bool = False,
            expect_camera: str | None = None,
            seam_film: str | None = None,
            seam_files: list[str] | None = None,
            seam_scenes: list[str] | None = None,
            seam_transition: str | None = None) -> dict | None:
    """量一个媒体文件：返回指标 + defects；不是真文件/没 ffmpeg → None（调用方回落 mock 标签）。

    除物理指标外还量两件"看着怪不怪"的事（都不花钱、纯抽帧算）：
    - **运镜**：全局位移/缩放（`measure_camera`）——进 metrics 做遥测，有规格时进判据；
    - **接缝**：成片里同场戏相邻镜头的剪辑点跳变（`seam_defects`，只有传 seam_files 时跑）。
    """
    if not is_media_file(path):
        return None
    container = probe_container(path)
    if not container:
        return None
    m = dict(container)
    m["path"] = str(path)
    m["size_bytes"] = os.path.getsize(str(path))
    # **一次解码**量全部指标（黑帧/静止/音量/模糊/切换/响度/静音段）——原来分两趟，白多解一遍
    m.update(measure_all(str(path), container.get("duration")))
    m["camera"] = measure_camera(str(path))
    if seam_files:
        seam_d, seams = seam_defects(seam_film or str(path), list(seam_files), seam_scenes,
                                     seam_transition)
        m["seams"], m["seam_defects"] = seams, seam_d
    m["defects"] = detect_defects(m, expect_duration, expect_audio, expect_single_shot,
                                  expect_camera)
    return m


def describe(m: dict) -> str:
    """指标 → 一行中文摘要（终端/面板展示用）。

    新增指标只在测到时才出现：没跑扩展探针的路径（老 run 记录、mock 世界）
    摘要保持原样，不会凭空多出 `模糊度None` 这种噪声。
    """
    fps = f"{m['fps']:.0f}" if m.get("fps") else "?"
    vol = (f"{m['mean_volume_db']:.1f}dB" if m.get("has_audio")
           else "无声（默认）")
    line = (f"实测 {m.get('width')}x{m.get('height')}@{fps}fps "
            f"{m.get('duration') or 0:.1f}s 音频{vol} "
            f"黑帧{m.get('black_seconds') or 0:.2f}s 静止{m.get('freeze_seconds') or 0:.2f}s")
    if m.get("blur_mean") is not None:
        line += f" 模糊度{m['blur_mean']:.1f}"
    if m.get("scene_cuts"):
        line += f" 切换{m['scene_cuts']}"
    if m.get("lufs") is not None:
        line += f" 响度{m['lufs']:.1f}LUFS"
    cam = m.get("camera") or {}
    if cam.get("zoom") is not None:
        line += f" 运镜缩放{cam['zoom']:.3f}/横移{cam.get('pan', 0):+.1f}px/俯仰{cam.get('tilt', 0):+.1f}px"
    for s in (m.get("seams") or []):
        line += f" 接缝{s['at']}相似度{s['similarity']:.2f}"
    return line
