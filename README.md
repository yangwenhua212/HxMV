# HxMV

<p align="center">
<b>自主内容生产智能体 · Autonomous Content Agent</b><br/>
规划 → 执行 → 观察 → 判断 → 修正
</p>

<p align="center">
<a href="https://github.com/yangwenhua212/HxMV"><img alt="GitHub" src="https://img.shields.io/github/stars/yangwenhua212/HxMV?style=social"></a>
<img alt="Python" src="https://img.shields.io/badge/Python-3.10+-blue?logo=python">
<img alt="License" src="https://img.shields.io/badge/License-MIT-green">
<img alt="Deps" src="https://img.shields.io/badge/dependencies-zero-orange">
</p>

**Hx = 个人 AI 生态前缀（HxSync 同族），MV = Media & Video。**
视频/图片/配音只是第一个应用场景——真正可复用的是一个控制循环，不是某家生成 API 的调用代码。

> **HxMV 是一人一实例的 AI——谁部署它，谁就是它唯一的主人。**
> 像把一个 AI 部署到自己服务器：出生是一张白纸（零记忆），大脑（`~/.hxmv/brain.json`）
> 只存在你自己的机器上。它只为你积累经验、只学你的偏好、只听你的指令，越用越懂你。
> 别人想用 HxMV？那就自己部署一个——每个实例从零开始，只属于它的主人。
> **没有中心账号、没有云端大脑：部署即拥有，隔离是天然的。**

> 你的系统应该像团队一样工作：**LLM 是策划，代码是执行，Critic 是质检，Refiner 是返工师傅，Brain 是老师傅的记忆**。


## 核心思想

```python
while not goal_reached:
    plan        = planner(state)       # LLM 想做什么
    result      = executor(plan)       # 代码怎么做
    observation = critic(result)       # 系统观察结果（眼睛）
    state       = controller(state, plan, result, observation)  # 判断 → 推进/修正/归档
```

**边界铁律：LLM 负责"想做什么"，你的代码负责"到底怎么做"。**
Planner 只输出结构化 Task；执行/检测/判断/调参全部是确定性代码。

## 能力（已可跑）

| 模块 | 说明 |
|---|---|
| `core/state.py` | Task / ExecutionState / Budget——LLM 与代码之间的结构化契约 |
| `core/planner.py` | LLM 规划（OpenAI 兼容端点）；失败自动降级内置 Mock 规划。两种规划都**带着大脑记忆起手** |
| `core/executor.py` | Mock 执行器：按参数概率注入真实感缺陷（物理/一致性缺陷概率都与参数挂钩，可被修好） |
| `core/critic.py` | **三层 Critic**：L1 物理 / L2 视觉(一致性) / L3 语义(LLM 可选)，合并为 QualityReport |
| `media/probe.py` | **真眼睛（v0.4）**：ffprobe/ffmpeg 从**真实媒体**里量出分辨率/帧率/时长/音量/黑帧/静止/外观一致度 |
| `core/refiner.py` | 按 failures→suggestions 调参重投；**优先查质量记忆里的历史成功修正** |
| `core/controller.py` | 判定 PASS/RETRY/FAIL；PASS 时把验证有效的修正回写质量记忆 |
| `core/brain.py` | **大脑**：持久记忆，自动注入/自动回写，会遗忘（详见下） |
| `core/project.py` | **项目档案（v0.5）**：跨 run 记住风格/角色/场景/已生成画面与分集——续做不重画 |
| `providers/local_render.py` | **真渲染 provider（v0.4）**：用系统 FFmpeg 真出片（资产/镜头/成片都落盘），参数真的决定可测质量 |
| `providers/zhipu_video.py` | **真 AI 视频（v0.6）**：智谱 CogVideoX-Flash（免费）文/图生视频，角色参考图当首帧锁角色 |
| `core/config.py` | 本地凭据：`~/.hxmv/config.json`（权限 600），`--set-key` 一次配好，CLI 与 Web 共用 |
| `providers/` | Provider 接口 + 可灵接入骨架 + fake 仿真（见 `docs/PROVIDERS.md`） |
| `server.py` | **Web 控制台 daemon**（纯 stdlib）：SSE 实时事件流 + run 存档 + 产物取回 + 单文件面板 |

### v0.4「真产物 + 真眼睛」：哪部分是真的

