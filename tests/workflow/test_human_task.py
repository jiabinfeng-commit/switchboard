from __future__ import annotations

import pytest

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.engine import IllegalState, InstanceStatus, WaitReason
from switchboard.workflow.human import (
    HumanTaskNotOpen,
    HumanTaskStatus,
    InvalidHumanOutcome,
)
from tests.workflow.harness import (
    NOW,
    Execution,
    ScriptedExecutor,
    Stores,
    need_human,
    ok,
)


def reach_manual(definition, stores, *extra):
    """跑一遍正常路径，把实例停在人工节点，返回 runner 和任务。

    extra 是人工处置之后才用得上的脚本，由调用方预先排进去。
    """
    executor = ScriptedExecutor(ok(), need_human("glpi_timeout"), *extra)
    runner = stores.runner(definition, executor)
    runner.start("i-1")
    instance = runner.resume("i-1", "confirmed")
    assert instance.suspend_reason is WaitReason.HUMAN

    task = stores.human_tasks.find_open("i-1")
    assert task is not None
    return runner, task


def test_need_human_creates_task_with_execution_details(definition):
    stores = Stores()
    _, task = reach_manual(definition, stores)

    assert task.node == "manual"
    assert task.cause == "glpi_timeout"
    assert task.may_have_committed is True
    assert task.allowed_outcomes == {"existing", "retry", "abandoned"}
    assert task.status is HumanTaskStatus.OPEN
    assert task.created_at == NOW
    assert task.deadline == stores.instances.load("i-1").deadline


def test_submit_existing_wakes_instance(definition):
    stores = Stores()
    runner, task = reach_manual(definition, stores)

    instance = runner.submit_human_task(task.id, "existing")

    assert instance.current_node == "track"
    assert instance.status is InstanceStatus.SUSPENDED
    assert instance.suspend_reason is WaitReason.EVENT
    done = stores.human_tasks.load(task.id)
    assert done.status is HumanTaskStatus.SUBMITTED
    assert done.submitted_outcome == "existing"
    assert done.submitted_at == NOW


def test_submit_retry_reruns_create(definition):
    stores = Stores()
    runner, task = reach_manual(
        definition,
        stores,
        Execution(StepResult(StepAction.SUCCEEDED, True), {"ticket_id": 9001}),
        Execution(StepResult(StepAction.SUCCEEDED, False)),
    )

    instance = runner.submit_human_task(task.id, "retry")

    assert instance.current_node == "track"
    assert instance.context["ticket_id"] == 9001
    assert [name for name, _ in runner._execute.calls] == [
        "draft",
        "create",
        "create",
        "receipt",
    ]
    assert stores.human_tasks.find_open("i-1") is None


def test_timeout_is_not_a_human_outcome(definition):
    stores = Stores()
    runner, task = reach_manual(definition, stores)

    with pytest.raises(InvalidHumanOutcome):
        runner.submit_human_task(task.id, "timeout")


def test_duplicate_submit_cannot_wake_twice(definition):
    stores = Stores()
    runner, task = reach_manual(definition, stores)
    runner.submit_human_task(task.id, "existing")

    with pytest.raises(IllegalState):
        runner.submit_human_task(task.id, "existing")

    # 存储层同样挡得住：并发下两个请求都通过了实例状态检查，
    # 后到的那个会在这里被拒绝。
    with pytest.raises(HumanTaskNotOpen):
        stores.human_tasks.submit(task.id, "existing", NOW)


def test_submit_after_timeout_is_rejected(definition):
    stores = Stores()
    runner, task = reach_manual(definition, stores)
    runner.resume_timeout("i-1")

    with pytest.raises(IllegalState):
        runner.submit_human_task(task.id, "existing")

    # 任务仍然开着：清理过期任务属于定时器/补偿逻辑（2c），
    # 2b 只保证它不会被用来唤醒一个已经结束的实例。
    assert stores.human_tasks.find_open("i-1") is not None
