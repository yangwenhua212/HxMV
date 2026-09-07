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
| `core/loop.py` | Autonomous Control Loop + 执行报告 + **事件化**（`emit=` 旁路：CLI print 与客户端事件并存，观察层不干预闭环） |
| `core/brain.py` | **大脑**：持久记忆，每次 run 自动加载注入、跑完自动回写（详见下） |
| `providers/` | **v0.2 Provider 层**：`VideoProvider` 接口 + 可灵接入骨架 + fake 仿真（见 `docs/PROVIDERS.md`） |
| `server.py` | **Web 控制台 daemon**（纯 stdlib）：SSE 实时事件流 + run 存档 + 大脑只读 + 单文件面板 |

**大脑（持久记忆，v0.1.1）**——像 Hermes 记忆一样"直接用"，但容量不受 2000 字限制：
- 持久化到 `~/.hxmv/brain.json`，进程退出不丢，跨目标/跨项目复用
- **自动注入**：每次 run 开始，把"重要 + 与当前目标相关"的经验自动注入 Planner 上下文（LLM 规划时天然带着历史教训）；Mock 模式也会抬高起手参数（`🧠 记忆起手`）
- **自动回写**：闭环里 FAIL→修正→PASS 的经验自动写入，无需手动记忆
- 容量无小预算：存多少都行，**注入时才**按（相关性×重要性）取 top-k 并受 token 预算约束（`HXMV_MEMORY_CHARS` 可调）——存不心疼，用时才花钱
- 会遗忘：importance 随时间衰减，长期不用的经验淡出（`--fresh` 可清空）
- 实测学习曲线：同一类目标连跑，一致性失败率降 ~28%

**质量记忆**（跨镜头复用）：每个失败原因记录"哪个修正方向被验证成功过"，
后续同类失败优先复用——系统越用越懂自家生成器的脾气。这是项目壁垒，不是套壳。

## 快速开始

```bash
# 零依赖，无需任何 API Key，Mock 世界跑通闭环
python3 -m hxmv "一只小猫在花园里追蝴蝶，5 秒钟"

# 清空大脑从零跑（看学习曲线）
python3 -m hxmv --fresh "一只小猫在花园里追蝴蝶"

# 接真实生成服务（v0.2 Provider 层，fake 仿真无需 key 可跑）
HXMV_PROVIDER=fake python3 -m hxmv "雪地里的柯基"
# HXMV_PROVIDER=kling HXMV_KLING_KEY=sk-xxx python3 -m hxmv "..."   # 真实服务

# 接真 LLM（Planner 规划 + L3 语义评审自动启用；失败自动降级 Mock）
export OPENAI_API_KEY=sk-xxx
export OPENAI_BASE_URL=https://api.deepseek.com/v1   # 任意 OpenAI 兼容端点
export HXMV_LLM_MODEL=deepseek-chat
python3 -m hxmv "30 秒产品宣传片，现代极简风"
```

标准库 only，Python 3.10+。

## Web 控制台（浏览器可视化闭环）

纯 stdlib daemon，零第三方依赖，浏览器里实时看闭环全过程：

```bash
python3 -m hxmv.server            # 默认 http://127.0.0.1:8668
python3 -m hxmv.server --host 0.0.0.0 --port 8668   # 局域网/公网访问
```

- **提交目标** → 选 provider（mock / fake 仿真 / kling）→ 实时流出 任务 → 三层评审 → 修正重试 → 完成
- 每步展示 L1/L2/L3 分层分数、failures→suggestions 修正对、`🧠 回写经验` 提示
- 侧栏实时显示大脑沉淀（LESSON 升华），底部历史 run 点击即回放
- 事件存档：`~/.hxmv/runs/<id>/events.jsonl`（可审计、可回放）
- 原理：`loop.run(goal, emit=cb)` 事件旁路——内核 print 不变（CLI 演示保真），daemon 订阅事件流走 SSE 推给面板；观察层永远不干预闭环判断

## 路线

- **V0.1** ✅ 自主闭环内核（Mock 世界）：LLM 提方案 → 执行 → 三层检测 → 修正 → 通过
- **V0.1.1** ✅ 大脑（Brain）：持久记忆、自动注入/回写、会遗忘——系统越用越懂
- **V0.2** 🚧 Provider 层完成（接真实生成器的桥梁已通，fake 仿真端到端验证）：
  `VideoProvider` 接口 + 可灵接入骨架 + `docs/PROVIDERS.md`；剩余：填真实 API 鉴权、L2 换真视觉模型抽关键帧比对
- **V0.3** 🚧 Web 控制台已落地（事件化内核 + stdlib daemon + 单文件面板，见上）；剩余：资产与一致管线（Asset Manager）、checkpoint 人工审批点
- **V0.4** 📋 扩展到 Research / Coding / Design Agent——复用同一个控制内核

## 设计文档

架构图与决策说明见 [DESIGN.md](DESIGN.md)。

## License

MIT
