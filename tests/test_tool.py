import pytest

from switchboard.agent.tool import ToolInvocation


def test_successful_invocation():
    invocation = ToolInvocation(
        call_id="c-1",
        tool_name="asset_search",
        arguments={"q": "SN12345"},
        result=[{"id": 12345, "model": "ThinkPad T14"}],
    )
    assert invocation.ok
    # 返回值原样保留，不做裁剪
    assert invocation.result[0]["model"] == "ThinkPad T14"


def test_empty_result_is_still_success():
    # 查无记录不是失败：工具正常执行了，只是返回空
    invocation = ToolInvocation(call_id="c-2", tool_name="asset_search", result=[])
    assert invocation.ok
    assert invocation.result == []


def test_failed_invocation():
    invocation = ToolInvocation(
        call_id="c-3",
        tool_name="asset_search",
        error="timeout",
    )
    assert not invocation.ok
    assert invocation.result is None


def test_result_and_error_are_exclusive():
    with pytest.raises(ValueError):
        ToolInvocation(
            call_id="c-4",
            tool_name="asset_search",
            result=[],
            error="timeout",
        )
