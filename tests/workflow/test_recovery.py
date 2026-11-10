from __future__ import annotations

import pytest

from switchboard.workflow.engine import InstanceStatus, WaitReason
from switchboard.workflow.execution import (
    InMemoryStepExecutionStore,
    StepExecutionStatus,
)
from tests.workflow.harness import ScriptedExecutor, Stores, need_human, ok


def crash(node, instance, idempotency_key):
    """模拟进程在执行节点期间死掉：异常冒泡，执行记录保持 STARTED。"""
    raise RuntimeError("进程崩溃")


class FailOnceComplete:
    """第一次收尾执行记录时抛错，模拟「实例已保存、记录没来得及写」。

    其余方法直接委托，测试只关心 complete 这一次失败。
    """

    def __init__(self, inner: InMemoryStepExecutionStore) -> None:
        self._inner = inner
        self._failed = False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def complete(self, *args, **kwargs):
        if not self._failed:
            self._failed = True
            raise RuntimeError("进程崩溃")
        return self._inner.complete(*args, **kwargs)


def test_recover_reruns_open_step_with_same_key(definition):
    stores = Stores()
    with pytest.raises(RuntimeError):
        stores.runner(definition, crash).start("i-1")

    open_row = stores.executions.find_open("i-1")
    assert open_row is not None
    assert open_row.status is StepExecutionStatus.STARTED

    executor = ScriptedExecutor(ok())
    instance = stores.runner(definition, executor).recover("i-1")

    assert instance.current_node == "confirm"
    assert instance.status is InstanceStatus.SUSPENDED
    assert executor.calls == [("draft", open_row.key)]
    [row] = stores.executions.list_for_instance("i-1")
    assert row.status is StepExecutionStatus.COMPLETED


def test_recover_marks_stale_record_without_rerun(definition):
    """实例已经迁移、执行记录没收尾时，恢复不能重跑那一步。

    顺序是「先保存实例、再收尾记录」，所以这个窗口里副作用已经发生。
    再执行一次就是重复建单。
    """
    stores = Stores()
    executor = ScriptedExecutor(ok())
    runner1 = stores.runner(
        definition, executor, executions=FailOnceComplete(stores.executions)
    )
    with pytest.raises(RuntimeError):
        runner1.start("i-1")

    # 实例已停在 confirm，但仍是 running；执行记录还是 STARTED。
    crashed = stores.instances.load("i-1")
    assert crashed.current_node == "confirm"
    assert crashed.status is InstanceStatus.RUNNING

    recovered = stores.runner(definition, executor).recover("i-1")

    assert recovered.current_node == "confirm"
    assert recovered.status is InstanceStatus.SUSPENDED
    # 没有第二次执行 draft。
    assert len(executor.calls) == 1
    [row] = stores.executions.list_for_instance("i-1")
    assert row.status is StepExecutionStatus.RECOVERED


def test_recover_does_not_advance_user_wait(definition):
    stores = Stores()
    stores.runner(definition, ScriptedExecutor(ok())).start("i-1")

    executor = ScriptedExecutor()
    recovered = stores.runner(definition, executor).recover("i-1")

    assert recovered.status is InstanceStatus.SUSPENDED
    assert recovered.suspend_reason is WaitReason.USER
    assert executor.calls == []


def test_recover_recreates_missing_human_task(definition):
    stores = Stores()
    executor = ScriptedExecutor(ok(), need_human("glpi_timeout"))
    runner1 = stores.runner(definition, executor, human_tasks=None)
    runner1.start("i-1")
    runner1.resume("i-1", "confirmed")

    assert stores.human_tasks.find_open("i-1") is None

    stores.runner(definition, ScriptedExecutor()).recover("i-1")

    task = stores.human_tasks.find_open("i-1")
    assert task is not None
    assert task.node == "manual"
    assert task.cause == "glpi_timeout"
    assert task.may_have_committed is True
    assert task.allowed_outcomes == {"existing", "retry", "abandoned"}


def test_recover_all_scans_active_instances(definition):
    stores = Stores()
    executor = ScriptedExecutor(ok(), ok())
    runner = stores.runner(definition, executor)
    runner.start("i-1")
    runner.start("i-2")

    recovered = stores.runner(definition, ScriptedExecutor()).recover_all()

    assert [instance.id for instance in recovered] == ["i-1", "i-2"]
    assert all(
        instance.status is InstanceStatus.SUSPENDED for instance in recovered
    )
