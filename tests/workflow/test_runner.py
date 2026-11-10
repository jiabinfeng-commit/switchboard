from __future__ import annotations

from datetime import datetime, timezone

import pytest

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.definition import load_definition
from switchboard.workflow.engine import InstanceStatus, IllegalState, WaitReason
from switchboard.workflow.execution import InMemoryStepExecutionStore
from switchboard.workflow.runner import Execution, WorkflowLoop, WorkflowRunner
from switchboard.workflow.store import DuplicateInstance, InMemoryInstanceStore
from tests.workflow.helpers import step_next

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def ok(committed: bool = False) -> Execution:
    """构造一次成功的执行，不往上下文写任何东西。"""
    return Execution(StepResult(StepAction.SUCCEEDED, committed))


class ScriptedExecutor:
    """按顺序返回预设结果，并记录实际执行了哪些节点。"""

    def __init__(self, *executions: Execution) -> None:
        """保存执行脚本；列表耗尽时抛 IndexError，暴露多执行的节点。"""
        self._queue = list(executions)
        self.calls: list[str] = []

    def __call__(self, node, instance, idempotency_key) -> Execution:
        """记录节点名，返回脚本里的下一份结果。"""
        self.calls.append(node.name)
        return self._queue.pop(0)


def build(definition, executor, *, on_suspend=None, max_steps=20):
    """用内存存储和可控时钟装配 runner，返回 runner 和 store。

    max_steps 调小一点，让「没有挂起的环」这类用例快速失败。
    """
    store = InMemoryInstanceStore()
    runner = WorkflowRunner(
        definition,
        store,
        executor,
        InMemoryStepExecutionStore(),
        clock=lambda: NOW,
        on_suspend=on_suspend,
        max_steps=max_steps,
    )
    return runner, store


def test_happy_path(definition):
    executor = ScriptedExecutor(
        Execution(ok().result, {"asset_id": 12345}),
        Execution(StepResult(StepAction.SUCCEEDED, True), {"ticket_id": 9001}),
        ok(),
    )
    suspended: list[str] = []
    runner, store = build(definition, executor, on_suspend=lambda node, _: suspended.append(node.name))

    instance = runner.start("i-1")
    assert instance.status is InstanceStatus.SUSPENDED
    assert instance.current_node == "confirm"
    assert instance.suspend_reason is WaitReason.USER
    assert suspended == ["confirm"]

    instance = runner.resume("i-1", "confirmed", {"user_answer": "对"})
    assert instance.current_node == "track"
    assert suspended == ["confirm", "track"]
    assert executor.calls == ["draft", "create", "receipt"]

    instance = runner.resume("i-1", "closed")
    assert instance.status is InstanceStatus.COMPLETED
    assert instance.context["ticket_id"] == 9001
    assert instance.context["asset_id"] == 12345
    assert store.load("i-1").status is InstanceStatus.COMPLETED


def test_revised_goes_back_to_draft(definition):
    executor = ScriptedExecutor(ok(), ok())
    runner, _ = build(definition, executor)

    runner.start("i-1")
    instance = runner.resume("i-1", "revised", {"last_user_message": "不是这台"})

    assert instance.current_node == "confirm"
    assert executor.calls == ["draft", "draft"]


def test_need_input_fix_suspends_for_user(definition):
    executor = ScriptedExecutor(
        Execution(StepResult(StepAction.NEED_INPUT_FIX, False, cause="missing_serial")),
        ok(),
    )
    runner, _ = build(definition, executor)

    instance = runner.start("i-1")
    assert instance.current_node == "ask"
    assert instance.status is InstanceStatus.SUSPENDED

    instance = runner.resume("i-1", "answered", {"last_user_message": "SN12345"})
    assert instance.current_node == "confirm"
    assert instance.context["last_user_message"] == "SN12345"


def test_retry_exhaustion_suspends_for_human(definition):
    executor = ScriptedExecutor(
        ok(),
        Execution(StepResult(StepAction.RETRYABLE, False, cause="timeout")),
        Execution(StepResult(StepAction.RETRYABLE, False, cause="timeout")),
    )
    runner, _ = build(definition, executor)

    runner.start("i-1")
    instance = runner.resume("i-1", "confirmed")

    assert instance.current_node == "manual"
    assert instance.status is InstanceStatus.SUSPENDED
    assert instance.suspend_reason is WaitReason.HUMAN
    assert executor.calls == ["draft", "create", "create"]


def test_timeout_ends_instance(definition):
    executor = ScriptedExecutor(ok())
    runner, _ = build(definition, executor)

    runner.start("i-1")
    instance = runner.resume_timeout("i-1")

    assert instance.status is InstanceStatus.COMPLETED
    assert instance.current_node == "end"


def test_resume_requires_suspended_instance(definition):
    executor = ScriptedExecutor(ok(), ok(), ok())
    runner, _ = build(definition, executor)
    runner.start("i-1")
    runner.resume("i-1", "confirmed")
    # 现在在 track 节点，挂起状态；先把它推进完
    runner.resume("i-1", "closed")

    with pytest.raises(IllegalState):
        runner.resume("i-1", "closed")


def test_duplicate_instance_rejected(definition):
    executor = ScriptedExecutor(ok())
    runner, _ = build(definition, executor)
    runner.start("i-1")
    with pytest.raises(DuplicateInstance):
        runner.start("i-1")


def test_loop_guard():
    flow = {
        "name": "loop",
        "version": 1,
        "start": "a",
        "nodes": {
            "a": {"type": "agent", "step": "s", "next": step_next("a", success="b")},
            "b": {"type": "agent", "step": "s", "next": step_next("b", success="a")},
            "end": {"type": "end"},
        },
    }
    # b 的失败分支要指向 end，否则 end 不可达，加载器会拒绝
    flow["nodes"]["a"]["next"]["need_human"] = "end"
    flow["nodes"]["a"]["next"]["need_input_fix"] = "end"
    flow["nodes"]["a"]["next"]["retry_exhausted"] = "end"
    flow["nodes"]["b"]["next"]["need_human"] = "end"
    flow["nodes"]["b"]["next"]["need_input_fix"] = "end"
    flow["nodes"]["b"]["next"]["retry_exhausted"] = "end"

    definition = load_definition(flow)
    executor = ScriptedExecutor(*[ok() for _ in range(10)])
    runner, _ = build(definition, executor, max_steps=5)

    with pytest.raises(WorkflowLoop):
        runner.start("i-1")
