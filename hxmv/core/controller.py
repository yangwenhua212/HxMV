"""Controller：闭环的"判断中枢"。

职责（纯代码，不调 LLM）：
1. 用 task.quality.min_score 判定 PASS / FAIL
2. PASS → 归档；FAIL 且 attempts 有余 → 交给 Refiner 派生重试任务
3. 每次判定后把 (failure, suggestion, score) 写进质量记忆——系统越用越懂自家生成器
4. 审计与预算记账
"""
from __future__ import annotations

import json

from .state import ExecutionState, Task, TaskStatus
from .refiner import MEASURABLE_FIXES, Refiner


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

        # 修正没生效的识别：**同样的失败 + 同样的实测值** = 上一轮那几刀在产物上没落地。
        # 真 API 上实测过：extend_duration 改了三次，出来的还是同一个 5.1 秒片——
        # 那不是"还在收敛"，是同一个动作重复烧额度，必须收手。
        #
        # 但只有**物理类**修正才能这么判：提示词类（四条守卫）/参考强度/运动幅度改的是画面内容，
        # 物理实测值本来就一样，拿它当"没落地"的证据会误杀（实测踩过：语义修正被提前收手，
        # 明明该重生成一次看看）。所以：上一轮只要动过非物理旋钮，就不许收手。
        measured = result.get("measured") or {}
        prev_fixes = [f.split(":")[0].strip() for f in (task.refine_history or [])]
        metrics = {MEASURABLE_FIXES.get(f) for f in prev_fixes}
        measurable_only = bool(prev_fixes) and None not in metrics
        relevant = {k: measured.get(k) for k in sorted(metrics)} if measurable_only else None
        sig = (tuple(sorted(report.failures)),
               json.dumps(relevant, sort_keys=True, ensure_ascii=False)) \
            if relevant is not None else None
        if report.failures and sig is not None and task.retry_policy.get("last_sig") == sig:
            self._fail(state, task)
            state.observations.append(
                {"task_id": task.task_id, "report": report,
                 "note": f"{task.action} 终态 FAIL（修正无效：实测结果与上次完全相同）score={report.score:.2f}"})
            state.log(f"❌ {task.action} {task.task_id} 终态 FAIL"
                      f"（修正没落到产物上，实测结果一模一样，提前收手）")
            return "FAIL"

        retry_task = None if attempts >= max_attempts else \
            self.refiner.refine(task, report, state.memory.get("quality", {}))
        if retry_task is not None:
            retry_task.retry_policy["last_sig"] = sig   # 留给下一轮比对

        if retry_task is not None:
            state.observations.append(
                {"task_id": task.task_id, "report": report,
                 "note": f"{task.action} FAIL@{report.score:.2f} → 修正: {'; '.join(retry_task.refine_history[-1:])}"})
            state.retry_queue.append(retry_task)
            state.log(f"🔧 {task.action} {task.task_id} FAIL score={report.score:.2f} "
                      f"(尝试 {attempts+1}/{max_attempts}) → 重试: {retry_task.refine_history[-1]}")
            return "RETRY"

        self._fail(state, task)
        # 区分三种终态：预算真的耗尽 / 没有可调参数 / 修了但产物一模一样（修正没生效）
        exhausted = attempts >= max_attempts
        why = f"{attempts+1} 次尝试耗尽" if exhausted else "已无参数可调，提前收手"
        state.observations.append(
            {"task_id": task.task_id, "report": report,
             "note": f"{task.action} 终态 FAIL（{why}）score={report.score:.2f}"})
        state.log(f"❌ {task.action} {task.task_id} 终态 FAIL（{why}）")
        return "FAIL"

    @staticmethod
    def _fail(state: ExecutionState, task: Task) -> None:
        """终结一条任务**血统**。

        重试任务是父任务的 child（同一个 task_id），失败的是 child，而 `state.tasks` 里
        留着那个仍是 PENDING 的父任务——不一起结掉，Planner 下一轮会把父任务原地再发一遍，
        同一件事无限重来（实测：提前收手后又被重发 13 轮，最后伪装成"预算耗尽"收场）。
        """
        for t in state.tasks:
            if t.task_id == task.task_id:
                t.status = TaskStatus.FAIL
        task.status = TaskStatus.FAIL
        if task not in state.failed:
            state.failed.append(task)

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
