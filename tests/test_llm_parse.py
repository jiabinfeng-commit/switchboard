import pytest

from switchboard.agent.llm import parse_model_output
from switchboard.agent.questions import AskKind, AskUser
from switchboard.agent.runtime import Draft, ModelError, ToolCallRequest


def test_parse_tool_call():
    output = parse_model_output(
        '{"action": "tool_call", "tool": "asset_search", "arguments": {"q": "SN1"}}'
    )
    assert output == ToolCallRequest("asset_search", {"q": "SN1"})


def test_parse_draft_with_provenance():
    output = parse_model_output(
        '{"action": "draft", "fields": {'
        '"asset_id": {"value": 12345, "call_id": "c-1", "path": "[0].id"}}}'
    )
    assert isinstance(output, Draft)
    assert output.fields["asset_id"].value == 12345
    assert output.fields["asset_id"].call_id == "c-1"


def test_parse_ask_user_missing_field():
    output = parse_model_output(
        '{"action": "ask_user", "kind": "missing_field", '
        '"field": "asset_serial", "label": "设备序列号"}'
    )
    assert output == AskUser(
        kind=AskKind.MISSING_FIELD, field="asset_serial", label="设备序列号"
    )


def test_parse_ask_user_no_result():
    output = parse_model_output(
        '{"action": "ask_user", "kind": "no_result", "call_id": "c-1"}'
    )
    assert output == AskUser(kind=AskKind.NO_RESULT, call_ids=("c-1",))


def test_parse_ask_user_ambiguous():
    output = parse_model_output(
        '{"action": "ask_user", "kind": "ambiguous_asset", "candidates": ['
        '{"value": 12345, "call_id": "c-1", "path": "[0].id"},'
        '{"value": 12346, "call_id": "c-1", "path": "[1].id"}]}'
    )
    assert output.kind is AskKind.AMBIGUOUS_ASSET
    assert [ref.value for ref in output.refs] == [12345, 12346]


def test_markdown_fence_is_tolerated():
    output = parse_model_output(
        '```json\n{"action": "ask_user", "kind": "no_result", "call_id": "c-1"}\n```'
    )
    assert isinstance(output, AskUser)


def test_free_text_question_is_no_longer_accepted():
    # 旧协议的自由文本追问必须被拒绝，否则事实校验会从这里漏出去
    with pytest.raises(ModelError):
        parse_model_output('{"action": "ask_user", "question": "没查到，请核对"}')


@pytest.mark.parametrize(
    "text",
    [
        "不是 json",
        "[]",
        '{"action": "unknown"}',
        '{"action": "tool_call"}',
        '{"action": "tool_call", "tool": "t", "arguments": []}',
        '{"action": "draft", "fields": {}}',
        # 字段缺少出处：这是最关键的一条，模型只给裸值必须被拒绝
        '{"action": "draft", "fields": {"asset_id": 12345}}',
        '{"action": "draft", "fields": {"asset_id": {"value": 1, "call_id": "c-1"}}}',
        '{"action": "ask_user", "kind": "missing_field", "field": "x"}',
        '{"action": "ask_user", "kind": "no_result"}',
        '{"action": "ask_user", "kind": "ambiguous_asset", "candidates": []}',
    ],
)
def test_bad_outputs_raise_model_error(text):
    with pytest.raises(ModelError):
        parse_model_output(text)
