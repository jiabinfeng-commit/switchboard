"""工具信封：记录一次工具调用的完整事实。

模型没有执行权，它只能提出「我想调某个工具」的请求；真正执行的是代码。
执行完成后由代码写下这条记录，作为校验和审计的唯一依据。

信任边界：
    tool_name / arguments  由模型提出，代码校验后才允许执行
    call_id / result / error  只能由代码写
如果让模型参与填写后三者，校验就失去意义——模型可以伪造自己想要的证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用的记录。"""

    # 代码生成的唯一标识。它同时承担三件事：
    #   1. 证明这次调用存在（校验器不接受没有记录的「证据」）
    #   2. 串联日志和事件，用于回放
    #   3. 将来作为幂等核对的依据
    call_id: str

    # 调用的工具名。由模型提出，执行前必须对照白名单。
    # 模型能提出任意字符串，白名单检查是唯一防线。
    tool_name: str

    # 调用参数。由模型提出，执行前必须校验 schema。
    # 另外要拒绝携带身份字段（user_id 之类）：身份由代码注入，
    # 不能由模型提供，否则模型可以替别人查数据。
    arguments: dict[str, Any] = field(default_factory=dict)

    # 工具原始返回值，原样保留，不做裁剪。
    # 校验器要判断「模型给的值是不是来自这里」，裁剪会让合法引用变成「编造」。
    result: Any = None

    # 失败时的结构化错误。与 result 互斥。
    # error 为 None 表示调用成功；此时 result 仍可能是 None（工具返回空）。
    error: str | None = None

    def __post_init__(self) -> None:
        """构造时拒绝「既有结果又有错误」的矛盾状态。

        成功时 result 可以为 None（工具确实返回空），失败时 result 必须为 None。
        两种情况互斥；一旦同时出现，说明执行层写错了，越早抛错越容易定位到
        写入这条记录的那一行。
        """
        # 理论上成功时也可能没有返回值，但「既有结果又有错误」绝不可能是正常状态。
        # 这种矛盾一旦出现，说明执行层写错了，越早抛错越好。
        if self.error is not None and self.result is not None:
            raise ValueError("一次调用不能同时有 result 和 error")

    @property
    def ok(self) -> bool:
        """调用是否成功。只看 error，不看 result 是否为空。"""
        return self.error is None
