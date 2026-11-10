"""证据校验：判断模型给的值是「从工具结果里选的」还是「自己编的」。

做法是让模型声明出处，再由代码按出处取值比对（方案 C）：
    模型输出 {"value": 12345, "call_id": "c-1", "path": "result[0].id"}
    代码按 call_id 找到那次调用，按 path 取值，和 value 严格比对。

模型可以说错出处，但改不了内容——路径取出来不等于 value 就直接失败。
所以模型只能「选」，不能「造」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from switchboard.agent.tool import ToolInvocation


class Rejection(StrEnum):
    """校验不通过的原因。只用于记录，不参与流程分支。"""

    UNKNOWN_CALL = "unknown_call"      # 引用了不存在的调用
    CALL_FAILED = "call_failed"        # 引用了失败的工具调用，没有返回值
    PATH_NOT_FOUND = "path_not_found"  # 路径在返回值里取不到
    VALUE_MISMATCH = "value_mismatch"  # 取到了，但值不相等


@dataclass(frozen=True)
class ValueRef:
    """模型声明的一个值及其出处。"""

    value: Any
    call_id: str
    path: str


@dataclass(frozen=True)
class Verified:
    """校验结果。ok 为 True 时表示该值可信。"""

    ok: bool
    rejection: Rejection | None = None
    detail: str | None = None


# 路径语法故意做得很小：只有「字段名」和「列表下标」两种元素，
# 例如 result[0].owner.name。不做表达式求值，模型无法构造任意访问逻辑。
#
# 每次匹配一个路径元素，两种形式二选一：
#   \.?([A-Za-z_][A-Za-z0-9_]*)  字段名，前面的点是可选的分隔符，
#                                 第 1 组是字段名本身（result / owner / name）。
#   \[(\d+)\]                    列表下标，只接受十进制数字，第 2 组是下标。
# 点跟着字段名一起吃掉，是为了让 finditer 的 token.start() 能判断
# 「有没有跳过的字符」——跳过就说明路径里混进了不认识的语法。
_TOKEN = re.compile(r"\.?([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


class PathError(ValueError):
    pass


def resolve_path(value: Any, path: str) -> Any:
    """按 path 从 value 中取值，取不到抛 PathError。"""
    pos = 0
    for token in _TOKEN.finditer(path):
        if token.start() != pos:
            raise PathError(f"路径语法错误：{path}")
        pos = token.end()

        name, index = token.groups()
        if name is not None:
            if not isinstance(value, Mapping) or name not in value:
                raise PathError(f"字段不存在：{name}")
            value = value[name]
        else:
            i = int(index)
            if not isinstance(value, list) or i >= len(value):
                raise PathError(f"下标越界：{i}")
            value = value[i]

    if pos != len(path) or pos == 0:
        raise PathError(f"路径语法错误：{path}")
    return value


def _strict_equal(a: Any, b: Any) -> bool:
    """比对值。bool 必须单独处理，否则 True == 1 会误判通过。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    # JSON 只有一种数字类型，Python 区分 int 和 float，这里按数值比较
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def verify_ref(ref: ValueRef, invocations: Mapping[str, ToolInvocation]) -> Verified:
    """校验一个 ValueRef 是否真的来自指定的工具调用。"""
    invocation = invocations.get(ref.call_id)
    if invocation is None:
        return Verified(False, Rejection.UNKNOWN_CALL, f"没有 call_id={ref.call_id} 的记录")

    if not invocation.ok:
        return Verified(
            False,
            Rejection.CALL_FAILED,
            f"call_id={ref.call_id} 的调用失败：{invocation.error}",
        )

    try:
        actual = resolve_path(invocation.result, ref.path)
    except PathError as exc:
        return Verified(False, Rejection.PATH_NOT_FOUND, str(exc))

    if not _strict_equal(actual, ref.value):
        return Verified(
            False,
            Rejection.VALUE_MISMATCH,
            f"{ref.path} 实际为 {actual!r}，模型声称 {ref.value!r}",
        )

    return Verified(True)
