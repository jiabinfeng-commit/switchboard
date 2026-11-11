"""一步执行的输入。

关键在于：把「模型能看到的」和「代码自己用的」拆成两个类型，
而不是混在一个 dataclass 里靠人记得挑。

prompt 只能由 ModelView 构造。这样「身份字段不进模型」就不是一条纪律，
而是类型上的约束——构造 prompt 的代码手里根本没有 user_id。
漏传是 bug，可以测；靠人记得不传，迟早会漏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class ToolSideEffect(StrEnum):
    """工具是否有副作用。agent 内部只允许 READ。"""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolSpec:
    """暴露给模型的一个工具。它同时是白名单：不在列表里的工具名一律拒绝执行。"""

    name: str
    description: str

    # 参数 schema（JSON Schema）。
    # 自然语言描述告诉模型「该传什么」，schema 让代码能校验「传得对不对」，
    # 两者不能互相替代。实测：只给描述时模型会自己发明参数名
    # （传 serial，执行器读的是 q），结果返回空列表，看起来像「查无记录」。
    parameters: Mapping[str, Any]

    # 故意不给默认值：新增工具时必须显式想一次「它写不写库」。
    # 默认成 READ 的话，以后加写工具忘了改，崩溃重跑就会重复建单，
    # 而没有任何一处代码提醒过。
    # 注意这是装配方的声明，运行时无法验证执行器真的只读；
    # 真正的保证是写操作不进 agent 的工具表，而是外层 tool 节点（ADR 0004）。
    side_effect: ToolSideEffect


@dataclass(frozen=True)
class ModelView:
    """模型能看到的全部内容。缺一样它就会填错或问错，多一样就扩大注入面。"""

    # 员工原话。这是唯一由外部输入的内容，也是 prompt 注入的入口。
    # 它的边界由 prompt 结构区分（数据区和指令区），不能靠内容过滤。
    user_message: str

    # 上一步已经确认过的字段。不给模型，它会重复追问已经确定的事。
    confirmed_fields: Mapping[str, Any] = field(default_factory=dict)

    # 可用工具及说明。同时充当白名单，执行前必须对照。
    tools: tuple[ToolSpec, ...] = ()

    # 模型要填的输出 schema。
    # TODO: 结构待定，先放 Mapping。字段里哪些要求带出处（ValueRef），
    # 也需要在这里表达出来。
    output_schema: Mapping[str, Any] = field(default_factory=dict)

    # 当前时间。模型用来解析「昨天」「上周五」这类相对时间。
    # 同时必须写入事件日志：回放时要用当时的时间，不能用回放时的时间，
    # 否则同一份输入会得到不同结果。
    now: datetime = field(default_factory=lambda: datetime.now().astimezone())


@dataclass(frozen=True)
class StepInput:
    """一步执行的完整输入。"""

    # 唯一允许进入 prompt 的部分。
    view: ModelView

    # 以下仅代码侧可见。
    # 身份由代码注入、不由模型提供，否则模型可以替别人查数据或建单。
    user_id: str

    # 该用户有权操作的实体范围。检索和数据过滤必须先用它裁剪，
    # 不能先查出来再过滤——那样越权数据已经进过内存和日志。
    allowed_entities: tuple[int, ...] = ()
