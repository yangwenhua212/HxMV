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
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..media import probe
from ..providers.base import ProviderError
from .artifacts import consolidate_artifacts
from .brain import Brain
from .context import ContextManager
from .controller import Controller
from .critic import PipelineCritic
from .executor import make_executor
from .planner import make_planner
from .state import ACTION_GENERATE_SHOT, ExecutionState, TaskStatus


# 同一个基础设施错误连续放倒几个任务 = 环境问题已坐实，停止本次 run
# （实测：ffmpeg 缺 fontconfig 时，5 个任务各重试 4 次 = 20 次必然失败的重试，
#  没有任何一次尝试有可能成功，纯烧预算 + 拖长失败反馈）。
INFRA_MAX_RETRY = 4        # 单个任务的基础设施重试上限
INFRA_ABORT_STREAK = 3     # 连续栽在同一错误上的任务数上限


def _execute_one(executor, task, state, emit, verbose: bool, max_infra: int,
                 budget_lock=None) -> tuple[dict | None, str]:
    """执行单个任务（含基础设施重试）。返回 `(result | None, 最后一次错误消息)`。

    抽成独立函数是为了能被**并发批次**复用：主线程串行调用它 = 原来的行为；
    多个任务并发调用它（各自拿一份 executor 副本）= 多镜头并行。
    """
    if verbose:
        print(f"\n▶ {task.action} {task.task_id}"
              + (f"  prompt={task.input.get('prompt', '')[:24]}…" if task.input.get("prompt") else "")
              + (f"  [第 {task.retry_policy.get('attempts', 0)+1} 次尝试]"
                 if task.retry_policy.get("attempts") else ""))
    result, last_err = None, ""
    retried = 0
    while retried < max_infra:
        try:
            result = executor.execute(task)
            break
        except ProviderError as e:
            # 预算累加必须加锁：并发任务同时改同一个 float，而 `+=` 不是原子操作——
            # 少加几次就等于预算护栏失效，而预算护栏护的正是"真花钱"的地方。
            if budget_lock is None:
                state.budget.used += e.cost_units
            else:
                with budget_lock:
                    state.budget.used += e.cost_units
            retried += 1
            last_err = str(e)
            state.log(f"⚠ 服务端错误: {e}（第 {retried} 次重试）")
            emit({"type": "infra.retry", "task_id": task.task_id,
                  "action": task.action, "attempt": retried,
                  "error": str(e), "retryable": bool(e.retryable)})
            if not e.retryable or retried >= max_infra:
                break
    return result, last_err


def _execute_batch(executor, tasks: list, state, emit, verbose: bool,
                   max_infra: int, budget_lock=None) -> dict:
    """执行一批任务：1 个 → 原地串行；多个 → 并发（每个任务独占一份 executor 副本）。

    并发时**事件的顺序会交错**（infra.retry 可能交叠出现），这是并行的固有代价；
    但判定与状态推进**完全串行**——critic / controller / 档案写回都在调用方按批次顺序做，
    所以 run 的最终结果与串行执行等价且可复现（有测试守着这条不变量）。

    为什么用副本而不是共享：provider 自带实例级缓存与产物状态（local_render 的资产缓存、
    zhipu 的本地中转目录），共享一个实例会被多线程互相踩。副本共享项目档案与输出目录，
    那两处另有锁（Project._lock / local_render._FILE_LOCK）。
    """
    tasks = [t for t in tasks if t is not None]
    if not tasks:
        return {}
    if len(tasks) == 1:
        task = tasks[0]
        return {task.task_id: _execute_one(executor, task, state, emit, verbose,
                                          max_infra, budget_lock)}

    if not executor.parallel_safe:
        # 防御：loop 组批时已经检查过，但这是公共入口——真拿一个不支持并发的 executor
        # 硬开线程池，等于让多线程共用一个带实例状态的 provider（缓存/产物路径互相踩）。
        state.log("⚠ executor 未声明并行安全 → 本批退回串行")
        return {t.task_id: _execute_one(executor, t, state, emit, verbose, max_infra, budget_lock)
                for t in tasks}

    state.log(f"⚡ 并行执行 {len(tasks)} 个互相独立的镜头（并行度 {len(tasks)}）")
    out: dict = {}

    def _one(task):
        clone = executor.for_concurrent() or executor
        return _execute_one(clone, task, state, emit, verbose, max_infra, budget_lock)

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(_one, t): t for t in tasks}
        for fut, task in futures.items():
            try:
                out[task.task_id] = fut.result()
            except Exception as e:   # 非 ProviderError 的异常也要收成"这一个任务失败"
                out[task.task_id] = (None, f"{type(e).__name__}: {e}")
    return out


