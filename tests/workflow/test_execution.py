from __future__ import annotations

import pytest

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.execution import (
    ExecutionAlreadyOpen,
    ExecutionNotOpen,
    InMemoryStepExecutionStore,
    StepExecution,
    StepExecutionStatus,
)
from tests.workflow.harness import NOW, Execution, ScriptedExecutor, Stores, ok


def failed(action: StepAction) -> Execution:
    """构造一个不违反不变量的失败结果。"""
    return Execution(StepResult(action, False, cause="downstream"))


def test_completed_record_keeps_result_and_context(definition):
    stores = Stores()
    executor = ScriptedExecutor(Execution(ok().result, {"asset_id": 12345}))
    runner = stores.runner(definition, executor)

    runner.start("i-1")

    [row] = stores.executions.list_for_instance("i-1")
    assert row.node == "draft"
    assert row.attempt == 1
    assert row.status is StepExecutionStatus.COMPLETED
    assert row.key
    assert row.result is not None and row.result.action is StepAction.SUCCEEDED
    assert row.context_update == {"asset_id": 12345}
    assert row.requested_at == NOW
    assert row.finished_at == NOW


def test_each_step_gets_its_own_key(definition):
    stores = Stores()
    executor = ScriptedExecutor(
        Execution(ok().result, {"asset_id": 12345}),
        Execution(StepResult(StepAction.SUCCEEDED, True), {"ticket_id": 9001}),
        ok(),
    )
    runner = stores.runner(definition, executor)

    runner.start("i-1")
    runner.resume("i-1", "confirmed")

    rows = stores.executions.list_for_instance("i-1")
    assert [row.node for row in rows] == ["draft", "create", "receipt"]
    keys = [row.key for row in rows]
    assert len(set(keys)) == 3
    assert all(row.status is StepExecutionStatus.COMPLETED for row in rows)


def test_retry_gets_new_key_and_increments_attempt(definition):
    stores = Stores()
    executor = ScriptedExecutor(
        ok(),
        failed(StepAction.RETRYABLE),
        failed(StepAction.RETRYABLE),
    )
    runner = stores.runner(definition, executor)

    runner.start("i-1")
    runner.resume("i-1", "confirmed")

    rows = stores.executions.list_for_instance("i-1")
    creates = [row for row in rows if row.node == "create"]
    assert [(row.attempt, row.status) for row in creates] == [
        (1, StepExecutionStatus.COMPLETED),
        (2, StepExecutionStatus.COMPLETED),
    ]
    assert creates[0].key != creates[1].key


def test_loop_back_reuses_attempt_but_gets_new_key(definition):
    """确认 -> 修改 -> 回草稿，attempt 重置为 1，键不能跟着撞。

    这是「键绑定执行记录而不是 instance+node+attempt」的直接证据。
    """
    stores = Stores()
    executor = ScriptedExecutor(ok(), ok())
    runner = stores.runner(definition, executor)

    runner.start("i-1")
    runner.resume("i-1", "revised", {"last_user_message": "不是这台"})

    drafts = [row for row in stores.executions.list_for_instance("i-1") if row.node == "draft"]
    assert [row.attempt for row in drafts] == [1, 1]
    assert drafts[0].key != drafts[1].key


def test_store_rejects_second_open_record_for_same_instance():
    store = InMemoryStepExecutionStore()
    store.begin(_row("k-1", "i-1"))

    with pytest.raises(ExecutionAlreadyOpen):
        store.begin(_row("k-2", "i-1"))


def test_store_rejects_completing_twice():
    store = InMemoryStepExecutionStore()
    store.begin(_row("k-1", "i-1"))
    store.complete("k-1", StepResult(StepAction.SUCCEEDED, False), None, NOW)

    with pytest.raises(ExecutionNotOpen):
        store.complete("k-1", StepResult(StepAction.SUCCEEDED, False), None, NOW)


def test_last_completed_ignores_open_record():
    store = InMemoryStepExecutionStore()
    store.begin(_row("k-1", "i-1"))
    store.complete("k-1", StepResult(StepAction.SUCCEEDED, False), None, NOW)
    store.begin(_row("k-2", "i-1"))

    last = store.last_completed("i-1")
    assert last is not None and last.key == "k-1"


def _row(key: str, instance_id: str) -> StepExecution:
    return StepExecution(
        key=key,
        instance_id=instance_id,
        node="draft",
        attempt=1,
        status=StepExecutionStatus.STARTED,
        requested_at=NOW,
    )
