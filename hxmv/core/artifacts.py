"""把"本次 run 真正用到的成品"归拢到一起。

为什么需要：项目档案复用画面时，文件在**旧的**产物目录里，本次 run 的目录是空的。
于是面板的产物列表、客户端的推送、分集产物清单都会变成"什么都没产出"——
而实际上这集片子是齐的（只是复用旧文件）。实测踩过。

做法：从每个已完成任务的结果里取真实文件路径（media/output），不在本次产物目录里的
就**硬链接**过来（同一文件系统零成本），失败则**拷贝**（跨文件系统兜底）。
"""
from __future__ import annotations

import os
import shutil

MEDIA_EXT = (".mp4", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3")
MAX_FILES = 24


def _is_media(path: str) -> bool:
    return bool(path) and path.lower().endswith(MEDIA_EXT) and os.path.isfile(path)


def consolidate_artifacts(state, artifacts_dir: str) -> list[str]:
    """返回本次 run 的成品绝对路径清单（成片在前，其次镜头，最后参考图）。"""
    paths: list[str] = []
    for t in state.completed:
        r = t.result or {}
        for key in ("output", "media", "asset"):
            p = r.get(key)
            if isinstance(p, str) and _is_media(p) and p not in paths:
                paths.append(os.path.abspath(p))
                break
    if not artifacts_dir:
        return paths
    try:
        os.makedirs(artifacts_dir, exist_ok=True)
    except OSError:
        return paths

    out: list[str] = []
    for p in paths[:MAX_FILES]:
        dest = os.path.join(artifacts_dir, os.path.basename(p))
        if os.path.abspath(p) == os.path.abspath(dest):
            out.append(p)
            continue
        if not os.path.exists(dest):
            try:
                os.link(p, dest)          # 同一文件系统：零成本
            except OSError:
                try:
                    shutil.copy2(p, dest)  # 跨文件系统：拷贝兜底（产物通常几百 KB～几 MB）
                except OSError:
                    continue
        out.append(dest)
    # 成片排最前，其余按名字稳定排序
    out.sort(key=lambda x: (0 if os.path.basename(x).startswith("final") else 1,
                            os.path.basename(x)))
    return out
