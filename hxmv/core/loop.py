"""Autonomous Control Loop：整个项目最核心的一小块。

while not goal_reached:
    plan = planner(state)      # LLM 想做什么
    result = executor(plan)    # 代码怎么做
    observation = critic(result)  # 系统观察结果（眼睛）
    state = controller(state, plan, result, observation)  # 判断 → 推进 / 修正 / 归档

执行顺序细节（重要）：
- retry_queue（Refiner 产物）优先于 Planner 新任务——修正的是"当前这一步"，
  不要跳过它去开新活。
- 上下文压缩在每轮开头检查——超阈值才压，不固定"每 N 步"。
- 预算耗尽立即停，输出已完成的成果，绝不无限烧。
"""
from __future__ import annotations

from .context import ContextManager
from .controller import Controller
from .critic import PipelineCritic
from .executor import MockVideoExecutor
from .planner import make_planner
from .state import ExecutionState, TaskStatus


def banner(state: ExecutionState) -> None:
    print("\n" + "═" * 52)
    print(f"  HxMV 自主控制闭环  v0.1")
    print(f"  目标：{state.goal}")
    print("═" * 52)


def summary(state: ExecutionState) -> None:
    print("\n" + "─" * 52)
    print("📊 执行报告")
    print("─" * 52)
    for t in state.completed:
        print(f"  ✅ {t.action:24s} {t.task_id}  score≈{t.result.get('_score', '')}  "
              f"尝试 {t.retry_policy.get('attempts', 0)+1} 次"
              + ("  [修正: " + t.refine_history[-1] + "]" if t.refine_history else ""))
    for t in state.failed:
        print(f"  ❌ {t.action:24s} {t.task_id}  尝试耗尽终态失败")
    print(f"\n  总尝试 {state.budget.attempts} 次 | 总成本 {state.budget.used:.2f} 元"
          f" | 迭代 {state.iteration} 轮")
    mem = state.memory.get("quality", {})
    if mem:
        print("  质量记忆（跨镜头复用）:")
        for f, hist in mem.items():
            ok = sum(1 for h in hist if h["success"])
            print(f"    · {f}: 历史 {len(hist)} 次记录，{ok} 次修正成功")
    comp = state.memory.get("_compressions")
    if comp:
        print(f"  上下文压缩: {len(comp)} 次")
    if state.budget.exhausted:
        print("  ⚠ 预算耗尽提前停止")
    print("─" * 52)


def run(goal: str,
        planner=None, executor=None, critic=None, controller=None,
        context: ContextManager | None = None,
        verbose: bool = True) -> ExecutionState:
    """跑一个目标到完成，返回最终 ExecutionState（可继续检视/续跑）。"""
    state = ExecutionState(goal=goal)
    if verbose:
        banner(state)

    executor = executor or MockVideoExecutor()
    critic = critic or PipelineCritic()
    controller = controller or Controller()
    context = context or ContextManager()
    planner = planner or make_planner(state)  # 工厂：LLM 优先，失败落 Mock

    while True:
        if state.budget.exhausted:
            state.log("⛔ 预算耗尽，停止")
            break
        context.maybe_compress(state)  # 动态上下文压缩（超阈值才压）

        # 1) 先取 Refiner 产物（修正当前缺陷），没有再问 Planner 要新活
        task = state.retry_queue.pop(0) if state.retry_queue else planner.next_task(state)
        if task is None:
            break  # 规划完毕且无重试 = 目标达成
        if task.task_id not in [t.task_id for t in state.tasks]:
            state.tasks.append(task)  # 登记完整任务清单
        state.current = task

        # 2) 执行 → 3) 观察（三层 Critic 合并报告）→ 4) 判断推进
        if verbose:
            print(f"\n▶ {task.action} {task.task_id}"
                  + (f"  prompt={task.input.get('prompt', '')[:24]}…" if task.input.get("prompt") else "")
                  + (f"  [第 {task.retry_policy.get('attempts', 0)+1} 次尝试]" if task.retry_policy.get("attempts") else ""))
        result = executor.execute(task)
        report = critic.evaluate(task, result)
        result["_score"] = f"{report.score:.2f}"
        if verbose:
            print(f"  👁 {report}")
        controller.update(state, task, result, report)

    state.phase = "DONE"
    state.current = None
    if verbose:
        summary(state)
    return state


if __name__ == "__main__":
    import sys

    goal = " ".join(sys.argv[1:]) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    run(goal)
