"""步骤执行记录：一次可执行节点的尝试。

记录存在的唯一理由是崩溃恢复。实例本身只保存「现在停在哪」，
而崩溃可能发生在「副作用已经发生、实例还没迁移」的窗口里，
没有执行记录就无法区分「这一步没跑过」和「跑了一半」。

幂等键绑定的是**一条执行记录**，不是「节点 + attempt」。
回环（确认 -> 修改 -> 回草稿）会把 attempt 重置为 1，
如果键由 instance+node+attempt 拼出来，第二轮回环会和第一轮撞键，
把两次不同的业务请求当成同一次重发。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol

from switchboard.agent.result import StepResult


class StepExecutionStatus(StrEnum):
    # 已经决定要执行、可能已经发出请求，但结果没有落库。
    STARTED = "started"

    # 结果已经写进执行记录。
    COMPLETED = "completed"

    # 结果已经通过实例迁移体现，只是执行记录没来得及收尾。
    # 这是崩溃恢复里的补偿状态：不重新执行，只把记录补完整。
    RECOVERED = "recovered"


@dataclass(frozen=True)
class StepExecution:
    """一次步骤尝试的持久化形状。

    key 就是幂等键：执行器必须把它原样带给下游。崩溃重跑时复用同一个 key，
    下游据此判断「这是同一次请求的重发」而不是两次业务请求。
    """

    key: str
    instance_id: str
    node: str
    attempt: int
    status: StepExecutionStatus
    requested_at: datetime
    result: StepResult | None = None
    context_update: Mapping[str, Any] | None = None
    finished_at: datetime | None = None


class ExecutionStoreError(RuntimeError):
    pass


class ExecutionNotFound(ExecutionStoreError):
    pass


class ExecutionAlreadyOpen(ExecutionStoreError):
    """同一个实例同时有两条未收尾的执行记录，说明推进逻辑出了问题。"""


class ExecutionNotOpen(ExecutionStoreError):
    """对一条已经收尾的记录做收尾操作。"""


class StepExecutionStore(Protocol):
    def begin(self, execution: StepExecution) -> StepExecution:
        """落一条 STARTED 记录。同一实例已有未收尾记录时拒绝。"""
        ...

    def complete(
        self,
        key: str,
        result: StepResult,
        context_update: Mapping[str, Any] | None,
        finished_at: datetime,
    ) -> StepExecution:
        """把 STARTED 记录收尾成 COMPLETED，带上结果和上下文增量。"""
        ...

    def mark_recovered(self, key: str, finished_at: datetime) -> StepExecution:
        """把 STARTED 记录补记成 RECOVERED。

        用于「实例已经迁移，但执行记录没收尾」的崩溃窗口：
        结果已经体现在实例上，再执行一次反而会重复副作用。
        """
        ...

    def find_open(self, instance_id: str) -> StepExecution | None:
        """取该实例尚未收尾的执行记录；没有则返回 None。"""
        ...

    def last_completed(self, instance_id: str) -> StepExecution | None:
        """取该实例最近一次收尾的记录，按写入顺序。"""
        ...

    def list_for_instance(self, instance_id: str) -> list[StepExecution]:
        """按写入顺序列出该实例的全部尝试。审计和回放用。"""
        ...

    def load(self, key: str) -> StepExecution:
        """按幂等键取记录；不存在时抛 ExecutionNotFound。"""
        ...


class InMemoryStepExecutionStore:
    """单进程实现。和实例存储一样，只用于测试与本地演示。"""

    def __init__(self) -> None:
        self._rows: dict[str, StepExecution] = {}

    def begin(self, execution: StepExecution) -> StepExecution:
        """插入一条 STARTED 记录，并拒绝同一实例的并发未收尾记录。

        已存在的键直接覆盖是危险的：那意味着两次执行共用一个幂等键，
        下游会把第二次当成第一次的重发。所以重复键也拒绝。
        """
        if execution.key in self._rows:
            raise ExecutionAlreadyOpen(f"执行记录已存在：{execution.key}")
        if self.find_open(execution.instance_id) is not None:
            raise ExecutionAlreadyOpen(
                f"实例 {execution.instance_id} 已有未收尾的执行记录"
            )
        self._rows[execution.key] = execution
        return execution

    def complete(
        self,
        key: str,
        result: StepResult,
        context_update: Mapping[str, Any] | None,
        finished_at: datetime,
    ) -> StepExecution:
        row = self._open_row(key)
        done = replace(
            row,
            status=StepExecutionStatus.COMPLETED,
            result=result,
            context_update=dict(context_update) if context_update is not None else None,
            finished_at=finished_at,
        )
        self._rows[key] = done
        return done

    def mark_recovered(self, key: str, finished_at: datetime) -> StepExecution:
        row = self._open_row(key)
        done = replace(
            row,
            status=StepExecutionStatus.RECOVERED,
            finished_at=finished_at,
        )
        self._rows[key] = done
        return done

    def find_open(self, instance_id: str) -> StepExecution | None:
        for row in self._rows.values():
            if (
                row.instance_id == instance_id
                and row.status is StepExecutionStatus.STARTED
            ):
                return row
        return None

    def last_completed(self, instance_id: str) -> StepExecution | None:
        """dict 保持插入顺序，所以最后一个匹配项就是最近收尾的那条。

        不用 finished_at 排序：测试和同一轮推进里时间戳可能完全相同，
        排序结果不稳定；写入顺序才是真实的先后。
        """
        latest = None
        for row in self._rows.values():
            if (
                row.instance_id == instance_id
                and row.status is not StepExecutionStatus.STARTED
            ):
                latest = row
        return latest

    def list_for_instance(self, instance_id: str) -> list[StepExecution]:
        return [
            row for row in self._rows.values() if row.instance_id == instance_id
        ]

    def load(self, key: str) -> StepExecution:
        try:
            return self._rows[key]
        except KeyError:
            raise ExecutionNotFound(f"执行记录不存在：{key}") from None

    def _open_row(self, key: str) -> StepExecution:
        row = self.load(key)
        if row.status is not StepExecutionStatus.STARTED:
            raise ExecutionNotOpen(f"执行记录 {key} 当前是 {row.status}，不能再收尾")
        return row
