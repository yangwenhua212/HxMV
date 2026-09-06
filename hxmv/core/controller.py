"""Controller：闭环的"判断中枢"。

职责（纯代码，不调 LLM）：
1. 用 task.quality.min_score 判定 PASS / FAIL
2. PASS → 归档；FAIL 且 attempts 有余 → 交给 Refiner 派生重试任务
3. 每次判定后把 (failure, suggestion, score) 写进质量记忆——系统越用越懂自家生成器
4. 审计与预算记账
"""
from __future__ import annotations

from .state import ExecutionState, Task, TaskStatus
from .refiner import Refiner


class Controller:
    def __init__(self, refiner: Refiner | None = None, brain=None):
        self.refiner = refiner or Refiner()
        self.brain = brain  # 可选：PASS 时把验证有效的经验写进持久记忆

    def update(self, state: ExecutionState, task: Task,
               result: dict, report) -> str:
        """返回决策：PASS / RETRY / FAIL。副作用：推进 state、记记忆、算预算。"""
        cost = float(result.get("cost_units", 0))
        state.budget.used += cost
        state.budget.attempts += 1
        task.result = result
        state.iteration += 1

        # 质量记忆：任务 PASS 时回写——本次验证有效的修正 (failure→suggestion)
        passed = report.passed and report.score >= float(task.quality.get("min_score", 0.8))
        if passed:
            self._remember_success(state, task, report)
        if passed:
            task.status = TaskStatus.PASS
            state.completed.append(task)
            state.observations.append(
                {"task_id": task.task_id, "report": report,
                 "note": f"{task.action} PASS @{report.score:.2f}（第 {task.retry_policy.get('attempts', 0)+1} 次尝试）"})
            state.log(f"✅ {task.action} {task.task_id} 通过 score={report.score:.2f}")
            return "PASS"

        max_attempts = int(task.retry_policy.get("max_attempts", 3))
        attempts = task.retry_policy.get("attempts", 0)
        retry_task = None if attempts >= max_attempts else \
            self.refiner.refine(task, report, state.memory.get("quality", {}))

        if retry_task is not None:
            state.observations.append(
                {"task_id": task.task_id, "report": report,
                 "note": f"{task.action} FAIL@{report.score:.2f} → 修正: {'; '.join(retry_task.refine_history[-1:])}"})
            state.retry_queue.append(retry_task)
            state.log(f"🔧 {task.action} {task.task_id} FAIL score={report.score:.2f} "
                      f"(尝试 {attempts+1}/{max_attempts}) → 重试: {retry_task.refine_history[-1]}")
            return "RETRY"

        task.status = TaskStatus.FAIL
        state.failed.append(task)
        state.observations.append(
            {"task_id": task.task_id, "report": report,
             "note": f"{task.action} 终态 FAIL（{attempts+1} 次尝试耗尽）score={report.score:.2f}"})
        state.log(f"❌ {task.action} {task.task_id} 终态 FAIL（尝试耗尽）")
        return "FAIL"

    def _remember_success(self, state: ExecutionState, task: Task, report) -> None:
        """PASS 时：把 fixes_applied 里被验证有效的 (failure→suggestion) 记入质量记忆。

        语义：这个失败用这个修正方向，成功了——后续同类失败 Refiner 优先复用。
        """
        if not task.fixes_applied:
            return
        # 持久记忆回写（Brain）：同源经验累积 → 重要性升 → 下次更优先注入
        if self.brain:
            for fix in task.fixes_applied:
                self.brain.remember_lesson(
                    fix["failure"], fix["suggestion"], report.score,
                    context=f"{task.action}: {str(task.input.get('prompt', ''))[:80]}")
        mem = state.memory.setdefault("quality", {})
        for fix in task.fixes_applied:
            f, s = fix["failure"], fix["suggestion"]
            hist = mem.setdefault(f, [])
            hist.append({"suggestion": s, "success": True, "score": report.score})
            if len(hist) > 5:
                del hist[: len(hist) - 5]
