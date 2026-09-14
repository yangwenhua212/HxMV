#!/usr/bin/env python3
"""开源前自检：仓库里不该出现的东西，一次扫干净。

为什么要有它（不是多此一举）：这类事故的特点是**提交那一刻没人看得见**——
面板令牌（`panel.env`）、大脑记忆（`brain.json`）、项目档案里的本机绝对路径、
粘进示例里忘了删的 API Key，混在几十个源码文件里 review 时根本不会注意；
等发现时它已经在公开仓库的**历史**里了（改一次还要重写历史）。

所以把它做成一道门：CI 里命中就红，逼你在合并前处理掉。

跑法：
    python tools/check_secrets.py              # 扫 git 跟踪的文件（推荐，避开产物与缓存）
    python tools/check_secrets.py hxmv docs    # 只扫指定路径
退出码：发现疑似私密信息 = 1（可直接当 CI 门禁），否则 0。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# 每条都是"出现在公开仓库里就是事故"的东西。宁可偶发误报（人工扫一眼就能排除），
# 也不要漏报——漏报的代价是令牌/个人目录结构永久留在公开历史里。
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("API Key（sk-…）", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("GitHub / Google token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_\-]{30,}")),
    ("本机绝对路径（Windows 用户名）",
     re.compile(r"[Cc]:[\\/](?:Users|WINDOWS)[\\/][\w\u4e00-\u9fa5]+")),
    ("home 绝对路径（/home 或 /Users）", re.compile(r"/(?:home|Users)/[\w\u4e00-\u9fa5]+/")),
    ("私人邮箱（QQ/163/126/Gmail 等）",
     re.compile(r"[\w.+-]+@(?:qq|gmail|163|126|outlook|foxmail)\.[\w.]+", re.I)),
    ("私钥块", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("疑似硬编码密钥赋值",
     re.compile(r"(?i)(api[_-]?key|secret|password|passwd|access[_-]?token)\s*[=:]\s*[\"'][^\"'\s]{12,}[\"']")),
    ("手机号", re.compile(r"\b1[3-9]\d{9}\b")),
    ("内网地址", re.compile(r"\b(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)\b")),
]

# 运行时数据/凭据文件名：出现在仓库里本身就是问题（不管内容如何）
FORBIDDEN_NAMES = {
    "panel.env", "brain.json", "config.json", "project.json",
    ".env", ".env.local", "credentials.json", "id_rsa", "id_ed25519",
}
SKIP_DIRS = {".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache",
             "node_modules", ".venv", "venv"}
BINARY_EXTS = {".mp4", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".ttf", ".ttc",
               ".woff", ".woff2", ".zip", ".gz", ".db", ".pyc", ".so", ".dll", ".exe"}


def tracked_files() -> list[str]:
    """优先用 git 跟踪清单（天然排除产物/缓存）；没有 git 就递归遍历。"""
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True)
        files = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    return _walk(".")


def _walk(root: str) -> list[str]:
    found: list[str] = []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        found += [os.path.join(base, n) for n in names]
    return found


def scan(files: list[str]) -> list[tuple[str, int, str, str]]:
    """返回 [(文件, 行号, 命中类型, 片段)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for path in files:
        name = os.path.basename(path)
        if name in FORBIDDEN_NAMES:
            hits.append((path, 0, "不该进仓库的文件", name))
            continue
        if os.path.splitext(name)[1].lower() in BINARY_EXTS:
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        for label, rx in PATTERNS:
            for m in rx.finditer(text):
                hits.append((path, text[:m.start()].count("\n") + 1, label, m.group(0)[:80]))
    return hits


def main(argv: list[str]) -> int:
    files = argv[1:] or tracked_files()
    hits = scan(files)
    if not hits:
        print(f"✅ 未发现私密信息（扫描 {len(files)} 个文件）")
        return 0
    print(f"⚠ 发现 {len(hits)} 处疑似私密信息（扫描 {len(files)} 个文件）：\n")
    for path, line, label, snippet in hits:
        where = f"{path}:{line}" if line else path
        print(f"  [{label}] {where}\n      {snippet}")
    print("\n处理建议：")
    print("  · 真实密钥/令牌 → 立即吊销并改写为占位符（历史里已提交的还要清理历史）")
    print("  · 本机绝对路径/私人邮箱 → 改成通用写法（~/… / your@example.com）")
    print("  · 运行时数据文件 → 从工作区删除，并确认已被 .gitignore 覆盖")
    print("  · 若是误报 → 在 PATTERNS 里收窄规则，别直接忽略整条检查")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
