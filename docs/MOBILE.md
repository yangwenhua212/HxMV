# 手机上跑 HxMV

**结论：可以。** 而且 HxMV 天生适合端侧——零第三方 Python 依赖、3573 行、664KB，
唯一的原生依赖是一个 ffmpeg 二进制。真正的重活（AI 生成）在云上，手机只当"导演"。

## 三种形态（按代价从低到高）

| 形态 | 手机负责 | 算力在哪 | 现在能用吗 | 代价 |
|---|---|---|---|---|
| ① **手机当遥控器** | 浏览器开面板、下产物 | 服务器 | ✅ 现在就行 | 0 |
| ② **手机上真跑内核**（Termux） | 规划/渲染/拼接/测量/调云 API | 手机 + 云 | ✅ 10 分钟装好 | 装 Termux；后台常驻要设省电白名单 |
| ③ **打成 APK（真 App）** | 同 ②，但有图标可分发给别人 | 手机 + 云 | ⚠ 要专门做（1-3 天） | python-for-android + 打包 ffmpeg；本机内存不够，得在 OVH 上构建 |

## 事实依据（不是估计）

| 项 | 实测 |
|---|---|
| 第三方 Python 依赖 | **无**（`import` 全是标准库，逐文件核过） |
| 代码量 | 3573 行 / 664KB（搬运成本≈0） |
| 原生依赖 | `ffmpeg` + `ffprobe`（Termux 一条命令装） |
| 手机要不要 GPU | **不要**：接云 provider 时模型在云端跑，手机只做规划 + HTTP + 拼接 + 测量 |
| 安卓 ffmpeg 没编 x264 怎么办 | 已加退让：`probe.encoder_args()` 检测不到 libx264 就退 mpeg4，**不会跑不起来** |
| 手机上渲染耗电/发热 | 出片时 ffmpeg 编码是唯一 CPU 重活；5 秒 720p 镜头量级可接受，但建议插电跑长片 |

## Termux 上装（6 步）

1. **装 Termux**：从 F-Droid 或 GitHub Releases 下 APK（小米应用商店没有；酷安上也有）。
   不要用 Google Play 的老版本。
2. **装依赖**：`pkg update && pkg install python ffmpeg git`
3. **取代码**：`git clone https://github.com/yangwenhua212/HxMV.git && cd HxMV`
4. **自检**（本地真渲染，1-2 分钟）：
   `python3 -m hxmv --provider local --out ~/storage/shared/HxMV/试跑 "一只柯基在雪地里打滚"`
5. **配 Key + 跑真 AI 视频**：
   `python3 -m hxmv --set-key zhipu <KEY>`
   `python3 -m hxmv --provider zhipu --project 柯基短剧 --episode 1 "第1集：柯基在雪地里打滚"`
6. **手机里起面板**：`python3 -m hxmv.server --port 8668` → 浏览器开 `http://127.0.0.1:8668`

一键版（把上面 2-6 步串起来）：`bash scripts/install-termux.sh`（可从 GitHub raw 直接管道执行）。

## 手机端的坑（都踩过或已规避）

| 坑 | 处理 |
|---|---|
| MIUI/澎湃把 Termux 后台杀掉，跑到一半没了 | `termux-wake-lock` 锁唤醒 + 设置→应用→Termux→省电策略→**无限制** |
| 产物在 app 私有目录里，相册看不到 | `termux-setup-storage`，产物写到 `~/storage/shared/HxMV/`（相册可见） |
| 安卓 ffmpeg 构建缺 libx264 | 已做编码器退让（mpeg4 兜底），跑不起来的风险已消除。**但实测**：mpeg4 压缩更糊 → 一致性测量偏低（0.78~0.84 vs x264 的 0.91+）→ 闭环会多花 2-3 次修正尝试才通过（仍能过，只是慢）。所以还是建议 Termux 的 `pkg install ffmpeg`（带 x264） |
| 手机端没有 1.6G 内存服务器那些限制 | 反过来说：**别拿 1.6G 的机器构建 APK**（要 Gradle/NDK），走 OVH |
| 手机断网/切后台 → 云 provider 轮询中断 | 云任务已提交，重跑同一集时**项目档案命中指纹 → 直接复用已生成的画面**，不重复花额度 |

## 形态③（真 APK）要做什么

- 用 python-for-android（或 Chaquopy 嵌 Python 进 Android 工程）把 `hxmv` 包进去
- 打包 ffmpeg 静态库（每个 ABI ~40-80MB）或首次启动时下载
- 面板已经是单文件 HTML + 纯 stdlib HTTP server → 直接用 WebView 套壳即可
- 构建环境：本机 1.6G 内存跑不了，走 OVH（java21 现成）

这条路值不值得走，取决于"要不要给别人的手机装"。如果只是自己用，形态②（Termux）已经足够，
而且保留了完整的 CLI 能力（改造、调试、加 provider 都不用重新打包）。
