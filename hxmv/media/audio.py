"""音频层：旁白 TTS + 环境音合成 + 混音。

为什么需要它：HxMV 以前**完全没有音画交付** —— 视频模型（CogVideoX-Flash 免费档）不带
音轨，成片默认无声，用户剧本里写的「旁白 / 音效」全落空。这一层把能做的补齐：

- **旁白**：edge-tts（免费、中文音色多、不需要额外账号）。没装就如实跳过，不伪造。
- **环境音**：ffmpeg 合成 —— 海浪 = 棕噪声 + 慢速起伏；风 = 粉噪声 + 带通 + 慢扫。
  鸟鸣这类**需要真实素材库**的声音不合成（合成出来是电子噪音，不如不加）：
  识别到就写进 detail 说明「未配制」，让用户知道差什么，而不是假装做了。
- **混音**：旁白压过环境音（约 -6dB），成片时长以旁白为准（画面不够就定格补，不裁旁白）。

对外只有两个入口：`plan_tracks(input)` 解析要什么；`apply(video, spec, outdir)` 真做。
"""
from __future__ import annotations

import os
import shutil
import subprocess

# 环境音关键词 → 合成配方（识别不到的词写进 missing，不假装）
_AMBIENCE = {
    "waves": ("海浪", "海涛", "波涛", "浪声", "海浪声"),
    "wind": ("风", "风声", "沙沙"),
}

# 旁白音色：中文男声（叙述感）；可用 HXMV_TTS_VOICE 覆盖
DEFAULT_VOICE = os.environ.get("HXMV_TTS_VOICE", "zh-CN-YunjianNeural")
DEFAULT_RATE = os.environ.get("HXMV_TTS_RATE", "-5%")


def _run(args: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stderr or "")[-600:]
    except Exception as e:  # ffmpeg/edge-tts 缺失或超时都不能拖垮闭环
        return -1, str(e)[:200]


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _probe_duration(path: str) -> float:
    """量媒体时长（秒）。量不到返回 0。之前把 stderr 当 JSON 解 → 恒为 0 → 混音时长失控。"""
    ff = _ffmpeg()
    if not os.path.isfile(path):
        return 0.0
    ffprobe = shutil.which("ffprobe") or (os.path.join(os.path.dirname(ff), "ffprobe") if ff else None)
    if not ffprobe:
        return 0.0
    try:
        p = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=120)
        return float((p.stdout or "").strip() or 0) if p.returncode == 0 else 0.0
    except Exception:
        return 0.0


def _edge_tts() -> str | None:
    """找 edge-tts：优先 PATH，其次 Hermes 自带 venv（本机就是这么装的）。"""
    hit = shutil.which("edge-tts")
    if hit:
        return hit
    cand = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/edge-tts")
    return cand if os.path.isfile(cand) else None


def tts_available() -> bool:
    return bool(_edge_tts())


def synthesize_narration(text: str, out: str, voice: str | None = None,
                         rate: str | None = None) -> bool:
    """旁白 → mp3。文本为空/没装 edge-tts → 返回 False（调用方如实记录）。"""
    text = (text or "").strip()
    if not text:
        return False
    exe = _edge_tts()
    if not exe:
        return False
    rc, _ = _run([exe, "--voice", voice or DEFAULT_VOICE, f"--rate={rate or DEFAULT_RATE}",
                  "--text", text, "--write-media", out])
    return rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 1024


def ambience_kinds(sfx_text: str) -> tuple[list[str], list[str]]:
    """从「音效：」那段文字里识别能合成的环境音，返回 (可做, 做不了的关键词)。"""
    text = str(sfx_text or "")
    can, missing = [], []
    for kind, words in _AMBIENCE.items():
        if any(w in text for w in words):
            can.append(kind)
    # 别的声音（鸟鸣/脚步/音乐…）如实列出来：合成就得接素材库
    for w in ("鸟鸣", "鸟叫", "鸟", "脚步", "笑声", "音乐", "配乐", "钟声", "雷", "雨",
              "水声", "溪", "鼓", "笛", "琴", "叫", "喊"):
        if w in text and not any(w in x for _, ws in _AMBIENCE.items() for x in ws):
            missing.append(w)
    return can, sorted(set(missing))


