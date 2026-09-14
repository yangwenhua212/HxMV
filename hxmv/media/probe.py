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
}

# 缺陷键必须与 core/critic.py 的 DEFECT_FIXES 对齐（那里定义修正方向）
DEFECT_KEYS = ("black_frame", "low_clarity", "fps_too_low", "low_volume", "no_audio",
               "frozen_frame", "too_short", "too_long",
               "blurry", "multi_shot", "silent_audio")

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


def measure_extended(path: str, duration: float | None = None) -> dict:
    """一次解码量出：画面模糊度 / 镜头切换次数 / EBU R128 响度 / 静音段占比。

    为什么不并进 measure_black_freeze_loudness：那条链的阈值已经标定过，
    再塞进 loudnorm（会归一化音频）与 blurdetect（依赖缩放与内容尺度）会互相影响，
    既有标定全部作废。新指标独立成链，代价是多一次解码——值得。

    三条实测坑（写在这里省得下次再踩）：
    1. `metadata=print` 必须紧跟在**产生它的滤镜之后**。放在 scdet 之前时，
       那一帧的 lavfi.scd.score 还没写上去，打印出来是空的（实测样本数 0）。
    2. 极度模糊时 blurdetect 输出 `nan`（梯度分母为 0）。只抓数字的正则会把它
       当成"没测到"→ 漏判最该抓的那一类糊。这里显式转成 blur_unmeasurable。
    3. loudnorm 对**全静音**音轨输出 `-inf`；解析成 float 后是 -inf，不能直接当响度用，
       要转成 None 并交给 silencedetect 的占比去判（否则会得出一条 -inf 的假指标）。
    """
    if not has_ffmpeg() or not is_media_file(path):
        return {}
    t = THRESHOLDS
    vf = (f"blurdetect=low=0.1:high=0.5,metadata=mode=print:key=lavfi.blur,"
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

    return {
        "blur_mean": blur_mean,
        "blur_max": blur_max,
        "scene_cuts": cuts,
        "lufs": round(lufs, 2) if lufs is not None else None,
        "silence_seconds": round(silence, 3),
        "silence_ratio": round(silence / duration, 3) if duration else None,
    }


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


# ---------- 汇总：一次测量 → 指标 + 缺陷 ----------

def detect_defects(m: dict, expect_duration: float | None = None,
                   expect_audio: bool = False,
                   expect_single_shot: bool = False) -> list[str]:
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
    return defects


def inspect(path: str | None, expect_duration: float | None = None,
            expect_audio: bool = False,
            expect_single_shot: bool = False) -> dict | None:
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
    # 第二趟量扩展判据（模糊/切换/响度/静音占比）——独立一条链，不动上面已标定的那条
    m.update(measure_extended(str(path), container.get("duration")))
    m["defects"] = detect_defects(m, expect_duration, expect_audio, expect_single_shot)
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
    return line
