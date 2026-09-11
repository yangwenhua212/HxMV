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

事件化（v0.3 客户端）：run(..., emit=cb) 在每个关键节点 emit 一个 JSON 可序列化事件
dict——CLI 的 print 是终端渲染器之一，emit 是给 Web/daemon 的旁路。两者并存：
print 保真（终端演示），emit 供客户端实时展示。事件 = 观察层，不改职责单向。
"""
from __future__ import annotations

import os
import time

from ..media import probe
from ..providers.base import ProviderError
from .brain import Brain
from .context import ContextManager
from .controller import Controller
from .critic import PipelineCritic
from .executor import make_executor
from .planner import make_planner
from .state import ExecutionState, TaskStatus


def banner(state: ExecutionState) -> None:
    print("\n" + "═" * 52)
    print(f"  HxMV 自主控制闭环  v0.4 · 真产物 + 真眼睛")
    print(f"  目标：{state.goal}")
    print("═" * 52)


def summary(state: ExecutionState) -> None:
    print("\n" + "─" * 52)
    print("📊 执行报告")
    print("─" * 52)
    for t in state.completed:
        print(f"  ✅ {t.action:24s} {t.task_id}  score≈{t.result.get('_score', '')}  "
              f"尝试 {t.retry_policy.get('attempts', 0)+1} 次"
              + (f"  [修正: {t.refine_history[-1]}]" if t.refine_history else ""))
    for t in state.failed:
        print(f"  ❌ {t.action:24s} {t.task_id}  尝试耗尽终态失败")
    # 真实产物：只有落盘的真文件才列（mock 世界的占位字符串不在此列），同一路径只列一次
    artifacts, seen = [], set()
    for t in state.completed:
        for key in ("media", "output", "asset", "reference"):
            p = t.result.get(key)
            if p and os.path.isfile(str(p)) and str(p) not in seen:
                seen.add(str(p))
                artifacts.append((t.action, key, str(p)))
    if artifacts:
        print("\n  🎬 真实产物（落盘文件）:")
        for action, key, p in artifacts:
            names = {"media": "镜头", "output": "成片", "asset": "资产", "reference": "参考图"}
            print(f"    · {names.get(key, key):4s} {p}  ({os.path.getsize(p)/1024:.0f} KB)")
    real = [t for t in state.completed if t.result.get("metrics")]
    if real:
        print("\n  👁 实测指标（ffmpeg/ffprobe 从媒体里量的，不是标签）:")
        for t in real:
            f = t.result.get("media") or t.result.get("output") or ""
            print(f"    · {os.path.basename(str(f)):24s} → {probe.describe(t.result['metrics'])}")
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


# ---------- 事件序列化 helpers（全 JSON-safe，供客户端/daemon 消费） ----------

def _task_brief(task) -> dict:
    return {
        "task_id": task.task_id,
        "action": task.action,
        "attempt": task.retry_policy.get("attempts", 0) + 1,
        "max_attempts": task.retry_policy.get("max_attempts", 3),
        "prompt": str(task.input.get("prompt", "") or task.input.get("goal", ""))[:80] or None,
        "min_score": float(task.quality.get("min_score", 0.8)),
    }


def _report_brief(report) -> dict:
    return {
        "layer": report.layer,
        "score": round(float(report.score), 3),
        "passed": report.passed,
        "failures": list(report.failures),
        "suggestions": list(report.suggestions),
    }


def _result_brief(result: dict) -> dict:
    keys = ("storyboard", "asset", "media", "duration", "fps", "resolution",
            "defects", "output", "shots", "params", "cost_units",
            "metrics", "consistency", "reference")
    return {k: result[k] for k in keys if k in result}


def _brain_brief(brain: Brain) -> dict:
    return {"size": brain.size, "stats": brain.stats(), "path": brain.path}


def run(goal: str,
        planner=None, executor=None, critic=None, controller=None,
        context: ContextManager | None = None,
        brain: Brain | None = None,
        verbose: bool = True,
        emit=None) -> ExecutionState:
    """跑一个目标到完成，返回最终 ExecutionState（可继续检视/续跑）。

    brain：持久记忆（"大脑"）。不传则自动加载 ~/.hxmv/brain.json。
    经验在闭环中自动积累：PASS 回写、下次 run 自动注入 Planner。
    emit：可选事件回调 emit(dict)——每个关键节点收到一个 JSON 可序列化事件。
    事件订阅者抛异常不影响闭环（观察层永远不打断生产）。
    """
    def _emit(event: dict) -> None:
        if emit is not None:
            try:
                event["ts"] = time.time()
                emit(event)
            except Exception:
                pass

    state = ExecutionState(goal=goal)
    brain = brain or Brain()
    provider_hint = ""
    if verbose:
        banner(state)
        if brain.size:
            print(f"  🧠 大脑：{brain.stats()}（已加载，自动注入规划）")
        else:
            print("  🧠 大脑：空（本次运行将开始积累经验）")
    _emit({"type": "run.start", "goal": goal, "brain": _brain_brief(brain),
           "budget_max_attempts": state.budget.max_total_attempts,
           "budget_max_cost": state.budget.max_cost_units})

    executor = executor or make_executor()
    critic = critic or PipelineCritic()
    controller = controller or Controller(brain=brain)
    context = context or ContextManager()
    planner = planner or make_planner(state, brain)  # 工厂：LLM 优先（带记忆），失败落 Mock

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
        brief = _task_brief(task)
        _emit({"type": "task.start", **brief,
               "kind": "retry" if task.retry_policy.get("attempts") else "new"})

        # 2) 执行：基础设施错误（ProviderError）按"服务重试"处理，
        #    与质量 FAIL 分道——不消耗 Refiner 的重试额度
        if verbose:
            print(f"\n▶ {task.action} {task.task_id}"
                  + (f"  prompt={task.input.get('prompt', '')[:24]}…" if task.input.get("prompt") else "")
                  + (f"  [第 {task.retry_policy.get('attempts', 0)+1} 次尝试]" if task.retry_policy.get("attempts") else ""))
        result = None
        infra_retried = 0
        while infra_retried < 4:
            try:
                result = executor.execute(task)
                break
            except ProviderError as e:
                state.budget.used += e.cost_units  # 部分 API 失败也计费
                infra_retried += 1
                state.log(f"⚠ 服务端错误: {e}（第 {infra_retried} 次重试）")
                _emit({"type": "infra.retry", "task_id": task.task_id,
                       "action": task.action, "attempt": infra_retried,
                       "error": str(e), "retryable": bool(e.retryable)})
                if not e.retryable or infra_retried >= 4:
                    break
        if result is None:
            task.status = TaskStatus.FAIL
            state.failed.append(task)
            state.observations.append(
                {"task_id": task.task_id, "report": None,
                 "note": f"{task.action} 基础设施失败（服务重试耗尽）"})
            state.log(f"❌ {task.action} {task.task_id} 服务不可用，放弃")
            _emit({"type": "decision", "task_id": task.task_id, "action": task.action,
                   "decision": "FAIL", "reason": "infra", "note": "服务不可用（重试耗尽）"})
            continue

        # 3) 观察（三层 Critic 合并报告）→ 4) 判断推进
        report = critic.evaluate(task, result)
        result["_score"] = f"{report.score:.2f}"
        _emit({"type": "critic", "task_id": task.task_id, "action": task.action,
               "score": round(float(report.score), 3),
               "passed": report.passed,
               "failures": list(report.failures),
               "suggestions": list(report.suggestions),
               "detail": report.detail,
               "measured": {"metrics": result.get("metrics"),
                            "consistency": result.get("consistency")}
                           if (result.get("metrics") or result.get("consistency") is not None) else None,
               "layers": [_report_brief(r) for r in critic.evaluate_layers(task, result)]
                         if hasattr(critic, "evaluate_layers") else []})
        if verbose:
            print(f"  👁 {report}")
        decision = controller.update(state, task, result, report)

        # 4b) decision 事件（PASS/RETRY/FAIL + 修正说明，供客户端渲染）
        attempts_used = task.retry_policy.get("attempts", 0) + 1
        ev = {"type": "decision", "task_id": task.task_id, "action": task.action,
              "decision": decision, "score": round(float(report.score), 3),
              "attempts_used": attempts_used,
              "max_attempts": task.retry_policy.get("max_attempts", 3),
              "refine_history": list(task.refine_history)}
        if decision == "PASS":
            ev["fixes_applied"] = [dict(f) for f in task.fixes_applied]
        elif decision == "RETRY":
            retry_task = state.retry_queue[-1] if state.retry_queue else None
            if retry_task is not None:
                ev["note"] = retry_task.refine_history[-1] if retry_task.refine_history else None
        elif decision == "FAIL":
            ev["note"] = "尝试耗尽，终态失败"
        _emit(ev)

    state.phase = "DONE"
    state.current = None
    if verbose:
        summary(state)
        print(f"  🧠 大脑已更新：{brain.stats()}（经验持久化到 {brain.path}）")
    brain.save()
    _emit({"type": "run.done", "phase": state.phase,
           "completed": [{"action": t.action, "task_id": t.task_id,
                          "score": t.result.get("_score"),
                          "refine_history": list(t.refine_history)} for t in state.completed],
           "failed": [{"action": t.action, "task_id": t.task_id} for t in state.failed],
           "attempts": state.budget.attempts,
           "cost_units": round(float(state.budget.used), 3),
           "iterations": state.iteration,
           "budget_exhausted": state.budget.exhausted,
           "brain": _brain_brief(brain),
           "outputs": [t.result.get("output") or t.result.get("media")
                       for t in state.completed if t.result.get("output") or t.result.get("media")]})
    return state


if __name__ == "__main__":
    import sys

    goal = " ".join(sys.argv[1:]) or "一只小猫在花园里追蝴蝶，5 秒钟短视频"
    run(goal)
