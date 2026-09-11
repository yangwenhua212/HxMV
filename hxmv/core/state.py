"""核心状态数据结构：Task / Budget / ExecutionState。

边界原则：LLM 负责"想做什么"（产出结构化 Task），
代码负责"到底怎么做"（validate/schedule/execute/observe/evaluate/retry/recover）。
Task 是 LLM 与代码之间的唯一契约，字段刻意结构化，
不要退化成自由文本——自由文本无法被 Critic/Controller 程序化消费。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


# V0.1 mock 世界的动作集合（视频应用的真实动作如 text_to_video 由 Executor 适配层映射）
ACTION_STORYBOARD = "STORYBOARD"          # 把需求拆成镜头表
ACTION_GENERATE_CHARACTER = "GENERATE_CHARACTER"
ACTION_GENERATE_SCENE = "GENERATE_SCENE"
ACTION_GENERATE_SHOT = "GENERATE_SHOT"    # 单镜头视频（5s 级别）
ACTION_COMPOSE = "COMPOSE"                # 拼接/成片


@dataclass
class Task:
    """一个可执行单元：结构化到代码可以直接 validate/execute。"""

    action: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    input: dict = field(default_factory=dict)          # prompt/duration/seed...
    constraints: dict = field(default_factory=dict)    # character/style/continuity/reference_strength
    quality: dict = field(default_factory=lambda: {"min_score": 0.82})
    retry_policy: dict = field(default_factory=lambda: {"max_attempts": 4, "attempts": 0})
    status: TaskStatus = TaskStatus.PENDING
    result: dict = field(default_factory=dict)         # executor 产物（V0.1 为 mock 元数据）
    refine_history: list[str] = field(default_factory=list)  # 每次修正的说明（给人看）
    fixes_applied: list[dict] = field(default_factory=list)  # 应用的修正 (failure→suggestion)，PASS 时回写记忆

    def child(self, **overrides) -> "Task":
        """派生一个新任务（Refiner 用）：保留血缘，attempts+1。"""
        kwargs = dict(
            action=self.action,
            task_id=self.task_id,
            input=dict(self.input),
            constraints=dict(self.constraints),
            quality=dict(self.quality),
            retry_policy=dict(self.retry_policy),
            refine_history=list(self.refine_history),
        )
        kwargs.update(overrides)
        t = Task(**kwargs)
        t.retry_policy["attempts"] = self.retry_policy.get("attempts", 0) + 1
        t.status = TaskStatus.PENDING
        t.result = {}
        return t


@dataclass
class Budget:
    """成本护栏：随机生成世界也烧钱，闭环必须有预算意识。"""

    max_total_attempts: int = 30   # 全局尝试上限（防死循环）
    max_cost_units: float = 100.0  # mock 成本单位（接真实 API 后映射为金额）
    used: float = 0.0
    attempts: int = 0

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_total_attempts or self.used >= self.max_cost_units


@dataclass
class ExecutionState:
    """闭环的全部可见状态。Controller 每次 update 后推进它。"""

    goal: str
    phase: str = "PLAN"
    tasks: list[Task] = field(default_factory=list)      # 完整任务清单（含已完成的）
    current: Task | None = None
    retry_queue: list[Task] = field(default_factory=list)  # Refiner 产物，优先于新任务执行
    completed: list[Task] = field(default_factory=list)
    failed: list[Task] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)  # [{task_id, report, note}]
    memory: dict = field(default_factory=dict)   # 质量记忆：(failure) -> [历史修正统计]
    budget: Budget = field(default_factory=Budget)
    iteration: int = 0

    def finished(self) -> bool:
        # 全部任务有终态且没有进行中任务
        if not self.tasks:
            return False
        return all(t.status in (TaskStatus.PASS, TaskStatus.FAIL, TaskStatus.SKIPPED)
                   for t in self.tasks) or self.budget.exhausted

    def log(self, line: str) -> None:
        print(f"  {line}")
