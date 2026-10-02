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

4.4 **并行只给"互相独立"的那一段，代价是事件顺序不可控**
   多镜头并行（`HXMV_PARALLEL>1`）能压缩墙钟时间，但边界必须画清楚，否则是拿正确性换速度：

   **能并行**：`GENERATE_SHOT`。镜头之间没有依赖，产物各按 `task_id` 命名互不覆盖。
   **不能并行**：`GENERATE_CHARACTER`/`GENERATE_SCENE`（共享资产缓存与档案写入）、
   `COMPOSE`（依赖全部镜头）、以及任何来自 `retry_queue` 的修正任务（修正必须立刻做）。
   **永远串行**：critic 判定、controller 状态推进、档案与大脑写回、事件序列化——
   这些都在主线程按批次顺序执行，所以 **run 的最终结果与串行执行逐字段等价**（有测试守着）。

   三处必须加的锁（不加不是"偶尔丢条记录"，而是文件写坏）：
   - `Project._lock`：并发登记镜头时 `self.shots` 的读改写；还要防 `best_for` 迭代
     一个正在被改的 dict（会直接抛 RuntimeError）
   - `Project.save` 的**原子替换**：两个线程同时 `open(path,"w")` 会把 project.json
     写成交错的坏 JSON → 下次 load 判成空档案 → 整个项目的记忆一次性归零
   - `local_render._FILE_LOCK`：参考图/基线帧/漂移图是**同名共享路径**，
     必须锁住"检查是否存在"那一刻（check-then-act 的竞态就出在检查上）

   并发还有两个"不做"的自觉：provider 必须**显式声明** `parallel_safe` 并支持重建实例
   （否则退回串行）；可灵适配器仍是骨架且并发会撞配额，所以明确不给并行——
   宁可不加速，也不给用户一个会 429 的"加速"。

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
**项目档案**解决的是另一件事（需求原话）："记得一系列的生成，比如做一个动画短剧风格以及角色，
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

## 多 API：注册表 + 公共实现 + 薄适配器（v0.10）

**设计目标（项目方向）**：HxMV 要能对接**多个 API，而不只是一家**；代价是代码得高效简洁，
不许堆——加一家 API 应该是「写一个文件 + 加一条表项」，不是「改八个地方」。

三层，各有唯一职责：

| 层 | 文件 | 管什么 | 不管什么 |
|---|---|---|---|
| **注册表** | `providers/registry.py` | 有哪几家、怎么建（模块/类）、Key 从哪来、有哪些档位（中文名 + 单价）、默认档 | 不提任何能力声明 |
| **公共实现** | `providers/api_video.py`（`ApiVideoProvider`） | 档案复用、参考图解析、首帧生成、尾帧派生、指纹与缓存、工程修正（音量/黑场）、结果登记、HTTP/轮询骨架 | 不认任何一家 API 的字段 |
| **适配器** | `zhipu_video.py` / `agnes_video.py` | **只有五处差异**：`_submit_body` / `_poll_url` / `_image_body` / `download_auth` / 能力与档位表 |

**为什么能力声明（`camera_support` / `MAX_DURATION` / `FIRST_LAST_MODELS`）放在适配器类、不放注册表**：
注册表是「配置」，能力是「模型的事实」。声明错了，闭环会照它挑运镜落点、照它钳制时长
（声明了首尾帧却做不到 → 判据判死好片），所以它必须贴着实现在一起、由改那家的人改。

**消费方一律读表，不许再写名字**：`core/executor.py`（工厂 + `_auto_provider`）、
`server.py`（`/api/config`、`/api/run` 校验、健康检查）、`__main__.py`（`--provider` / `--set-key` /
`--default` / `--key-status`）、`core/doctor.py`、`core/llm.py`（大脑跟哪家走）、
`web/index.html`（设置页每张卡由 `/api/config` 渲染）。`tests/test_providers.py` 守着这条。

**大脑跟着哪家走**（`llm.active_provider()`）：本轮 `HXMV_PROVIDER`（能出图的真 API 那家）→
面板选的默认家 → 第一个配了 Key 的家。`local`/`fake` 没有 base/Key，自动跳过——
所以 `--provider local` 跑本地渲染时，规划/评审仍用已经配好的那家 LLM，不会退回 Mock。

**免费档是免费档，别当它是「便宜的生产档」**：Agnes 免费档文本 10 次/分钟、视频排队 + 限流
（实测连投 429 / `video_queue_full`）。适配器把 429/503 都当**基础设施失败**（`retryable=True`），
闭环的熔断器会在连续失败后停下——这是对的：限流不是片子不好，不该拿 Refiner 去修。

