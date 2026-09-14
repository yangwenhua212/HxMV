# HxMV 设计文档

## 架构

```
用户需求
  │
  ▼
┌─────────────┐      ┌──────────────────────┐
│  Planner    │─────▶│    Task Queue        │
│ (LLM 想做什么)│      │ (结构化 Task 契约)     │
└─────────────┘      └──────────┬───────────┘
                                │
                                ▼
                    ┌────────────────────┐      ┌──────────────┐
                    │   Orchestrator     │─────▶│   Executor   │
                    │  (核心代码: validate/│      │ (视频/图片/配音) │
                    │   schedule/execute/ │      └──────┬───────┘
                    │   observe/evaluate/ │             │
                    │   retry/recover)    │◀────────────┘
                    └─────────┬──────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
      ┌────────────┐  ┌────────────┐  ┌────────────┐
      │ L1 物理检测 │  │ L2 视觉检测 │  │ L3 语义检测 │
      │ FFmpeg 实测  │  │ 抽帧+视觉模型│  │ 抽帧+VLM    │
      │ 清晰/黑帧/FPS │  │ 身份/场景一致│  │ 剧本/情绪/连续性│
      └────────────┘  └────────────┘  └────────────┘
              └───────────┬──────────┘
                          ▼
                 QualityReport(score, failures[], suggestions[])
                          │
              PASS        │ FAIL
              └───▶ Next ─┴────▶ Refiner（查质量记忆 → 调参重投）
```

## 关键决策

1. **三层 Critic 不设限为纯算法**
   L1 物理层（清晰度/黑帧/FPS/音量）算法可解；L2/L3（人物一致/镜头符合剧本/情绪/连续性）
   必须视觉模型 + LLM。统一输出 `QualityReport(score, failures, suggestions)`——
   分数告诉你"不好"，failures/suggestions 告诉你"怎么修"。

   **v0.7 起 L2/L3 是真看图**：`probe.extract_frames()` 从产物里抽真帧（L2 抽 3 帧 + 参考图、
   L3 抽 4 帧），`llm.chat_vision()` 以 base64 data URL 交给视觉模型（`HXMV_VLM_MODEL`）。
   **两条防假失败 / 假修的规则**（真 API 上踩出来的）：
   ① **只判给了参考的那一类**——只有角色参考图时不去问场景，否则换个背景就被判「场景不一致」，
   而它根本没有场景参考、修无可修；② **默认无声**——真 AI 视频本来就不带音轨，
   用户没要音频时"没音轨"是正常状态（判它=每条都废片）；只有任务明确要音频
   （`input.with_audio`）时缺音轨才记 `no_audio`，并用
   `enable_audio`（真给 API 传 `with_audio`）去修。同理，Controller 一旦发现
   **同样的失败 + 完全相同的实测值**就提前收手，不再重复烧额度。

   **没配视觉模型时不许假装看过**：L2 退回 16×16 像素一致度、L3 退回文字判断，
   两条兜底路径都会在 `report.detail` 里明写「未做视觉检查」，`/api/health` 与 `--doctor`
   也会报 `vision.ready=false`。抽帧是纯读操作（临时目录，用完即删），不进产物目录。

2. **结构化 Task 是 LLM 与代码的契约**
   LLM 输出 Task（action/input/constraints/quality/retry_policy），不直接控制工具。
   自由文本无法被 Critic/Controller 程序化消费。

3. **Refiner 收敛护栏**
   视频生成是随机过程，"调参→重生成"不保证单调变好。V0.1 就内置：
   - seed 确定性（同一任务同一 attempt 产出同一结果，可复现）
   - 每任务 max_attempts + 全局 Budget（预算耗尽立即停，输出已完成的成果）
   - 质量记忆（失败→验证过的修正方向），避免重复踩坑

4. **质量记忆 = 工程机制（不是壁垒）**
   每次 PASS 把"失败原因 → 成功修正 → 分数"回写记忆；Refiner 优先复用历史成功方向。
   系统越用越懂自家生成器的脾气（与 EraHerm 反馈进化同源）。
   **但机制本身别人一周能仿**：真正会变厚的是积累量——被验证过的修正、失败样本库、
   固定基准集的跑分曲线。别把工程功能写成护城河。

