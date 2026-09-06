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
      │ OpenCV/FFmpeg│  │ 视觉模型    │  │ LLM/VLM    │
      │ 清晰/黑帧/FPS │  │ 一致性/构图 │  │ 剧本/情绪/连续性│
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

2. **结构化 Task 是 LLM 与代码的契约**
   LLM 输出 Task（action/input/constraints/quality/retry_policy），不直接控制工具。
   自由文本无法被 Critic/Controller 程序化消费。

3. **Refiner 收敛护栏**
   视频生成是随机过程，"调参→重生成"不保证单调变好。V0.1 就内置：
   - seed 确定性（同一任务同一 attempt 产出同一结果，可复现）
   - 每任务 max_attempts + 全局 Budget（预算耗尽立即停，输出已完成的成果）
   - 质量记忆（失败→验证过的修正方向），避免重复踩坑

4. **质量记忆 = 项目壁垒**
   每次 PASS 把"失败原因 → 成功修正 → 分数"回写记忆；Refiner 优先复用历史成功方向。
   系统越用越懂自家生成器的脾气——这是抄不走的部分（与 EraHerm 反馈进化同源）。

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
│   ├── __main__.py      # CLI
│   ├── quality.py       # QualityReport
│   └── core/
│       ├── state.py     # Task / ExecutionState / Budget
│       ├── planner.py   # Planner（LLM + Mock 降级）
│       ├── executor.py  # MockVideoExecutor（缺陷注入）
│       ├── critic.py    # L1/L2/L3 + PipelineCritic
│       ├── refiner.py   # 调参重投 + 记忆查询
│       ├── controller.py# PASS/RETRY/FAIL + 记忆回写
│       ├── context.py   # 动态压缩
│       └── loop.py      # Autonomous Control Loop
├── README.md
└── DESIGN.md
```

## 边界（勿越界）

- Critic **只观察不改**——改东西是 Refiner/Controller 的事
- Planner **只提方案不执行**——执行是 Executor/Controller 的事
- Controller **只判断不动参数**——参数调整是 Refiner 的事
- 一切以 `state` 为准：闭环的每次推进都改变 state，state 可审计、可续跑、可回放
