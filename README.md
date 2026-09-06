# HxMV — 自主内容生产智能体（Autonomous Content Agent）

**规划 → 执行 → 观察 → 判断 → 修正**的自主闭环控制内核。
视频/图片/配音只是第一个应用场景——真正可复用的是那个控制循环，不是某一家生成 API 的调用代码。

> Hx = 个人 AI 生态前缀（HxSync 同族），MV = Media & Video。

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

## V0.1 能力（已可跑）

| 模块 | 说明 |
|---|---|
| `core/state.py` | Task / ExecutionState / Budget——LLM 与代码之间的结构化契约 |
| `core/planner.py` | LLM 规划（OpenAI 兼容端点）；失败自动降级内置 Mock 规划 |
| `core/executor.py` | Mock 执行器：按参数概率注入真实感缺陷（一致性缺陷与参考强度挂钩） |
| `core/critic.py` | **三层 Critic**：L1 物理(算法) / L2 视觉(一致性) / L3 语义(LLM 可选)，合并为 QualityReport |
| `core/refiner.py` | 按 failures→suggestions 调参重投；**优先查质量记忆里的历史成功修正** |
| `core/controller.py` | 判定 PASS/RETRY/FAIL；PASS 时把验证有效的修正回写质量记忆 |
| `core/context.py` | 动态上下文压缩（超 token 阈值才压，不固定"每 N 步"） |
| `core/loop.py` | Autonomous Control Loop + 执行报告 |

**质量记忆**（跨镜头复用）：每个失败原因记录"哪个修正方向被验证成功过"，
后续同类失败优先复用——系统越用越懂自家生成器的脾气。这是项目壁垒，不是套壳。

## 快速开始

```bash
# 零依赖，无需任何 API Key，Mock 世界跑通闭环
python3 -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟短视频"

# 接真 LLM（Planner 规划 + L3 语义评审自动启用；失败自动降级 Mock）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export HXMV_LLM_MODEL=deepseek-chat
python3 -m hxmv "30 秒产品宣传片，现代极简风"
```

标准库 only，Python 3.10+。

## 路线

- **V0.1** ✅ 自主闭环内核（Mock 世界）：LLM 提方案 → 执行 → 三层检测 → 修正 → 通过
- **V0.2** 📋 接真实生成器：实现 Executor 适配层（可灵/Veo 等 text_to_video），L2 换真视觉模型抽关键帧比对
- **V0.3** 📋 资产与一致管线：角色参考图资产库（Asset Manager）、checkpoint 人工审批点
- **V0.4** 📋 扩展到 Research / Coding / Design Agent——复用同一个控制内核

## 设计文档

架构图与决策说明见 [DESIGN.md](DESIGN.md)。

## License

MIT