4.1 **经验要进"起手参数"，不能只躺在记事本里**
   跑分暴露过：记忆在记、但规划器不读它 → 首轮通过率卡死在 12%（平线）。现在两类都进起手：
   - 物理旋钮：分辨率/帧率/音量/去黑场/运动幅度（`_learnt_params` 返回 input 与 constraints 两袋种子）
   - 一致性：参考强度按**验证次数** +0.1 起步（不是按记忆条数——同源经验会合并成一条，按条数永远停第一档）
   - 语义：四条**具体**守卫（贴剧本/锁动作/锁情绪/锁衔接），点名哪类就补哪条，没点名才用笼统那条

   关键：**守卫必须是真改提示词的动作**。`rewrite_prompt_closer` 曾经只写一个没人读的开关，
   在真链路里"改了参数→指纹变了→重生成→提示词一个字没变"，白烧一次生成（和"时长落不到 API"
   "无音轨判音量低"同类）。凡是改产物的参数，都要同时进 `_FP_KEYS`，否则命中缓存 = 修了等于没修。

4.2 **一张首帧只能锁一类：另一类必须靠文字**
   图生视频的 API 只吃一张图（首帧）。所以：
   - 首帧放角色图（默认）→ 提示词必须写明「这张图只定角色长相，背景别抄它」，否则模型会把参考图的
     环境一起搬过来（实测：草地上的柯基照片 → 生成还是草地，"雪地"没出现，L3 判不符判得有理）
   - 首帧放场景图（`ref_use="scene"`）→ 反向锁角色
   - **实际用了哪类参考图要进画面指纹**：不然把 ref_use 从角色改成场景，指纹不变 → 复用旧画面
   - 给参考的那类才判定一致性（`reference_kind` 记录，critic 只问给过参考的那一类）

4.3 **判据要能分开「病」，修正方向才对得上**
   v0.8 用 ffmpeg 自带滤镜补了四类过去量不出来的缺陷（`blurdetect` / `scdet` / `loudnorm` /
   `silencedetect`，**零新依赖**）。关键不是"多量了几个数"，而是把原来混在一起的病拆开：

   - **糊 ≠ 不够清晰**：分辨率不足 → 升分辨率；对焦/细节糊 → 改提示词锁清晰度。
     两者混成一个 `low_clarity` 时，糊片会被反复升分辨率，**永远修不好**（方向从一开始就错）。
   - **模型自己剪了片**：单镜头任务里出现镜头切换 → 提示词锁"一个连续镜头"。
     判据必须带 `expect_single_shot` 门控——成片本来就由多镜头拼成，拿它判成片等于每部都判死。
   - **三种音频病**：没音轨 / 有音轨但全程静音 / 只是偏轻。前两种做增益是**空操作**
     （得让模型真的出声）。还要区分"**没测量**"与"**测出来是静音**"（loudnorm 对全静音给
     `-inf` → None）：混为一谈会让没有该指标的调用路径凭空被判 silent_audio。

   新判据同样遵守"守卫必须真改提示词"的铁律：`_guard_sharp` / `_guard_single_shot` 都进了 `_FP_KEYS`。

5. **上下文动态压缩**
   不固定"每 N 镜头压缩一次"，而是 observations 估算 token 超阈值才压缩
   （折叠最旧保留最新），由 ContextManager 统一管。

6. **大脑 = 持久记忆层（brain.py）**
   对齐 Hermes 记忆哲学但打破小容量限制：
   - 写入自动化：闭环 PASS 自动回写经验，无需手动 remember
   - 读取自动化：每次 run 自动注入"重要+相关"记忆进 Planner 上下文，无需手动 recall
   - 容量不限死：存多少都行；注入时才按（相关性×重要性）top-k 受预算约束
   - 会遗忘：importance 时间衰减、软上限淘汰低价值条目
   - 三层记忆的 HxMV 版：Brain(LESSON/FACT 常驻+检索) 是 Hermes 便签+大脑的合一，
     未来可再接 EraHerm 做跨项目大档案（L3）

## 文件结构

