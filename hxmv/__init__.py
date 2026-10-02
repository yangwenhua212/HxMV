"""HxMV — 自主规划 → 执行 → 观察 → 判断 → 修正 的内容生产控制内核。

v0.1 闭环内核（Mock 世界）→ v0.2 Provider 层 → v0.3 Web 控制台
→ v0.4 真产物 + 真眼睛（FFmpeg 真渲染、ffprobe 真测量、真像素一致性）
→ v0.5 项目档案（续做不重画）→ v0.6 真 AI 视频接入 + 客户端友好
→ v0.7 真视觉闭环 + 参考图入口 + 基准集跑分
→ v0.8 判据扩容（模糊/切换/静音）+ 人工审批点 + 多镜头并行
→ v0.9 自己写剧情（编剧）+ 会运镜（规格/落点声明/真判据）+ 接缝转场
→ v0.10 多 API（注册表 + 公共实现 + 薄适配器；智谱/Agnes 两家）+ 面板按注册表渲染。
"""
__version__ = "0.10.0"

# 统一 UA：Cloudflare 之类的 WAF 会 403 掉 "Python-urllib" 的默认 UA（实测踩过：
# 产物链接 curl 得 200、python urllib 拿 403）。所有对外请求都带上这个。
USER_AGENT = f"HxMV/{__version__} (+https://github.com/yangwenhua212/HxMV)"
