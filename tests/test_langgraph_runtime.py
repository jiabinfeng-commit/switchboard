"""LangGraph 实现与代码循环实现的行为对照。

同一套脚本、同一套输入分别喂给两个 runtime，要求 StepResult 和每轮
feedback 完全一致。差异只能来自实现，不能来自用例——所以复用
test_runtime 里的模型脚本和输入构造函数。
"""

from typing import Any, Callable

import pytest

from switchboard.agent.input import ModelView, StepInput, ToolSideEffect, ToolSpec
from switchboard.agent.langgraph_runtime import LangGraphRuntime
from switchboard.agent.questions import AskKind, AskUser
from switchboard.agent.result import StepAction
from switchboard.agent.runtime import (
    AgentRuntime,
    Draft,
    ModelError,
    RuntimeLimits,
    SingleAgentRuntime,
    ToolCallRequest,
)
from switchboard.agent.verify import ValueRef
from tests.test_runtime import (
    ScriptedModel,
    asset_search,
    make_input,
    make_input_with_schema,
)


ModelFactory = Callable[[list[Any]], ScriptedModel]


def run_both(
    outputs: list[Any],
    *,
    tools: dict | None = None,
    limits: RuntimeLimits = RuntimeLimits(),
    step_input=None,
    model_factory: ModelFactory = ScriptedModel,
):
    """用两个实现各跑一遍，断言结果一致后返回 (结果, 各自的模型)。

    模型脚本会被 pop，所以两边必须各造一个 model；输出对象是不可变的，
    共享同一批对象不会互相影响。
    """
    executor_map = tools if tools is not None else {"asset_search": asset_search}
    input_ = step_input or make_input()

    single_model = model_factory(list(outputs))
    graph_model = model_factory(list(outputs))
    single = SingleAgentRuntime(single_model, executor_map, limits).run(input_, "test-key")
    graph = LangGraphRuntime(graph_model, executor_map, limits).run(input_, "test-key")

    assert single == graph
    assert single_model.feedbacks == graph_model.feedbacks
    return single, single_model, graph_model


def test_parity_tool_then_verified_draft():
    result, _, _ = run_both(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )

    assert result.action is StepAction.SUCCEEDED
    assert result.evidence == ("c-1",)


def test_parity_ask_user():
    result, _, _ = run_both(
        [
            ToolCallRequest("asset_search", {"q": "张三"}),
            AskUser(kind=AskKind.MISSING_FIELD, field="asset_serial", label="设备序列号"),
        ],
        step_input=make_input_with_schema(),
    )

    assert result.action is StepAction.NEED_INPUT_FIX
    assert result.payload == "为了继续处理，还需要您提供设备序列号。"


def test_parity_fabricated_value_is_sent_back_and_corrected():
    result, _, model = run_both(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(999, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )

    assert result.action is StepAction.SUCCEEDED
    assert "value_mismatch" in model.feedbacks[2]


def test_parity_verification_budget_exhausted():
    bad = Draft({"asset_id": ValueRef(999, "c-1", "[0].id")})
    result, _, _ = run_both(
        [ToolCallRequest("asset_search", {"q": "SN12345"}), bad, bad, bad]
    )

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "verification_failed"


def test_parity_invalid_ask_budget_exhausted():
    bad = AskUser(kind=AskKind.NO_RESULT, call_ids=("c-1",))
    result, _, model = run_both(
        [ToolCallRequest("asset_search", {"q": "SN12345"}), bad, bad, bad]
    )

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "invalid_ask"
    assert "非空结果" in model.feedbacks[2]


def test_parity_turn_budget_exhausted():
    result, _, model = run_both(
        [ToolCallRequest("asset_search", {"q": "x"})] * 6,
        limits=RuntimeLimits(max_model_turns=6),
    )

    assert result.action is StepAction.NEED_HUMAN
    assert result.cause == "turn_budget_exhausted"
    assert len(model.feedbacks) == 6


def test_parity_tool_outside_whitelist_gets_new_call_id():
    result, _, _ = run_both(
        [
            ToolCallRequest("delete_everything", {}),
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-2", "[0].id")}),
        ]
    )

    assert result.action is StepAction.SUCCEEDED
    assert result.evidence == ("c-2",)


def test_parity_executor_exception_is_recorded():
    def broken(arguments):
        """模拟下游超时：失败要变成调用记录，而不是让整步崩掉。"""
        raise TimeoutError("glpi timeout")

    bad = Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")})
    result, _, model = run_both(
        [ToolCallRequest("asset_search", {"q": "SN12345"}), bad, bad, bad],
        tools={"asset_search": broken},
    )

    assert result.action is StepAction.NEED_HUMAN
    assert "execution_error" in model.feedbacks[1]


def test_parity_unparsable_output_does_not_use_verify_budget():
    class FlakyModel(ScriptedModel):
        def respond(self, view, invocations, feedback):
            """第一轮抛 ModelError 模拟格式坏了，之后恢复正常脚本。"""
            if not self.feedbacks:
                self.feedbacks.append(feedback)
                raise ModelError("not json")
            return super().respond(view, invocations, feedback)

    result, _, _ = run_both(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ],
        model_factory=FlakyModel,
    )

    assert result.action is StepAction.SUCCEEDED


def test_both_implementations_satisfy_the_protocol():
    """外层只认契约；换实现不应该改调用方。"""
    for cls in (SingleAgentRuntime, LangGraphRuntime):
        assert isinstance(cls(ScriptedModel([]), {}), AgentRuntime)


def test_missing_executor_is_a_wiring_error():
    runtime = LangGraphRuntime(ScriptedModel([]), tools={})
    with pytest.raises(RuntimeError, match="没有执行器"):
        runtime.run(make_input(), "test-key")


def test_write_tools_are_rejected_before_any_call():
    """写工具进 agent 会让整步重跑不再安全，必须在装配时挡掉。

    两个实现都必须拒绝；只挡一个的话，换实现就把安全属性弄丢了。
    """
    called = []
    spec = ToolSpec(
        name="create_ticket",
        description="建单",
        parameters={"type": "object", "properties": {}},
        side_effect=ToolSideEffect.WRITE,
    )
    step_input = StepInput(
        view=ModelView(user_message="建个单", tools=(spec,)),
        user_id="u-1",
    )

    for cls in (SingleAgentRuntime, LangGraphRuntime):
        runtime = cls(ScriptedModel([]), {"create_ticket": called.append})
        with pytest.raises(RuntimeError, match="只读"):
            runtime.run(step_input, "test-key")

    assert called == []


def test_graph_visits_nodes_in_the_same_order_as_the_loop():
    """轨迹要能还原「为什么是这个结果」：先看走过哪些节点。"""
    seen: list[str] = []

    model = ScriptedModel(
        [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(999, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]
    )
    runtime = LangGraphRuntime(
        model,
        {"asset_search": asset_search},
        on_node=lambda node, update: seen.append(node),
    )
    result = runtime.run(make_input(), "outer-key-123")

    assert result.action is StepAction.SUCCEEDED
    assert seen == ["model", "tool", "model", "draft", "model", "draft"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