def banner(state: ExecutionState) -> None:
    print("\n" + "═" * 52)
    print(f"  HxMV 自主控制闭环  v0.6 · 真 AI 视频 + 项目档案")
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
    reused = [t for t in state.completed if t.result.get("reused")]
    if reused:
        print(f"\n  ♻ 复用已有画面 {len(reused)} 个（档案命中指纹 → 没有重新生成/没有重新花钱）")
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
        project=None,
        episode: int | None = None,
        verbose: bool = True,
        emit=None,
        approve=None) -> ExecutionState:
    """跑一个目标到完成，返回最终 ExecutionState（可继续检视/续跑）。

    brain：持久记忆（"大脑"）。不传则自动加载 ~/.hxmv/brain.json。
    经验在闭环中自动积累：PASS 回写、下次 run 自动注入 Planner。
    project：项目档案（跨 run 记住风格/角色/已生成画面）。传入后：
      - 规划沿用档案里的角色/场景/风格（不会新建角色）
      - 生成前先查档案指纹，命中就**复用已有画面，不重新生成**
      - 跑完把本次记成新的一集（episode 可指定集号）
    emit：可选事件回调 emit(dict)——每个关键节点收到一个 JSON 可序列化事件。
    事件订阅者抛异常不影响闭环（观察层永远不打断生产）。
    approve：可选人工审批点（checkpoint）。签名 `approve(task) -> bool | None`：
      - `None` = 这个动作不需要审批 → 直接开工（调用方决定策略：只审付费档、审全部…）
      - `True` = 已获批准 → 正常执行
      - `False` = 被拒绝 → 该任务判 FAIL 并跳过，**不消耗生成预算**
    策略放在回调里，闭环本身不认识"付费""高危"这些业务概念——边界不越。
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
        if project is not None:
            print(f"  📁 {project.describe()}")
        if brain.size:
            print(f"  🧠 大脑：{brain.stats()}（已加载，自动注入规划）")
        else:
            print("  🧠 大脑：空（本次运行将开始积累经验）")
    _emit({"type": "run.start", "goal": goal, "brain": _brain_brief(brain),
           "budget_max_attempts": state.budget.max_total_attempts,
           "budget_max_cost": state.budget.max_cost_units})

    executor = executor or make_executor(project=project, episode=episode)
    critic = critic or PipelineCritic()
    controller = controller or Controller(brain=brain)
    context = context or ContextManager()
    planner = planner or make_planner(state, brain, project)  # 工厂：LLM 优先（带记忆/项目）

    provider = getattr(executor, "provider", None)
    if provider is not None and hasattr(provider, "set_episode"):
        provider.set_episode(episode)

    infra_sig = ""      # 最近一次基础设施错误的签名
    infra_streak = 0    # 该签名连续放倒了几个任务
    peeked: list = []   # 为组批试探取出、但本轮不该执行的任务（下一轮先用，绝不丢）
    parallel_n = max(1, int(os.environ.get("HXMV_PARALLEL", "1") or 1))
    budget_lock = threading.Lock()   # 并发任务的预算累加保护（+= 不是原子操作）
    while True:
        if state.budget.exhausted:
            state.log("⛔ 预算耗尽，停止")
            break
        context.maybe_compress(state)  # 动态上下文压缩（超阈值才压）

        # 1) 先取 Refiner 产物（修正当前缺陷），没有再问 Planner 要新活。
        #    peeked：上一轮组批时试探取出、但"不该现在执行"的任务（例如成片），留着先用。
        #    retry_queue 永远最优先——修当前缺陷优先于开新活。
        task = (state.retry_queue.pop(0) if state.retry_queue
                else (peeked.pop(0) if peeked else planner.next_task(state)))
        if task is None:
            break  # 规划完毕且无重试 = 目标达成

        # 1a) 组批：**只并互相独立的镜头**。
        #     镜头之间没有依赖（各自产物按 task_id 命名），所以可以同时做；而资产
        #     （角色/场景）有共享缓存与档案写入、成片（COMPOSE）依赖全部镜头 → 都不并行。
        #     只在"没有待修正任务"时组批：修正必须立刻做，不能被攒进批次。
        batch = [task]
        if (parallel_n > 1 and not state.retry_queue
                and task.action == ACTION_GENERATE_SHOT and executor.parallel_safe):
            while len(batch) < parallel_n:
                nxt = planner.next_task(state)
                if nxt is None:
                    break
                if nxt.action != ACTION_GENERATE_SHOT:
                    peeked.append(nxt)   # 不是镜头 → 留给串行阶段，顺序不变
                    break
                batch.append(nxt)

        for t in batch:
            if t.task_id not in [x.task_id for x in state.tasks]:
                state.tasks.append(t)    # 登记完整任务清单
            # **立刻**标记 RUNNING：LLMPlanner 是"找第一个 PENDING"来发任务的，
            # 不标记的话组批时会反复拿到同一个任务（批里只有一个元素，等于没组批）。
            t.status = TaskStatus.RUNNING
            _emit({"type": "task.start", **_task_brief(t),
                   "kind": "retry" if t.retry_policy.get("attempts") else "new"})

        # 1.5) 人工审批点（checkpoint）：付费/高危动作**开工前**问一次。
        #      必须在执行之前：被拒绝时一个字节都不会发出去，也不会产生任何费用
        #      （事后审批只能补救，钱已经花了）。策略由 approve 回调决定，闭环不认识业务概念。
        #      批内逐个问（串行）：审批是"人的动作"，并发提问既乱又容易点错。
        runnable: list = []
        for t in batch:
            if approve is not None:
                verdict = None
                try:
                    verdict = approve(t)
                except Exception as e:   # 审批通道故障不能打断生产——按"无需审批"放行
                    state.log(f"⚠ 审批回调异常（按放行处理）: {e}")
                if verdict is False:
                    t.status = TaskStatus.FAIL
                    state.failed.append(t)
                    state.observations.append(
                        {"task_id": t.task_id, "report": None,
                         "note": f"{t.action} 被人工拒绝"})
                    state.log(f"⛔ {t.action} {t.task_id} 未获批准，跳过（未产生费用）")
                    _emit({"type": "checkpoint", "task_id": t.task_id, "action": t.action,
                           "approved": False, "note": "人工拒绝"})
                    continue
                if verdict is True:
                    _emit({"type": "checkpoint", "task_id": t.task_id, "action": t.action,
                           "approved": True, "note": "人工批准"})
            runnable.append(t)
        if not runnable:
            continue

        # 2) 执行：基础设施错误（ProviderError）按"服务重试"处理，
        #    与质量 FAIL 分道——不消耗 Refiner 的重试额度。
        #    熔断：前面的任务已经确认过这个错误无解（环境问题而不是网络抖动）→
        #    新任务只试 1 次，不再重复踩 4 次（实测缺字体时连烧 20 次无效重试）
        max_infra = 1 if infra_streak else INFRA_MAX_RETRY
        results = _execute_batch(executor, runnable, state, _emit, verbose,
                                 max_infra, budget_lock)

        infra_aborted = False
        for task in runnable:
            state.current = task
            result, last_err = results.get(task.task_id, (None, "未执行"))
            if result is None:
                task.status = TaskStatus.FAIL
                state.failed.append(task)
                state.observations.append(
                    {"task_id": task.task_id, "report": None,
                     "note": f"{task.action} 基础设施失败（服务重试耗尽）"})
                state.log(f"❌ {task.action} {task.task_id} 服务不可用，放弃")
                _emit({"type": "decision", "task_id": task.task_id, "action": task.action,
                       "decision": "FAIL", "reason": "infra", "note": "服务不可用（重试耗尽）"})

                # 熔断：同一个错误放倒连续多个任务 → 判定为环境问题，提前收手。
                # 这跟质量层的"相同失败+相同实测值收手"是同一套思路，只是发生在基础设施层。
                sig = last_err[:120]
                infra_streak = infra_streak + 1 if sig == infra_sig else 1
                infra_sig = sig
                if infra_streak >= INFRA_ABORT_STREAK:
                    hint = ("环境问题，继续跑只会重复失败：先跑 `python -m hxmv --doctor` 定位，"
                            "或临时用 `--provider mock` 绕开渲染/远端")
                    state.log(f"⛔ 连续 {infra_streak} 个任务栽在同一个基础设施错误上 → {hint}")
                    _emit({"type": "infra.abort", "task_id": task.task_id,
                           "error": sig, "streak": infra_streak, "hint": hint})
                    infra_aborted = True
                    break
                # 这里用 continue 而不是 break：批内其他任务**已经执行完了**，
                # 必须继续把它们的评审与判定做完，否则它们会永远停在 RUNNING
                # （串行时代 continue 的语义是"回到取任务"，结果等价）。
                continue
            if verbose and result.get("reused"):
                print("  ♻ 复用已有画面（未重新生成，0 成本）")

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

        if infra_aborted:
            # 组批时试探取出、还没来得及执行的任务：它们停在 PENDING，
            # 不标记的话 run 结束后状态是"既没完成也没失败"（客户端会当成还在跑）。
            for t in peeked:
                t.status = TaskStatus.SKIPPED
            if peeked:
                state.log(f"  （{len(peeked)} 个任务未轮到执行，已标记为跳过）")
            break

    state.phase = "DONE"
    state.current = None
    # 归拢"本次 run 真正用到的成品"——复用自项目档案的旧文件也算本次的产物。
    # 不这么做的话：全是复用的时候产物目录是空的，面板/客户端会以为"什么都没产出"（实测踩过）。
    artifact_paths = consolidate_artifacts(state, os.environ.get("HXMV_ARTIFACTS", ""))
    if verbose:
        summary(state)
        print(f"  🧠 大脑已更新：{brain.stats()}（经验持久化到 {brain.path}）")
        if artifact_paths:
            print(f"  🎬 本次成品 {len(artifact_paths)} 个（含复用）："
                  + "、".join(os.path.basename(p) for p in artifact_paths[:4]))
    brain.save()
    if project is not None:
        project.add_episode(goal, artifact_paths or
                            [t.result.get("output") or t.result.get("media")
                             for t in state.completed], episode)
        project.save()
        if verbose:
            print(f"  📁 项目档案已更新：{project.describe()}")
    _emit({"type": "run.done", "phase": state.phase,
           "n_reused": sum(1 for t in state.completed if t.result.get("reused")),
           "project": project.summary() if project is not None else None,
           "artifacts": artifact_paths,
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
