"""HxMV — 自主规划 → 执行 → 观察 → 判断 → 修正 的内容生产控制内核。

v0.1 闭环内核（Mock 世界）→ v0.2 Provider 层 → v0.3 Web 控制台
→ v0.4 真产物 + 真眼睛（FFmpeg 真渲染、ffprobe 真测量、真像素一致性）
→ v0.5 项目档案（续做不重画）→ v0.6 真 AI 视频接入 + 客户端友好。
"""
__version__ = "0.6.0"

# 统一 UA：Cloudflare 之类的 WAF 会 403 掉 "Python-urllib" 的默认 UA（实测踩过：
# 产物链接 curl 得 200、python urllib 拿 403）。所有对外请求都带上这个。
USER_AGENT = f"HxMV/{__version__} (+https://github.com/yangwenhua212/HxMV)"
