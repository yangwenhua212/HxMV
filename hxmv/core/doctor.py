"""部署自检（doctor）：一条命令回答"这台机器能不能跑 HxMV、缺什么、怎么补"。

给谁用：
    · 手机端（Termux）装完后自检——避免"装了但跑不起来"的挫败
    · HxSync 等客户端把结果直接渲染成"部署向导"页面
    · 出问题时先跑它，省得靠猜

返回结构化的检查项（name/ok/detail/fix），文本渲染只是其中一种用法。
"""
from __future__ import annotations

import os
import socket
import sys
import urllib.error
import urllib.request

from ..media import probe
from . import config

DATA_DIR = os.path.expanduser("~/.hxmv")


def run_checks(probe_network: bool = True) -> list[dict]:
    checks: list[dict] = []

    v = sys.version_info
    checks.append({
        "name": "Python", "ok": v >= (3, 10), "detail": f"{v.major}.{v.minor}.{v.micro}",
        "fix": "" if v >= (3, 10) else "需要 Python 3.10+（Termux: pkg install python）",
    })

    has_ff = probe.has_ffmpeg()
    checks.append({
        "name": "ffmpeg/ffprobe", "ok": has_ff,
        "detail": (probe.ffmpeg_path("ffmpeg") or "缺失") if has_ff else "缺失",
        "fix": "" if has_ff else "Termux: pkg install ffmpeg ｜ Ubuntu: apt install ffmpeg",
    })
    if has_ff:
        enc = probe.encoder_name()
        checks.append({
            "name": "视频编码器", "ok": True, "detail": enc,
            "fix": "" if enc == "libx264" else "只有 mpeg4：能跑，但闭环会多花几次修正（画质更糊）",
        })

    writable = True
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        t = os.path.join(DATA_DIR, ".write_test")
        with open(t, "w") as f:
            f.write("ok")
        os.remove(t)
    except OSError as e:
        writable = False
        detail = str(e)
    checks.append({
        "name": "数据目录可写", "ok": writable, "detail": DATA_DIR if writable else detail,
        "fix": "" if writable else "检查目录权限/磁盘空间（df -h）",
    })

    zhipu = config.api_key("zhipu")
    checks.append({
        "name": "智谱 Key（真 AI 视频）", "ok": bool(zhipu),
        "detail": config.mask(zhipu) if zhipu else "未配置（只能用本地渲染/模拟世界）",
        "fix": "" if zhipu else "python3 -m hxmv --set-key zhipu <KEY>（bigmodel.cn 免费申请）",
    })

    free_port = True
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 8668))
    except OSError:
        free_port = False
    checks.append({
        "name": "面板端口 8668", "ok": free_port,
        "detail": "可用" if free_port else "已被占用（可能已经在跑）",
        "fix": "" if free_port else "换端口：python3 -m hxmv.server --port 8669",
    })

    if probe_network:
        net = False
        try:
            with urllib.request.urlopen("https://open.bigmodel.cn", timeout=4):
                net = True
        except (urllib.error.URLError, OSError):
            net = False
        checks.append({
            "name": "外网可达（云生成）", "ok": net,
            "detail": "通" if net else "不通（只能用本地渲染）",
            "fix": "" if net else "检查网络/代理；纯本地跑可忽略这一项",
        })
    return checks


def render(checks: list[dict]) -> str:
    ok_all = all(c["ok"] for c in checks)
    lines = ["HxMV 部署自检", "─" * 52]
    for c in checks:
        lines.append(f"  {'✅' if c['ok'] else '⚠ '} {c['name']:<22} {c['detail']}")
        if not c["ok"] and c.get("fix"):
            lines.append(f"     → {c['fix']}")
    hard = [c for c in checks if not c["ok"] and c["name"] in ("Python", "ffmpeg/ffprobe",
                                                              "数据目录可写")]
    lines.append("─" * 52)
    if not hard:
        lines.append("  结论：✅ 可以跑（本地渲染闭环 + 面板都行）"
                     + ("" if ok_all else "；标⚠的项只影响对应能力"))
    else:
        lines.append(f"  结论：❌ 还跑不了，先修：{'、'.join(c['name'] for c in hard)}")
    return "\n".join(lines)