**「免费就行不怕等」+「我让它做，它就做」（2026-10 定下）**：等待成本由**用户**承担，所以把「等」做进适配器，
而不是让用户重跑——但**等待必须发生在他要的那一刻**：

1. **原地耐心（默认）**：提交被按回（429/队列满）就退避重试（20s 起、×1.6、上限 180s）直到
   `SUBMIT_WAIT`（Agnes 默认 **2 小时**）用尽；`DEFAULT_TIMEOUT`（900s）管「已受理之后的出片等待」。
2. **低峰策略（机制保留、默认关）**：`NIGHT_FIRST` 的家才会在窗口外 park 到 `HXMV_NIGHT_WINDOW`
   再重试。曾经把它设成 Agnes 的默认，被否掉：**「不要自动凌晨，而是我让它做它就做」**——
   自动把任务挪到几小时后，用户看到的是「点了没反应」，这比等待本身更糟。要低峰行为必须显式开。

判据只有一条：**「排队/限流」才等，「参数错/鉴权错」立刻失败**（后者等着等于把 bug 藏起来）。
代价要说在前面：面板 worker 是**串行**的，一条任务原地等 2 小时会占住工位，后面的任务得排在它后面。

**多 API 之后的新风险：Key 写错家。** 单体时代只有一把 Key，写错也没处可错；多 API 之后
「写进哪一家的槽位」本身就是个坑，而且**失败是静默的**（面板说"已配 Key"、health 说 ready，
但那一家的链全 401）。所以：注册表声明 `key_pattern` 做**形状校验**（不符即拒，`force` 可强写），
doctor 做**指纹查重**（两家共用一把 = 必有一家错）。一条原则：**「已配置」不等于「能用」——
写 Key 必须打到那家真实接口验证过才算数。**

同一条政策也落在**大脑**上（`core/llm.py` 的 `_call_patient`）：模型端 429/5xx 时退避重试
（上限 `HXMV_LLM_WAIT`，默认 120s）——免费档文本只有 10 次/分钟，撞一次就让分镜退成兜底
是**白丢质量**；而 400 这类立刻抛，不许拿「等」当遮羞布。

## 编剧与运镜（v0.9）：把「会拍」拆成规格 / 落点 / 判据

2026-09 的需求原话：「相当于 hxmv 会自己写剧情和视频画面的流畅度和场景适配度，不要出来怪怪的」。
拆成三件可验证的事，缺一件都不是"会"。

### 一、编剧（`core/story.py` + `StoryPlanner`）

- 现状缺口（改前）：`STORYBOARD` 是**空壳**（executor 直接返回「镜头 1: 目标前 12 字…」），
  HxMV 只会两种活——用户贴剧本（`script.parse` 照做）或 LLM 一次性给几句镜头 prompt。
  **没有故事、没有场次、镜头之间没有叙事关系**。
- 现在：`story.write(goal, project, brain)` 让 LLM 产出**结构化分镜**
  （title/logline/scenes[]/shots[{prompt,camera,speed,duration,cast,scene}]/narration/sfx），
  `story.normalize()` 逐项校验（镜数、场次键、运镜白名单、cast 落回档案键、时长钳制），
  认不出的一律落回确定值；没有 Key 或模型不听话 → `story.fallback()` 确定性兜底并标 `written_by=fallback`。
- **两条来源共用一条生产路**：`planner.tasks_from_plan(plan, ...)` 同时服务"用户剧本"和"自己写的分镜"，
  避免"只有某条路才有的 bug"（历史教训：剧本路径空的镜头、LLM 路径漏 constraints.character）。
- 两相执行（`StoryPlanner`）：第一相发 STORYBOARD 任务（执行器真调编剧）→ 第二相按分镜派资产/镜头/成片。
  编剧失败 → 交回 `LLMPlanner`/Mock 兜底（**绝不让编剧把整次生产卡死**）。
  坑：`tasks_from_plan(include_storyboard=False)` —— 不然一次 run 会出现两个 STORYBOARD，白花一次模型调用。

### 二、运镜：规格在内核、落点在 provider、判据与模型无关

- 规格 `core/camera.py`：14 种规范名 + 中英认词（长词优先；**方向不明不猜**——只写"摇"没写左右时返回 None）
  + `strip()` 把运镜词从画面描述里剥掉（认出过才剥，否则会误删正文的"镜头"）。
- 落点声明：`VideoProvider.camera_support`；`camera.pick_strategy()` 按 native > render > first_last > prompt 挑。
  **换更强的模型 = 加一个适配器 + 一行能力声明**，规划/判据/指纹/闭环一个字不改。