| 环节 | 真不真 | 说明 |
|---|---|---|
| 控制闭环（规划/执行/观察/判断/修正） | **真** | 每轮推进都改 state，可审计可回放 |
| 产物 | **真** | `local` provider 用 FFmpeg 真渲染 mp4/png 到磁盘，能播放；`kling` 等接真实生成服务 |
| 质量检测 | **真测量** | L1 用 ffprobe 量分辨率/帧率/时长/音量、blackdetect 量黑帧、freezedetect 量静止；L2 与参考图做**真像素**外观比对（16×16 感知指纹） |
| 修正是否有效 | **真因果** | 参数→可测指标的映射是真的：升分辨率→量出高度变化；加增益→量出音量变化；提参考强度→量出外观一致度变化 |
| AI 生成模型 | **不是** | `local` 是 **FFmpeg 合成的仿真生成器**，用来在没有付费生成 API 时端到端验证"真产物+真检测"。画面是合成图案，不是 AI 画的 |

**一句话**：`local` 造的是"一个脾气很差、但参数确实能调好的低端生成器"——闭环在它身上做的事，
与将来接真实 AI 生成服务时要做的事**是同一套**（Provider 接口不变，换了 provider 就行）。

**大脑（持久记忆）**——像 Hermes 记忆一样"直接用"，但容量不受小预算限制：
- 持久化到 `~/.hxmv/brain.json`，进程退出不丢，跨目标/跨项目复用
- **自动注入**：每次 run 开始，把"重要 + 与当前目标相关"的经验自动注入 Planner 上下文
- **自动回写**：闭环里 FAIL→修正→PASS 的经验自动写入，无需手动记忆
- **真的变快**（实测同一目标连跑，`--provider local`）：

| 第 N 跑 | 总尝试 | 每镜头尝试 | 起手记忆 | 耗时 |
|---|---|---|---|---|
| 1（空大脑） | 12 | 4 / 4 | — | 63s |
| 2 | 8 | 2 / 2 | 参考强度 0.60 + 学到的参数 | 35s |
| 3 | 8 | 2 / 2 | 参考强度 0.70 | 35s |
| 4 | **6（理论下限：6 个任务各 1 次）** | 1 / 1 | 参考强度 0.80 | 24s |

（第 3 跑多花 1 次的尝试是因为这个生成器仍随机带片头黑场——不是记忆没用，是它还在踩新坑）

学到的不是玄学，是**这个生成器的脾气**：「它默认 640x360/15fps/音轨 -45dB/爱加片头黑场/参考强度低于 0.8 就偏色」
——下次起手就按量出来好用的参数走，试错次数直接砍半。

**质量记忆**（跨镜头复用）：每个失败原因记录"哪个修正方向被验证成功过"，
后续同类失败优先复用——系统越用越懂自家生成器的脾气。这是项目壁垒，不是套壳。

**项目档案（v0.5）——"记得这一系列的生成"**，做动画短剧这类连续作品的关键：

- `~/.hxmv/projects/<项目名>/project.json` 记住：**风格**、**角色/场景参考图**、
  **已生成的每一个画面**（含当时用的参数）、**每一集**做到哪了
- 续做时先读档案：角色/场景**直接复用同一张参考图**（不重新生成 → 角色长相不漂移）；
  这一集这个镜头做过 → **直接引用旧文件，一张新画面都不生成**
- 判据是 **画面指纹**（prompt/时长/分辨率/帧率/参考强度/种子/角色/场景/风格）+
  **剧情身份**（同一集同一镜头）——后者优先于大脑的泛化经验，避免"参数慢慢漂、每次都重画"

同一部片子连续做四次的实测（`--provider local`）：

| 跑法 | 结果 | 新生成的画面 | 耗时 |
|---|---|---|---|
| 第 1 集（新建项目） | 全部通过，11 次尝试 | 角色图 + 场景图 + 2 镜头 + 成片 | ~57s |
| 第 2 集（续做，新剧情） | 全部通过，8 次尝试 | **角色/场景图复用**，只有 2 个新镜头 | 36s |
| 第 2 集再跑一次（同一集） | 全部通过，**6 次尝试全是一次过** | **0 个新文件**（指纹全命中） | **10s** |
| 第 3 集（又一集新剧情） | 全部通过，6 次尝试 | 角色/场景图复用，2 个新镜头 | 23s |

## 快速开始