```
hxmv/
├── hxmv/
│   ├── __main__.py      # CLI（--provider/--out/--fresh/--brain）
│   ├── quality.py       # QualityReport
│   ├── server.py        # Web 控制台 daemon（stdlib HTTP + SSE + 产物取回）
│   ├── web/
│   │   └── index.html   # 单文件面板（零框架，中文深色）
│   ├── media/
│   │   └── probe.py     # 真眼睛：ffprobe/ffmpeg 量指标 + 判缺陷 + 外观一致度
│   ├── providers/
│   │   ├── base.py      # VideoProvider 接口 + ProviderError
│   │   ├── local_render.py  # 本地 FFmpeg 真渲染（仿真生成器，参数真影响质量）
│   │   ├── fake_api.py  # 仿真线上服务（提交/轮询/503）
│   │   └── kling_example.py # 真实生成服务接入骨架
│   └── core/
│       ├── state.py     # Task / ExecutionState / Budget
│       ├── planner.py   # Planner（LLM + Mock 降级，都带记忆起手）
│       ├── executor.py  # MockVideoExecutor（参数相关的缺陷注入）+ ProviderExecutor 投影
│       ├── critic.py    # L1/L2/L3 + PipelineCritic
│       ├── refiner.py   # 调参重投 + 记忆查询
│       ├── controller.py# PASS/RETRY/FAIL + 记忆回写
│       ├── context.py   # 动态压缩
│       └── loop.py      # Autonomous Control Loop（emit 事件旁路）
├── tests/
│   └── test_core.py     # 纯标准库单测：判据 / 指纹白名单 / 调参 / 记忆 / 评审并行
├── .github/workflows/ci.yml  # 单测 + Mock 端到端 + 真渲染冒烟（3.10/3.12/3.13）
├── pyproject.toml       # 打包与工具配置（零运行时依赖是硬约束）
├── .gitattributes       # 行尾统一 LF（Windows/Unix 协作者不再互踩假 diff）
├── README.md
└── DESIGN.md
```

## 真产物 + 真眼睛（v0.4）

### 为什么要这层

mock 世界的缺陷是 executor 按概率"贴标签"的，Critic 读标签——那不是观察，是复述；
真实生成服务不会自带质量标签，物理质量只能从像素和音轨里**量**出来。
所以 v0.4 把 L1/L2 分成两条路：

- `result["media"]/["output"]` 是**磁盘上真实存在的文件** → 调 `media/probe.py` 实测（真观察）
- 否则（mock/fake 世界）→ 读 executor 注入的缺陷标签（服务端已知问题的加速通道）

两条路产出同一个 `QualityReport`，Controller/Refiner 完全不用知道区别——**边界不变**。

### 判据阈值表（`probe.THRESHOLDS`，标定实测）

| 阈值 | 值 | 依据 |
|---|---|---|
| `min_height` | 720 | 低于 720p 判 `low_clarity`（分辨率是可稳定量出的清晰度维度） |
| `min_fps` | 24 | 低于 24 判 `fps_too_low` |
| `min_mean_volume_db` | -40 | `volumedetect` 量出的平均音量低于此判 `low_volume` |
| `black_seconds` | 0.10 | `blackdetect` 累计黑屏超此判 `black_frame`（真片头全黑段实测 0.4s） |
| `freeze_seconds` | 0.80 | `freezedetect(n=0.002)` 累计静止超此判 `frozen_frame` |
| `min_consistency` | 0.90 | 外观一致度低于此判 `character/scene_inconsistency` |
| `duration_ratio_*` | 0.75 / 1.30 | 实际/期望时长比值越界判 `too_short` / `too_long` |
| `max_blur_mean` | 12.0 | `blurdetect` 均值超此判 `blurry`。标定：清晰原片 5.13 → gblur σ=1.5 时 8.93 → σ=4 时 11.95（1280x720 合成素材）。**内容相关**，接真 AI 视频后应重标 |
| `blur_unmeasurable` | 999.0 | 极模糊时 blurdetect 输出 `nan`（梯度分母为 0）——显式转成大值，否则最该抓的那类糊会被当成"没测到" |
| `scene_cut_score` / `max_scene_cuts` | 10.0 / 0 | `scdet` 单帧得分超 10 记一次切换。标定：红→蓝硬切 15.6，连续画面 ≈0（local 两镜头拼接最大仅 2.5）；**只对单镜头任务**判 `multi_shot` |
| `min_lufs` | -40.0 | EBU R128 综合响度（`loudnorm`）低于此判 `low_volume`——与 mean_volume 任一越界即判，比单一 dB 更贴近人耳 |
| `silence_ratio` | 0.90 | `silencedetect` 静音累计占比超此判 `silent_audio`（有音轨但等于没有；增益对它无效，得让模型真出声） |

