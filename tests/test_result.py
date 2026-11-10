import pytest

from switchboard.agent.result import StepAction, StepResult


@pytest.mark.parametrize(
    ("action", "committed"),
    [
        # 未发出、明确被拒：确定没有副作用
        (StepAction.NEED_INPUT_FIX, False),
        (StepAction.RETRYABLE, False),
        # 需人工发生在发出前（403）或发出后（超时），两种都合法
        (StepAction.NEED_HUMAN, False),
        (StepAction.NEED_HUMAN, True),
        # 写步骤成功（已建单）
        (StepAction.SUCCEEDED, True),
        # 只读步骤成功（查到资产），没有任何副作用
        (StepAction.SUCCEEDED, False),
        # 成功但结果与预期不一致，写/读两种都可能
        (StepAction.SUCCEEDED_NEEDS_REVIEW, True),
        (StepAction.SUCCEEDED_NEEDS_REVIEW, False),
    ],
)
def test_legal_combinations(action, committed):
    result = StepResult(action=action, may_have_committed=committed)
    assert result.action is action


@pytest.mark.parametrize(
    ("action", "committed"),
    [
        # 输入没通过校验时不可能已经产生副作用
        (StepAction.NEED_INPUT_FIX, True),
        # 这是最危险的一种组合：标成可重试，却可能已经建过单
        (StepAction.RETRYABLE, True),
    ],
)
def test_illegal_combinations_raise(action, committed):
    with pytest.raises(ValueError):
        StepResult(action=action, may_have_committed=committed)


def test_unknown_outcome_must_be_need_human():
    # GLPI 超时属于「已发出、结果未知」。
    # 不能表达成 retryable，否则流程重试时可能建出第二张单。
    with pytest.raises(ValueError):
        StepResult(action=StepAction.RETRYABLE, may_have_committed=True)

    # 正确表达：交给人，并标出副作用可能已发生，让处理者先核对再决定
    result = StepResult(
        action=StepAction.NEED_HUMAN,
        may_have_committed=True,
        cause="glpi_timeout",
    )
    assert result.may_have_committed is True
