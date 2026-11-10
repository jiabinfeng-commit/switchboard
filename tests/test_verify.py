import pytest

from switchboard.agent.tool import ToolInvocation
from switchboard.agent.verify import (
    PathError,
    Rejection,
    ValueRef,
    resolve_path,
    verify_ref,
)

ASSETS = [
    {"id": 12345, "model": "ThinkPad T14", "owner": {"name": "张三"}},
    {"id": 12346, "model": "ThinkPad T14", "owner": {"name": "李四"}},
]

INVOCATIONS = {
    "c-1": ToolInvocation(call_id="c-1", tool_name="asset_search", result=ASSETS),
    "c-2": ToolInvocation(call_id="c-2", tool_name="asset_search", error="timeout"),
}


def test_resolve_simple_index_and_field():
    assert resolve_path(ASSETS, "[0].id") == 12345
    assert resolve_path(ASSETS, "[1].owner.name") == "李四"


@pytest.mark.parametrize(
    "path",
    [
        "[9].id",          # 下标越界
        "[0].missing",     # 字段不存在
        "[0].id.nope",     # 在标量上继续取字段
        "",                # 空路径
        "id",              # 对列表直接取字段
        "[0].id; drop",    # 非法语法
    ],
)
def test_bad_paths_raise(path):
    with pytest.raises(PathError):
        resolve_path(ASSETS, path)


def test_verified_when_value_really_comes_from_result():
    result = verify_ref(ValueRef(12345, "c-1", "[0].id"), INVOCATIONS)
    assert result.ok


def test_unknown_call_is_rejected():
    result = verify_ref(ValueRef(12345, "c-9", "[0].id"), INVOCATIONS)
    assert result.rejection is Rejection.UNKNOWN_CALL


def test_failed_call_is_rejected():
    result = verify_ref(ValueRef(1, "c-2", "[0].id"), INVOCATIONS)
    assert result.rejection is Rejection.CALL_FAILED


def test_missing_path_is_rejected():
    result = verify_ref(ValueRef(12345, "c-1", "[0].nope"), INVOCATIONS)
    assert result.rejection is Rejection.PATH_NOT_FOUND


def test_fabricated_value_is_rejected():
    # 值确实存在于结果的某处，但模型声称的路径上不是这个值
    result = verify_ref(ValueRef(12346, "c-1", "[0].id"), INVOCATIONS)
    assert result.rejection is Rejection.VALUE_MISMATCH


def test_bool_is_not_int():
    # 这是最容易漏的坑：True == 1 在 Python 里成立
    invocations = {
        "c-3": ToolInvocation(call_id="c-3", tool_name="t", result=[{"active": True}])
    }
    result = verify_ref(ValueRef(1, "c-3", "[0].active"), invocations)
    assert result.rejection is Rejection.VALUE_MISMATCH