**每像素码率不进判据**：静态/低细节内容码率天然低，实测干净的 1080p 静止镜头 < 0.02 bpp 也完全清晰，
拿它当"清晰度"会把正常片子判死（踩过）。真模糊要靠帧内高频能量，属后续升级。

### L2 外观一致度怎么算（真像素）

`probe.appearance_consistency(media, baseline_frame)`：取镜头 **50% 处**的一帧，与基线帧各压成
**16×16 缩略图**，算归一化 RGB 欧氏距离 → `1 - 距离`。

三个关键细节（都是实测标定出来的）：

1. **基线要走同一条编码管线**：直接拿 PNG 参考图当基线，会掺进 h264 压掉高频细节的差异，
   零漂移也能差出 0.12——那 0.12 会被误算成"不一致"。基线 = 参考图经同分辨率/同 CRF 编码后再取帧。
2. **采样点取 50%**：避开片头黑场；且运镜的两条正弦周期成整数倍 → 50% 处平移量刚好归零，
   画面正是"居中裁切"，与基线几何完全对齐，量出来的差就只剩漂移本身（对齐前实测差 0.12，对齐后 0.03）。
3. **漂移在参考图上做一次，不逐帧做**：`noise` 是逐像素熵，1080p 逐帧跑实测 5s 片段 34MB/耗时 2 分钟；
   改在图上做一次，结果等价（帧都是这张图的运动），体积降到 1MB 内、耗时 7s。

标定曲线（720p@30，6 个不同配色 key）：

| 参考强度 | 0.4 | 0.6 | 0.8 | 1.0（零漂移） |
|---|---|---|---|---|
| 一致度区间 | 0.77–0.82 | 0.84–0.88 | 0.91–0.93 | 0.97–0.98 |

阈值 0.90 → 0.4/0.6 必失败、0.8 必通过：**"提高参考强度 0.4→0.6→0.8"这条路是量出来的、可复现的**。

### 参数 → 可测指标（闭环能收敛的前提）

| 内部语义参数 | 渲染行为 | 被哪层量出来 |
|---|---|---|
| `input.resolution` | 输出分辨率（默认 640x360） | L1 清晰度 |
| `input.fps` | 输出帧率（默认 15） | L1 帧率 |
| `input.audio_gain_db` | 音轨增益（基线 -24dB → 实测 ≈-45dB） | L1 音量 |
| `input.trim_black` | 是否去掉片头黑场 | L1 黑帧 |
| `constraints.motion_scale` | 运镜平移幅度（0 = 真静止） | L1 静止 |
| `constraints.reference_strength` | 色彩/亮度/噪声漂移（1 = 零漂移） | L2 一致度 |

同类规则也回填进了 **MockVideoExecutor**：物理缺陷概率不再是纯随机，而是挂在分辨率/帧率/增益/
`trim_black`/`motion_scale` 上——否则 Refiner 的调参永远修不好它，末次尝试随机中一个就终态 FAIL（实测踩过）。

### 运镜的坑

- 从 0 开始的余弦推镜：前 1.4s 几乎不动 → `freezedetect`（正确地）判成"画面静止"。改成**平移为主**。
- 素材太"平"（纯渐变）：平移每帧像素几乎不变，同样被正确地判成静止。参考图必须**有纹理细节**。
- 重试占**同一镜头位**：`shot_<task_id>.mp4` 按任务 id 命名 + 显式镜头位映射，
  成片永远拼"每个镜头位最新那一版"，不会把最早那版废片拼进去（踩过）。
- 片头黑场的随机判据**不含 attempts**：同一任务的"坏习惯"跨重生成是稳定的，必须显式 `trim_black` 才消除；
  否则缺陷在重试间忽有忽无，闭环学不到因果。

## 项目档案（v0.5）：让 HxMV 记得"我们在做哪部片子"

同一个目标连跑（大脑记住"这个生成器的脾气"，跨目标通用）——
**项目档案**解决的是另一件事（老大的原话）："记得一系列的生成，比如做一个动画短剧风格以及角色，
只要后续还是要做这个动画就可以接着继续做，不希望生成新画面。"

### 数据结构（`~/.hxmv/projects/<id>/project.json`）

```
{ id, title, style,
  characters: {key: {path, name, runs}},      # 角色参考图（复用即"角色长相不漂移"）
  scenes:     {key: {path, ...}},             # 场景参考图
  shots:      {指纹: {path, result, params, task_input, task_constraints, episodes[]}},
  episodes:   [{n, goal, date, outputs[]}] }  # 做到哪了
```

