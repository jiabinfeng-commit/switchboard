"""人工任务：流程挂起在 wait_human 时，给人看的那条记录。

它和 Executor 的挂起回调不是一回事。回调做外部副作用（发消息、建工单备注），
可能失败、可能重做；人工任务是流程的一部分，决定了「谁能用哪个 outcome
唤醒实例」。所以它有自己的状态：开着的任务只能被处置一次，
处置结果必须落在流程定义声明过的边上。

人工处置不是一个「已完成」勾选框，而是明确选择 `existing` / `retry` /
`abandoned`。见 ADR 0003 决策 6：只勾完成会让引擎猜下一步，
而猜错在「可能已建单」的场景里代价是重复建单。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class HumanTaskStatus(StrEnum):
    OPEN = "open"
    SUBMITTED = "submitted"


@dataclass(frozen=True)
class HumanTask:
    id: str
    instance_id: str
    node: str
    # 触发转人工的那次执行结果；恢复出来的实例可能没有，允许为空。
    cause: str | None
    may_have_committed: bool
    # 来自流程定义里该节点的 next（去掉 timeout）。
    allowed_outcomes: frozenset[str]
    status: HumanTaskStatus
    created_at: datetime
    deadline: datetime
    submitted_outcome: str | None = None
    submitted_at: datetime | None = None


class HumanTaskError(RuntimeError):
    pass


class HumanTaskNotFound(HumanTaskError):
    pass


class HumanTaskNotOpen(HumanTaskError):
    """任务已经被处置过，重复提交不能把实例唤醒两次。"""


class InvalidHumanOutcome(HumanTaskError):
    """提交的 outcome 不在流程定义声明的边上。"""


class HumanTaskStore(Protocol):
    def create(self, task: HumanTask) -> HumanTask: ...
    def load(self, task_id: str) -> HumanTask: ...
    def submit(self, task_id: str, outcome: str, submitted_at: datetime) -> HumanTask: ...
    def find_open(self, instance_id: str) -> HumanTask | None: ...


class InMemoryHumanTaskStore:
    """单进程实现。并发提交靠「只有 OPEN 才能提交」这一条挡住。"""

    def __init__(self) -> None:
        self._rows: dict[str, HumanTask] = {}

    def create(self, task: HumanTask) -> HumanTask:
        if task.id in self._rows:
            raise HumanTaskError(f"人工任务已存在：{task.id}")
        self._rows[task.id] = task
        return task

    def load(self, task_id: str) -> HumanTask:
        try:
            return self._rows[task_id]
        except KeyError:
            raise HumanTaskNotFound(f"人工任务不存在：{task_id}") from None

    def submit(self, task_id: str, outcome: str, submitted_at: datetime) -> HumanTask:
        """校验并落处置结果。

        先用状态挡重复提交，再校验 outcome。顺序不能反：
        一个已提交的任务即使 outcome 合法也不能再提交一次。
        """
        task = self.load(task_id)
        if task.status is not HumanTaskStatus.OPEN:
            raise HumanTaskNotOpen(f"人工任务 {task_id} 已经处置过：{task.submitted_outcome}")
        if outcome not in task.allowed_outcomes:
            raise InvalidHumanOutcome(
                f"人工任务 {task_id} 不接受 {outcome!r}，"
                f"可选：{sorted(task.allowed_outcomes)}"
            )
        done = replace(
            task,
            status=HumanTaskStatus.SUBMITTED,
            submitted_outcome=outcome,
            submitted_at=submitted_at,
        )
        self._rows[task_id] = done
        return done

    def find_open(self, instance_id: str) -> HumanTask | None:
        for task in self._rows.values():
            if task.instance_id == instance_id and task.status is HumanTaskStatus.OPEN:
                return task
        return None
