from typing import Any, Mapping

import pytest

from switchboard.agent.input import ModelView, StepInput, ToolSpec
from switchboard.agent.result import StepAction
from switchboard.agent.questions import AskKind, AskUser
from switchboard.agent.runtime import (
    AgentRuntime,
    Draft,
    ModelError,
    RuntimeLimits,
    SingleAgentRuntime,
    ToolCallRequest,
)
from switchboard.agent.tool import ToolInvocation
from switchboard.agent.verify import ValueRef

ASSETS = [{"id": 12345, "model": "ThinkPad T14"}]


class ScriptedModel:
    """按脚本依次返回，记录每轮收到的 feedback，便于断言打回是否生效。"""

    def __init__(self, outputs: list[Any]) -> None:
        """保存按顺序返回的输出脚本，并初始化 feedback 记录列表。"""
        self._outputs = list(outputs)
        self.feedbacks: list[str | None] = []

    def respond(
        self,
        view: ModelView,
        invocations: Mapping[str, ToolInvocation],
        feedback: str | None,
    ):
        """记录本轮收到的 feedback，返回脚本里的下一个输出。

        脚本耗尽时 pop 会抛 IndexError，说明 runtime 调模型的次数超过预期，
        测试应当失败而不是悄悄重复最后一个输出。
        """
        self.feedbacks.append(feedback)
        return self._outputs.pop(0)


def make_input() -> StepInput:
    """构造一份最小可用的 StepInput：一个带参数 schema 的工具，无输出 schema。"""

    return StepInput(
        view=ModelView(
            user_message="我电脑开不了机，序列号 SN12345",
            tools=(
                ToolSpec(
                    name="asset_search",
                    description="按序列号查资产",
                    parameters={
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                        "additionalProperties": False,
                    },
                ),
            ),
        ),
        user_id="u-1",
        allowed_entities=(3,),
    )


def make_input_with_schema() -> StepInput:
    """在 make_input 基础上补一个输出 schema，用于追问相关的用例。"""

    base = make_input()
    return StepInput(
        view=ModelView(
            user_message=base.view.user_message,
            tools=base.view.tools,
            output_schema={"asset_id": "资产 id", "asset_serial": "设备序列号"},
        ),
        user_id=base.user_id,
        allowed_entities=base.allowed_entities,
    )


def asset_search(arguments: Mapping[str, Any]):
    """假工具：不检查参数，永远返回同一条资产记录。"""
    return ASSETS


def run(model, tools=None, limits=RuntimeLimits(), step_input=None):
    """装配 runtime 并执行一步，返回 StepResult。

    默认挂一个永远返回 ASSETS 的 asset_search；用例要测白名单或执行器
    故障时自己传 tools 覆盖。
    """
    runtime = SingleAgentRuntime(model, tools or {"asset_search": asset_search}, limits)
    return runtime.run(step_input or make_input(), "test-key")


def test_tool_then_verified_draft_succeeds():
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )
    result = run(model)

    assert result.action is StepAction.SUCCEEDED
    assert result.may_have_committed is False
    assert result.evidence == ("c-1",)


def test_fabricated_value_is_sent_back_and_corrected():
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            # 第一版草稿编了一个结果里没有的 id
            Draft({"asset_id": ValueRef(999, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )
    result = run(model)

    assert result.action is StepAction.SUCCEEDED
    assert model.feedbacks[1] is None          # 工具调用成功，无反馈
    assert "value_mismatch" in model.feedbacks[2]


def test_always_fabricating_ends_as_need_human():
    bad = Draft({"asset_id": ValueRef(999, "c-1", "[0].id")})
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            bad,
            bad,
            bad,
        ]
    )
    result = run(model)

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "verification_failed"
    # 用户补信息解决不了模型乱引的问题，所以不是 need_input_fix
    assert result.may_have_committed is False


def test_ask_user_keeps_the_invocations_it_was_based_on():
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "张三"}),
            AskUser(kind=AskKind.MISSING_FIELD, field="asset_serial", label="设备序列号"),
        ]
    )
    result = run(model, step_input=make_input_with_schema())

    assert result.action is StepAction.NEED_INPUT_FIX
    # 追问基于哪些调用，必须留下记录
    assert result.evidence == ("c-1",)
    assert result.payload == "为了继续处理，还需要您提供设备序列号。"


def test_no_result_requires_an_empty_successful_call():
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN999"}),
            AskUser(kind=AskKind.NO_RESULT, call_ids=("c-1",)),
        ]
    )
    result = run(model, tools={"asset_search": lambda arguments: []})

    assert result.action is StepAction.NEED_INPUT_FIX
    assert result.evidence == ("c-1",)
    assert "没有查到" in result.payload


