"""2b 测试用的装配辅助。

单独放一份而不是复用 test_runner 里的 build：这里需要拿到执行记录和
人工任务存储，才能断言幂等键、记录状态和任务字段。
"""

from __future__ import annotations

from datetime import datetime, timezone

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.execution import InMemoryStepExecutionStore
from switchboard.workflow.human import InMemoryHumanTaskStore
from switchboard.workflow.runner import Execution, WorkflowRunner
from switchboard.workflow.store import InMemoryInstanceStore

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def ok(committed: bool = False) -> Execution:
    """构造一次成功执行。"""
    return Execution(StepResult(StepAction.SUCCEEDED, committed))


def need_human(cause: str, committed: bool = True) -> Execution:
    return Execution(StepResult(StepAction.NEED_HUMAN, committed, cause=cause))


class ScriptedExecutor:
    """按顺序返回预设结果，并记录每次执行的节点名和幂等键。"""

    def __init__(self, *executions: Execution) -> None:
        self._queue = list(executions)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, node, instance, idempotency_key) -> Execution:
        self.calls.append((node.name, idempotency_key))
        return self._queue.pop(0)


class Stores:
    """一组内存存储，测试里可以让多个 runner 共享它们模拟进程重启。"""

    def __init__(self) -> None:
        self.instances = InMemoryInstanceStore()
        self.executions = InMemoryStepExecutionStore()
        self.human_tasks = InMemoryHumanTaskStore()

    def runner(
        self,
        definition,
        executor,
        *,
        executions=None,
        human_tasks="default",
        clock=lambda: NOW,
    ) -> WorkflowRunner:
        """装配 runner。

        executions 可替换成包装过的存储（模拟某一刻写失败）；
        human_tasks 传 None 用来模拟「挂起落了、任务没建出来」的残留。
        """
        return WorkflowRunner(
            definition,
            self.instances,
            executor,
            executions if executions is not None else self.executions,
            human_tasks=(
                self.human_tasks if human_tasks == "default" else human_tasks
            ),
            clock=clock,
        )