def synthesize_ambience(kinds: list[str], seconds: float, outdir: str) -> dict[str, str]:
    """合成环境音层（每个 kind 一个文件）。合成不出来就不产出那个文件。"""
    ff = _ffmpeg()
    made: dict[str, str] = {}
    if not ff or seconds <= 0:
        return made
    d = max(2.0, float(seconds))
    recipes = {
        # 海浪：棕噪声 + 低通 + 约 7 秒一次的慢速起伏（涌浪感）
        "waves": ("anoisesrc=color=brown:amplitude=0.6:d={d}[n];"
                  "[n]lowpass=f=900,volume='0.35+0.30*sin(2*PI*t/7)':eval=frame,"
                  "afade=t=in:st=0:d=1.5,afade=t=out:st={fo}:d=1.5[a]"),
        # 风：粉噪声 + 带通 + 更慢的起伏 + 轻微音高摆动
        "wind": ("anoisesrc=color=pink:amplitude=0.5:d={d}[n];"
                 "[n]highpass=f=250,lowpass=f=1800,volume='0.22+0.18*sin(2*PI*t/11)':eval=frame,"
                 "afade=t=in:st=0:d=1.2,afade=t=out:st={fo}:d=1.2[a]"),
    }
    for kind in kinds:
        graph = recipes.get(kind)
        if not graph:
            continue
        path = os.path.join(outdir, f"ambience_{kind}.m4a")
        fo = max(0.0, d - 1.5)
        rc, err = _run([ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-filter_complex", graph.format(d=d, fo=fo),
                        "-map", "[a]", "-t", f"{d:.2f}",
                        "-c:a", "aac", "-b:a", "96k", path])
        if rc == 0 and os.path.isfile(path):
            made[kind] = path
        else:
            print(f"⚠ 环境音 {kind} 合成失败：{err[-180:]}")
    return made


def apply(video: str, spec: dict, outdir: str) -> dict:
    """给成片加音轨。spec 支持 {narration, sfx}。

    返回 detail：做了什么、没做什么（缺素材库的如实列出）。
    """
    detail: dict = {"applied": False, "narration": False, "ambience": [], "missing": []}
    if not os.path.isfile(video):
        detail["error"] = "成片不存在"
        return detail
    narration_text = str((spec or {}).get("narration") or "").strip()
    sfx_text = str((spec or {}).get("sfx") or "").strip()
    if not narration_text and not sfx_text:
        return detail                      # 没要求音频 → 保持默认无声

    out = os.path.join(outdir, "final_voiced_" + os.path.basename(video))
    tracks: list[str] = []
    if narration_text:
        vo = os.path.join(outdir, "narration.mp3")
        if synthesize_narration(narration_text, vo):
            tracks.append(vo)
            detail["narration"] = True
        else:
            detail["missing"].append("旁白（edge-tts 不可用或合成失败）")

    # 音轨时长 = 旁白时长（有旁白）或成片时长
    total = _probe_duration(tracks[0]) if tracks else _probe_duration(video)
    kinds, missing_sfx = ambience_kinds(sfx_text)
    detail["missing"].extend(missing_sfx)
    if kinds and total > 0:
        made = synthesize_ambience(kinds, total, outdir)
        tracks.extend(made.values())
        detail["ambience"] = sorted(made)

    if not tracks:
        return detail                      # 一条音轨都没做出来 → 不动成片（宁可无声，不要假声）

    ff = _ffmpeg()
    if not ff:
        detail["error"] = "没有 ffmpeg"
        return detail
    args = [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", video]
    for t in tracks:
        args += ["-i", t]
    # 画面不够长就定格补到音轨长度（不裁旁白）；旁白压过环境音
    graph = ["[0:v]tpad=stop_mode=clone:stop_duration=60[v]"]
    mix_in = []
    for i, t in enumerate(tracks):
        if i == 0 and detail["narration"]:
            graph.append(f"[{i + 1}:a]volume=1.0[a{i}]")      # 旁白原音量
        else:
            graph.append(f"[{i + 1}:a]volume=0.5[a{i}]")      # 环境音压低（约 -6dB）
        mix_in.append(f"[a{i}]")
    if len(mix_in) > 1:
        graph.append(f"{''.join(mix_in)}amix=inputs={len(mix_in)}:duration=first:normalize=0[aout]")
    else:
        graph.append(f"{mix_in[0]}anull[aout]")
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[aout]",
             "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.2f}", out]
    rc, err = _run(args)
    if rc != 0 or not os.path.isfile(out):
        detail["error"] = err
        return detail
    # 用带音轨的成片替换原成片（成片是最终交付物，路径不能变）
    os.replace(out, video)
    detail["applied"] = True
    # 预期时长按**输入**算（旁白长度）：评审拿它当基准，而不是量输出（那是自证）
    detail["expected_duration"] = round(total, 2)
    return detail