def test_claiming_no_result_when_the_call_returned_data_is_rejected():
    # 调用返回了资产，模型却说没查到——这句话现在说不出来了
    bad = AskUser(kind=AskKind.NO_RESULT, call_ids=("c-1",))
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            bad,
            bad,
            bad,
        ]
    )
    result = run(model)

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "invalid_ask"
    assert "非空结果" in model.feedbacks[2]


def test_ambiguous_asset_candidates_must_be_traceable():
    bad = AskUser(
        kind=AskKind.AMBIGUOUS_ASSET,
        refs=(ValueRef(12345, "c-1", "[0].id"), ValueRef(99999, "c-1", "[0].id")),
    )
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "张三"}),
            bad,
            bad,
            bad,
        ]
    )
    result = run(model)

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "invalid_ask"


def test_tool_outside_whitelist_is_not_executed():
    called = []

    def dangerous(arguments):
        """记录被调用的参数；如果真被执行，called 会非空，用例据此断言。"""
        called.append(arguments)
        return "boom"

    model = ScriptedModel(
        [
            ToolCallRequest("delete_everything", {}),
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-2", "[0].id")}),
        ]
    )
    result = run(model, tools={"asset_search": asset_search, "delete_everything": dangerous})

    assert result.action is StepAction.SUCCEEDED
    assert called == []                                   # 没被执行
    assert "tool_not_allowed" in model.feedbacks[1]


def test_arguments_not_matching_schema_are_not_executed():
    # 模型自己发明参数名（serial 而不是 q）。如果不校验，执行器读不到 q
    # 会返回空列表，模型会把它当成「查无记录」——错误被伪装成空结果。
    called = []

    def recording_search(arguments):
        """记录每次实际收到的参数，用来验证非法参数从未到达执行器。"""
        called.append(dict(arguments))
        return ASSETS

    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"serial": "SN12345"}),
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-2", "[0].id")}),
        ]
    )
    result = run(model, tools={"asset_search": recording_search})

    assert result.action is StepAction.SUCCEEDED
    assert called == [{"q": "SN12345"}]          # 第一次没有执行
    assert "invalid_arguments" in model.feedbacks[1]


def test_identity_argument_is_rejected():
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345", "user_id": "u-9"}),
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-2", "[0].id")}),
        ]
    )
    result = run(model)

    assert result.action is StepAction.SUCCEEDED
    assert "forbidden_argument" in model.feedbacks[1]


def test_executor_exception_is_recorded_as_failed_invocation():
    def broken(arguments):
        """模拟下游超时，验证异常被收敛成一条失败调用记录而不是中断循环。"""
        raise TimeoutError("glpi timeout")

    bad = Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")})
    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            bad,
            bad,
            bad,
        ]
    )
    result = run(model, tools={"asset_search": broken})

    assert result.action is StepAction.NEED_HUMAN
    assert "execution_error" in model.feedbacks[1]


def test_reference_to_failed_call_is_rejected():
    def broken(arguments):
        """模拟下游超时，验证异常被收敛成一条失败调用记录而不是中断循环。"""
        raise TimeoutError("glpi timeout")

    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )
    result = run(model, tools={"asset_search": broken})

    assert result.action is StepAction.NEED_HUMAN
    assert "call_failed" in model.feedbacks[2]


def test_unparsable_output_is_retried_without_using_verify_budget():
    class FlakyModel(ScriptedModel):
        def respond(self, view, invocations, feedback):
            """第一轮抛 ModelError 模拟格式坏了，之后恢复正常脚本。"""
            if not self.feedbacks:
                self.feedbacks.append(feedback)
                raise ModelError("not json")
            return super().respond(view, invocations, feedback)

    model = FlakyModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )
    result = run(model)

    assert result.action is StepAction.SUCCEEDED
    assert "无法解析" in model.feedbacks[1]


def test_missing_executor_is_a_wiring_error():
    runtime = SingleAgentRuntime(ScriptedModel([]), tools={})
    with pytest.raises(RuntimeError, match="没有执行器"):
        runtime.run(make_input(), "test-key")


def test_single_runtime_satisfies_agent_runtime_protocol():
    """接口是给外层看的；实现换不换，外层都不该知道。"""
    runtime = SingleAgentRuntime(ScriptedModel([]), tools={})
    assert isinstance(runtime, AgentRuntime)


def test_turn_budget_exhausted():
    # 模型一直调工具、从不出草稿
    model = ScriptedModel([ToolCallRequest("asset_search", {"q": "x"})] * 6)
    result = run(model, limits=RuntimeLimits(max_model_turns=6))

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "turn_budget_exhausted"
