from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.definition import NodeType, load_definition
from switchboard.workflow.engine import (
    ContextConflict,
    Finish,
    IllegalState,
    InstanceStatus,
    InvalidOutcome,
    RunStep,
    Suspend,
    WaitReason,
    apply_outcome,
    apply_result,
    next_action,
    start_instance,
    timeout_outcome,
)
from tests.workflow.helpers import step_next

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def ok(committed: bool = False) -> StepResult:
    """构造一个成功结果；默认没有副作用，写步骤测试时传 committed=True。"""
    return StepResult(StepAction.SUCCEEDED, committed)


def started(definition):
    """创建停在 start 节点、尚未推进的实例。"""
    return start_instance(definition, "i-1")


def test_start_sets_first_node_and_version(definition):
    instance = started(definition)
    assert instance.current_node == "draft"
    assert instance.status is InstanceStatus.RUNNING
    assert instance.workflow_version == 1
    assert instance.attempt == 1


def test_step_node_plans_run(definition):
    plan = next_action(definition, started(definition), NOW)
    assert isinstance(plan, RunStep)
    assert plan.node.name == "draft"
    assert plan.attempt == 1


def test_wait_node_plans_suspend_with_deadline(definition):
    instance = apply_result(definition, started(definition), ok())
    plan = next_action(definition, instance, NOW)
    assert isinstance(plan, Suspend)
    assert plan.reason is WaitReason.USER
    assert plan.deadline == NOW + timedelta(hours=24)


def test_end_plans_finish(definition):
    instance = apply_result(definition, started(definition), ok())
    instance = apply_outcome(definition, instance, "timeout")
    plan = next_action(definition, instance, NOW)
    assert isinstance(plan, Finish)
    assert plan.status is InstanceStatus.COMPLETED


def test_success_moves_to_next_and_merges_context(definition):
    instance = apply_result(
        definition, started(definition), ok(), context_update={"asset_id": 12345}
    )
    assert instance.current_node == "confirm"
    assert instance.context == {"asset_id": 12345}
    assert instance.attempt == 1


def test_need_input_fix_goes_to_ask(definition):
    result = StepResult(StepAction.NEED_INPUT_FIX, False, cause="missing_serial")
    instance = apply_result(definition, started(definition), result)
    assert instance.current_node == "ask"


def test_retryable_stays_and_counts_attempts(definition):
    result = StepResult(StepAction.RETRYABLE, False, cause="tool_timeout")
    first = apply_result(definition, started(definition), result)
    assert first.current_node == "draft"
    assert first.attempt == 2

    second = apply_result(definition, first, result)
    assert second.current_node == "manual"
    assert second.attempt == 1


def test_retry_exhausted_after_max_attempts(definition):
    result = StepResult(StepAction.RETRYABLE, False, cause="tool_timeout")
    one = apply_result(definition, started(definition), result)
    # draft 的 max_attempts 是 2，第二次已经用尽
    two = apply_result(definition, one, result)
    assert two.current_node == "manual"


def test_wait_outcome_moves(definition):
    instance = apply_result(definition, started(definition), ok())
    instance = apply_outcome(definition, instance, "confirmed")
    assert instance.current_node == "create"


def test_undeclared_wait_outcome_rejected(definition):
    instance = apply_result(definition, started(definition), ok())
    with pytest.raises(InvalidOutcome, match="confirmed"):
        apply_outcome(definition, instance, "maybe")


def test_outcome_on_step_node_rejected(definition):
    with pytest.raises(IllegalState):
        apply_outcome(definition, started(definition), "confirmed")


def test_result_on_wait_node_rejected(definition):
    instance = apply_result(definition, started(definition), ok())
    with pytest.raises(IllegalState):
        apply_result(definition, instance, ok())


def test_other_node_cannot_overwrite_context_key(definition):
    instance = apply_result(
        definition, started(definition), ok(), context_update={"asset_id": 1}
    )
    with pytest.raises(ContextConflict, match="asset_id"):
        apply_outcome(definition, instance, "confirmed", context_update={"asset_id": 2})


def test_same_node_rerun_may_overwrite_its_own_keys(definition):
    # 用户要求修改后回到 draft，draft 重新生成 asset_id 是正常的覆盖
    first = apply_result(
        definition, started(definition), ok(), context_update={"asset_id": 1}
    )
    back = apply_outcome(definition, first, "revised")
    second = apply_result(definition, back, ok(), context_update={"asset_id": 2})

    assert second.context["asset_id"] == 2
    assert second.context_owners["asset_id"] == "draft"


def test_initial_context_cannot_be_overwritten(definition):
    instance = start_instance(definition, "i-1", context={"user_id": "u-1001"})
    with pytest.raises(ContextConflict, match="user_id"):
        apply_result(
            definition, instance, ok(), context_update={"user_id": "u-9999"}
        )


def test_self_loop_increments_attempt_other_node_resets(definition):
    result = StepResult(StepAction.RETRYABLE, False)
    instance = apply_result(definition, started(definition), result)
    assert (instance.current_node, instance.attempt) == ("draft", 2)

    instance = apply_result(definition, instance, ok())
    assert (instance.current_node, instance.attempt) == ("confirm", 1)


def test_timer_node_fires_with_its_own_outcome(definition):
    step = step_next("a", success="hold")
    step.update(
        need_input_fix="end", need_human="end", retry_exhausted="end"
    )
    timer_flow = load_definition(
        {
            "name": "timer_test",
            "version": 1,
            "start": "a",
            "nodes": {
                "a": {"type": "agent", "step": "s", "next": step},
                "hold": {
                    "type": "wait_timer",
                    "timeout": "10m",
                    "next": {"fired": "end"},
                },
                "end": {"type": "end"},
            },
        }
    )
    instance = apply_result(timer_flow, started(timer_flow), ok())
    plan = next_action(timer_flow, instance, NOW)
    assert isinstance(plan, Suspend)
    assert plan.reason is WaitReason.TIMER
    assert timeout_outcome(plan.node) == "fired"
    assert timeout_outcome(definition.node("confirm")) == "timeout"


def test_suspended_instance_cannot_advance(definition):
    instance = apply_result(definition, started(definition), ok())
    instance = replace(instance, status=InstanceStatus.SUSPENDED)
    with pytest.raises(IllegalState):
        next_action(definition, instance, NOW)
