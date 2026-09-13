"""设定表 → 视频首帧：把角色设定表（三视图/表情表）自动裁成主视觉。

为什么需要这一刀：整张设定表当首帧，生成模型会照着**版式**画成一张表格；
实测裁出上部主视觉之后，图生视频才真出戏（小石猴那轮：视觉身份判定"角色一致"）。

纯标准库 + 可选 PIL；没有 PIL 就退到 ffmpeg（本项目本来就依赖 ffmpeg）。
"""

from __future__ import annotations

import os
import subprocess

from . import probe

TARGET_RATIO = 16 / 9        # 视频首帧比例
MAX_WIDTH = 1280             # 首帧没必要更大（生成端也会自己缩放）
SHEET_TOP_FRACTION = 0.50    # 竖长设定表：取上部约一半当主视觉

MODES = ("auto", "crop_top", "keep")
MODE_LABELS = {
    "auto": "自动（设定表取上部主视觉，单张画面原样保留）",
    "crop_top": "强制只取上部 16:9",
    "keep": "原样不裁",
}


def crop_box(size: tuple[int, int], mode: str = "auto") -> tuple[int, int, int, int]:
    """算出裁切框 (left, top, right, bottom)。纯函数，可单测。

    auto 的判据：够宽（宽高比 ≥ 16:9）的图本身就是"一帧画面" → 原样；
    近方形/略竖的多层版式（典型就是设定表）→ 取上部 16:9；很竖 → 取上部一半。
    """
    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        raise ValueError(f"图片尺寸不合法: {size}")
    if mode not in MODES:
        raise ValueError(f"未知裁切模式: {mode}（可选 {'/'.join(MODES)}）")
    if mode == "keep":
        return (0, 0, w, h)
    if mode == "crop_top":
        return (0, 0, w, min(h, max(1, round(w / TARGET_RATIO))))
    if w / h >= TARGET_RATIO:
        return (0, 0, w, h)
    if w / h >= 1.0:
        return (0, 0, w, min(h, max(1, round(w / TARGET_RATIO))))
    return (0, 0, w, min(h, max(1, round(h * SHEET_TOP_FRACTION))))


def _load_size(src: str) -> tuple[int, int]:
    if not os.path.isfile(src):
        raise FileNotFoundError(src)
    try:                                   # 先试 PIL（准，且能处理 exif 旋转）
        from PIL import Image
        with Image.open(src) as im:
            return im.size
    except Exception:
        pass
    m = probe.probe_container(src) or {}
    if not m.get("width") or not m.get("height"):
        raise ValueError(f"读不出图片尺寸（既没有 PIL 也探不到元数据）: {src}")
    return int(m["width"]), int(m["height"])


def normalize(src: str, dst: str, mode: str = "auto") -> dict:
    """把 src 变成"能当首帧"的图写进 dst：裁主视觉 + 缩到 ≤1280 宽 + 存 JPEG。

    返回 {path, box, src_size, out_size, mode, engine}——面板/日志把里头的数值照实显示。
    """
    box = crop_box(_load_size(src), mode)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    engine = "pillow"
    try:
        from PIL import Image
        with Image.open(src) as im:
            out = im.convert("RGB").crop(box)
            if out.width > MAX_WIDTH:
                # Pillow 10 把常量挪进了 Image.Resampling：写死任一个都会在另一个版本上炸
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
                out = out.resize((MAX_WIDTH, max(1, round(out.height * MAX_WIDTH / out.width))),
                                 resample)
            out.save(dst, "JPEG", quality=92)
            out_size = out.size
    except ImportError:
        engine = "ffmpeg"
        w, h = box[2] - box[0], box[3] - box[1]
        vf = (f"crop={w}:{h}:{box[0]}:{box[1]},"
              f"scale='min({MAX_WIDTH},iw)':-2")
        p = subprocess.run([probe.ffmpeg_path() or "ffmpeg", "-y", "-v", "error",
                            "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", "3", dst],
                           capture_output=True, text=True)
        if p.returncode != 0 or not os.path.isfile(dst):
            raise RuntimeError(f"ffmpeg 裁图失败: {p.stderr.strip()[:200]}")
        out_size = _load_size(dst)
    return {"path": dst, "box": box, "src_size": _load_size(src), "out_size": out_size,
            "mode": mode, "engine": engine}


def save_reference(project, kind: str, key: str, src: str, mode: str = "auto",
                   name: str | None = None, **meta) -> dict:
    """把一张图登记成项目的参考图（角色/场景）——面板"上传参考图"与 CLI --set-ref 共用。

    落在 <项目目录>/refs/<kind>_<key>.jpg：路径稳定，且随项目档案一起走。
    """
    if kind not in ("character", "scene"):
        raise ValueError(f"kind 只能是 character/scene，收到 {kind}")
    key = (key or "").strip()
    if not key:
        raise ValueError("参考图必须给一个键名（镜头约束里的角色名/场景名）")
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in key)[:48] or "ref"
    dst = os.path.join(project.dir, "refs", f"{kind}_{safe}.jpg")
    info = normalize(src, dst, mode)
    project.register_asset(kind, key, dst, name=name or key, **meta)
    info.update({"kind": kind, "key": key, "name": name or key})
    return info