```bash
# 零依赖、无需任何 API Key：Mock 世界跑通闭环
python3 -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟"

# 真产物 + 真检测（需要系统装了 ffmpeg；产物落在 ~/.hxmv/artifacts/）
python3 -m hxmv --provider local "雪地里的柯基在打滚，5 秒短片"
python3 -m hxmv --provider local --out /tmp/film "指定产物目录"

# 清空大脑从零跑（看学习曲线）／指定大脑文件
python3 -m hxmv --fresh --provider local "同一个目标"
python3 -m hxmv --brain /path/brain.json "..."

# 开一部"片子"并续做（项目档案：记住风格/角色，续做不重画已有画面）
python3 -m hxmv --provider local --project 柯基短剧 --episode 1 --style cinematic \
    "第1集：柯基在雪地里打滚"
python3 -m hxmv --provider local --project 柯基短剧 --episode 2 \
    "第2集：柯基跑到海边看浪"          # 角色图复用，只生成本集新镜头
python3 -m hxmv --list-projects        # 看有哪些项目、做到第几集

# 接真实 AI 视频生成（智谱 CogVideoX-Flash，免费；支持图生视频→角色一致）
python3 -m hxmv --set-key zhipu <你的KEY>     # 一次配好，存 ~/.hxmv/config.json（600）
python3 -m hxmv --key-status                  # 确认
python3 -m hxmv --provider zhipu --project 柯基短剧 --episode 1 "第1集：柯基在雪地里打滚"

# 接真实生成服务（Provider 层，fake 仿真无需 key 可跑）
HXMV_PROVIDER=fake python3 -m hxmv "雪地里的柯基"
# HXMV_PROVIDER=kling HXMV_KLING_KEY=sk-xxx python3 -m hxmv "..."   # 真实服务

# 接真 LLM（Planner 规划 + L3 语义评审自动启用；失败自动降级）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export HXMV_LLM_MODEL=deepseek-chat
python3 -m hxmv "30 秒产品宣传片，现代极简风"
```

标准库 only，Python 3.10+；`ffmpeg`/`ffprobe` 是**可选**运行时依赖（只用 `local` provider 与真检测时需要，没装就自动回落 Mock 世界）。

## Web 控制台（浏览器可视化闭环）

纯 stdlib daemon，零第三方依赖，浏览器里实时看闭环全过程：

```bash
python3 -m hxmv.server            # 默认 http://127.0.0.1:8668
python3 -m hxmv.server --host 0.0.0.0 --port 8668   # 局域网/公网访问
```

- **提交目标** → 选 provider（本地真渲染 / 模拟世界 / 线上仿真 / 可灵）→ 实时流出 任务 → 三层评审 → 修正重试 → 完成
- 每步展示 L1/L2/L3 分层分数、failures→suggestions 修正对，以及**实测证据**（如「实测 1280x720@30fps 音量-35.1dB / 与参考图外观一致度 0.933」）
- 完成后的**真实产物可直接在面板点开播放/下载**（`GET /api/artifact?run_id=…&name=…`）
- 侧栏实时显示大脑沉淀（LESSON 升华），底部历史 run 点击即回放
- 事件存档：`~/.hxmv/runs/<id>/events.jsonl`（可审计、可回放）
- 公网：设 `HXMV_WEB_TOKEN` 后所有 `/api/*` 需 token（header 或 `?token=`），面板 URL 带一次即记住

## 路线

- **V0.1** ✅ 自主闭环内核（Mock 世界）
- **V0.1.1** ✅ 大脑（Brain）：持久记忆、自动注入/回写、会遗忘
- **V0.2** ✅ Provider 层：`VideoProvider` 接口 + 可灵接入骨架 + fake 仿真（`docs/PROVIDERS.md`）
- **V0.3** ✅ Web 控制台：事件化内核 + stdlib daemon + 单文件面板 + 产物取回
- **V0.4** ✅ 真产物 + 真眼睛：FFmpeg 真渲染 + ffprobe 真测量 + 真像素一致性 + 参数学到的经验起手
- **V0.5** ✅ 项目档案：风格/角色/已生成画面跨 run 记忆 + 剧情身份复用（续做不重画），Web 端可指定项目
- **V0.6** ✅ 真 AI 视频接入：智谱 CogVideoX-Flash（免费）文/图生视频 + 角色参考图当首帧 + Key 本地配置（`--set-key`）+ 仿真端点自测
- **V0.7** 📋 资产一致性深化（角色参考图版本管理、跨镜头锁脸、视觉模型抽帧比对）、checkpoint 人工审批点（高成本高主观产物必须有人把关）
- **V0.8** 📋 扩展到 Research / Coding / Design Agent——复用同一个控制内核

## 设计文档

架构图、判据阈值标定与决策说明见 [DESIGN.md](DESIGN.md)。

## License

MIT