- 硬落点两条：① `derive_last_frame()` 用 FFmpeg 从首帧派生尾帧（推/拉/摇/俯仰），
  以 `image_url=[首帧, 尾帧]` 提交给 CogVideoX-3 —— 模型必须从 A 走到 B；② 本地渲染按规格真画轨迹。
- **指纹**：`camera / camera_speed / camera_amount / _guard_camera` 已进 `_FP_KEYS`，
  provider 侧还把**实际落点**（`camera_realization`）算进指纹——换落点画面不同，不进指纹就会复用旧片。

### 三、判据：抽帧测运镜 + 剪辑点测接缝（都零新依赖）

- `probe.measure_camera()`：抽 10%/90% 两帧转 64×36 灰度，在候选 (缩放, 位移) 里找最能解释"从 A 到 B"
  的那组（**双线性**重采样 + 平均绝对差）。踩过的三个坑写在这里：
  1. 最近邻采样让静止镜头的缩放估计在 0.94~1.04 之间抖（比门限还大）→ 换双线性，噪声降到 ±0.01；
  2. 候选尺度必须覆盖真实幅度（推镜一整段能到 1.25）：只在 1.0 附近搜会把好镜头判成 `camera_mismatch`；
  3. `_match` 的 scale 是"取景窗口"大小，与"放大倍数"**互为倒数** —— 对外统一成 zoom>1=推近，
     免得写反了还看不出来（第一版就写反了，实测 push_in 量出 0.785）。
- `probe.camera_defects()`：方向必须一致、幅度必须够；static 与无规格不判。
- `probe.seam_defects(film, files, scenes, transition)`：**量成片本身**在剪辑点前后各 0.08s 的帧相似度，
  **只判同一场戏**的相邻镜头（换场硬切是正常手法）。为什么必须量成片：如果量两个原始镜头文件，
  "加淡入淡出"这条修正不会改变实测值 → 被收手守卫当"修正没落地"（"报了缺陷但修不动"的坑）。
  标定：同场戏正常接缝 0.891 / 坏接缝 0.772 / 坏接缝加淡入淡出 **0.905** → 门限 0.84。
- **只在有硬落点时判运镜**：provider 只能靠提示词（`realization="prompt"`）时如实降级成遥测并写明，
  不拿它把免费档能出片的镜头判死。
- **已知未修（写在这里免得下次重新发现）**：Controller 的收手守卫读的是 `result["measured"]`，
  而实测值其实在 `result["metrics"]` 里 → 「同样失败 + 同样的实测值」这条判断现在实际等价于
  「同样失败 + 同样的修正集合就收手」。现有缺陷的修正多为非物理类（提示词/参考强度），
  守卫对这些本来就不生效，所以危害有限；要它真按实测值判断，得把取值改成 `result["metrics"]`
  （会让重试判定更准，也可能让 mock 基准集的通过率略变——改的时候要重跑跑分）。

### 四、本地渲染的真轨迹（`local_render._render_shot`）

三条不变量（破坏任何一条，既有标定全部作废）：
1. **中点（50%）几何 = 居中裁切 + 该镜头自己的中点缩放** —— 推/拉的中点固定 1.15（线性/smoothstep 都过 0.5 时等于它），
   平移类用 1.35 换位移余量；`_baseline_frame(zoom=...)` 按同一个值做基线，L2 在 50% 采样才对齐（否则一致度假跌）；
2. **平移/俯仰走线性单向**（不是正弦来回）——判据要方向，来回摆的符号会自相矛盾；
3. `motion_scale≈0` 或 `camera=static` → 真静止。

实测踩过的两个 zoompan 坑：
- **`z<1` 会被钳住**（画面反而真的静止）：固定机位的"呼吸式锁机"必须写成 `z_mid ± 0.015`，不能从 1.0 往下摆；
- **慢到一定程度 freezedetect 会（正确地）判静止**：线性推镜每帧只变 0.2% → 走 **smoothstep**
  （中点斜率 1.5×、中段每帧 0.6%），中点仍是 1.15，两头平缓不突兀。

### 五、验证（改这一域要重跑的）

```bash
python3 -m unittest discover -s tests          # 63 例
python3 tools/calib_camera.py <outdir>          # 8 种运镜真渲染 → 实测缩放/位移/一致度（对照 README 的标定表）
python3 tools/calib_seam.py <outdir>            # 好接缝/坏接缝/淡入淡出 三组对照
```
端到端：`HXMV_PROVIDER=local ... python3 -m hxmv --project X "目标"` 看三行——
编剧产出（《片名》几镜/运镜）、镜头实测的「运镜缩放…」、成片的「接缝1相似度…」。