### 复用的两级判据（缺一不可）

1. **画面指纹**（精确）：`prompt|duration|resolution|fps|seed|reference_strength|motion_scale|
   audio_gain_db|trim_black|character|scene|style` → md5 前 12 位。命中且文件在 → **直接返回旧文件，
   跳过生成**（provider 层实现，mock/真渲染都一样）。
2. **剧情身份**（模糊，优先）：`best_for(prompt, character, scene, style)`。

**为什么要第二级**：只靠指纹会死循环——大脑的泛化经验会让起手参数慢慢变（实测 0.8 → 0.7 → 0.9），
指纹永远对不上，于是"同一集重跑"每次都在重画。**具体档案必须优先于泛化经验**：
规划阶段先查"这个镜头做过吗"，命中就把上次那版的参数盖到任务上，指纹随即命中。

### 语义边界（说清楚，别混）

- **同一集同一镜头重跑** → 指纹/身份命中 → **0 张新画面**（实测 10s 跑完，一个新文件都没有）
- **新一集新剧情** → 剧情身份不命中 → 该生成的还是生成（**本来就该是新画面**），
  但角色/场景参考图继续复用 → 画风与角色设定不漂移
- 复用的是"画面文件 + 当时参数"；生成端最终能否守住角色一致性仍取决于生成服务
  （参考图/首尾帧能力），档案保证的是"每次都喂同一张参考图 + 不重复造已有的画面"

### 工程注意

- 档案里存的是**绝对路径**（跨 run 的产物目录）；文件被删则 `prune()` / 命中校验会跳过该条
- `register_asset` 时同名 key 只归一类（早期版本把 `GENERATE_SCENE` 的 key 登记成了角色，踩过）
- 学习到的起手强度必须 `round(..., 3)`，否则日志出现 `0.6000000000000001`（踩过两次）

## Web 控制台设计（客户端）

- **事件旁路，观察不改**：`loop.run(goal, emit=cb)` 在每个关键节点（run.start / task.start /
  infra.retry / critic / decision / run.done）emit JSON 事件 dict。CLI 的 print 原样保留
  （终端是渲染器之一），事件供 daemon/Web 消费——事件 = 观察层，不干预闭环任何判断。
  订阅者抛异常被吞（`_emit` try/except），永远不打断生产。
- **critic 事件带分层**：`PipelineCritic.evaluate_layers()` 暴露 L1/L2/L3 每层报告，
  UI 才能展示「哪层挂了」而不是只有合并分。`evaluate()` 复用它 merge，老接口不变。
- **串行 worker**：daemon 单线程顺序跑 run——Brain 是单文件（`~/.hxmv/brain.json`），
  并发写会打架。多 run 提交即排队。
- **事件落盘**：每个 run `~/.hxmv/runs/<id>/events.jsonl`，追加即 flush——daemon 重启后
  历史仍在，前端可整 run 回放（`GET /api/run/<id>`）。SSE 先回放已落盘事件再实时推送，
  订阅者不会漏事件。
- **provider 选择**：daemon 提交时带 provider 参数，worker 执行前设 `HXMV_PROVIDER`
  环境变量（串行安全）；mock/fake/local/kling 四选一。
- **产物取回**：worker 同时把 `HXMV_ARTIFACTS` 指到本 run 的 `runs/<id>/artifacts/`，
  面板用 `GET /api/artifact?run_id=&name=`（同 token 防护 + 文件名白名单防目录穿越）
  直接播放/下载成片与镜头——"真产物"这件事在浏览器里就能验证。
- **产物形态**：run.done 事件带 completed/failed/预算/大脑统计，`summary()` 同源数据。
- 坑（实测）：面板 JS 里 `forEach` 回调内 `continue` 是非法的（SyntaxError 会让整个
  script 不执行，页面看起来「全没反应」）——要跳过元素用 `for...of` 循环或 `return`。

## 边界（勿越界）

- Critic **只观察不改**——改东西是 Refiner/Controller 的事
- Planner **只提方案不执行**——执行是 Executor/Controller 的事
- Controller **只判断不动参数**——参数调整是 Refiner 的事
- 一切以 `state` 为准：闭环的每次推进都改变 state，state 可审计、可续跑、可回放
