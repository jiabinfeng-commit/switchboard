from datetime import datetime, timezone

from switchboard.agent.input import ModelView, StepInput, ToolSpec
from switchboard.agent.prompt import build_data_prompt, build_system_prompt
from switchboard.agent.tool import ToolInvocation

VIEW = ModelView(
    user_message="我电脑开不了机，序列号 SN12345",
    tools=(
        ToolSpec(
            name="asset_search",
            description="按序列号查资产",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        ),
    ),
    output_schema={"asset_id": "资产 id"},
    now=datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc),
)


def test_system_prompt_contains_tools_and_schema():
    prompt = build_system_prompt(VIEW)
    assert "asset_search" in prompt
    assert "asset_id" in prompt
    assert '"action"' in prompt


def test_data_prompt_marks_sections_as_data():
    prompt = build_data_prompt(VIEW, {}, None)
    assert "<员工输入>" in prompt
    assert "</员工输入>" in prompt
    assert "SN12345" in prompt


def test_feedback_is_rendered():
    prompt = build_data_prompt(VIEW, {}, "asset_id: value_mismatch")
    assert "value_mismatch" in prompt


def test_tool_results_are_included_with_call_ids():
    invocations = {
        "c-1": ToolInvocation(
            call_id="c-1", tool_name="asset_search", result=[{"id": 12345}]
        )
    }
    prompt = build_data_prompt(VIEW, invocations, None)
    assert "c-1" in prompt
    assert "12345" in prompt


def test_identity_never_reaches_the_model():
    # 身份字段不在 ModelView 里，这段代码手上没有它们。
    # 用哨兵值断言，防止以后有人把它们塞进 view。
    step_input = StepInput(view=VIEW, user_id="SECRET-USER-ID", allowed_entities=(424242,))
    prompts = (
        build_system_prompt(step_input.view)
        + build_data_prompt(step_input.view, {}, None)
    )
    assert "SECRET-USER-ID" not in prompts
    assert "424242" not in prompts
