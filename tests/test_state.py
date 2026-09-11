"""Tests for hxmv.core.state — Task / Budget / ExecutionState."""
from hxmv.core.state import Budget, ExecutionState, Task, TaskStatus


class TestTask:
    def test_defaults(self):
        t = Task("GENERATE_SHOT")
        assert t.action == "GENERATE_SHOT"
        assert t.status == TaskStatus.PENDING
        assert len(t.task_id) == 8
        assert t.quality["min_score"] == 0.82
        assert t.retry_policy["max_attempts"] == 4
        assert t.retry_policy["attempts"] == 0
        assert t.input == {} and t.constraints == {}

    def test_child_bumps_attempt_and_resets(self):
        t = Task("GENERATE_SHOT", input={"prompt": "x"}, retry_policy={"max_attempts": 4, "attempts": 2})
        c = t.child()
        assert c.retry_policy["attempts"] == 3
        assert c.status == TaskStatus.PENDING
        assert c.result == {}
        # 基因保留：血缘 / 输入 / 约束 / 修正历史
        assert c.task_id == t.task_id
        assert c.action == t.action
        assert c.input["prompt"] == "x"

    def test_child_does_not_mutate_parent(self):
        t = Task("GENERATE_SHOT")
        c = t.child()
        c.input["prompt"] = "changed"
        assert "prompt" not in t.input


class TestBudget:
    def test_exhausted_by_attempts(self):
        b = Budget(max_total_attempts=30)
        b.attempts = 30
        assert b.exhausted

    def test_exhausted_by_cost(self):
        b = Budget(max_cost_units=10.0)
        b.used = 10.0
        assert b.exhausted

    def test_not_exhausted_below_limits(self):
        assert not Budget().exhausted


class TestExecutionState:
    def test_finished_empty_is_false(self):
        assert not ExecutionState(goal="g").finished()

    def test_finished_all_terminal(self):
        s = ExecutionState(goal="g")
        s.tasks = [Task("A", status=TaskStatus.PASS), Task("B", status=TaskStatus.FAIL)]
        assert s.finished()

    def test_finished_pending_is_false(self):
        s = ExecutionState(goal="g")
        s.tasks = [Task("A", status=TaskStatus.PENDING)]
        assert not s.finished()

    def test_finished_when_budget_exhausted(self):
        s = ExecutionState(goal="g")
        s.tasks = [Task("A", status=TaskStatus.PENDING)]
        s.budget.used = s.budget.max_cost_units
        assert s.finished()