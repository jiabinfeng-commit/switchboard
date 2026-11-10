"""流程定义：节点、边、加载校验。

定义是数据而不是代码。理由见 ADR 0002 决策 1：流程要能按版本回放，
要能不改代码就新增一条流程；if/else 写的分支没法被当成数据重放。

加载期的校验比运行期严格得多，因为「漏一条边」这种错误在运行时表现为
实例卡死或静默走错分支，而单测未必刚好走到那条路径上。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping

from switchboard.agent.result import StepAction


class DefinitionError(ValueError):
    """流程定义不合法。在加载期抛出，不带进运行时。"""


class NodeType(StrEnum):
    # 可执行节点：都返回 StepResult，由执行器决定用不用模型
    AGENT = "agent"
    TOOL = "tool"
    TEMPLATE = "template"

    # 挂起节点：进入后实例不再占用进程，等外部唤醒
    WAIT_USER = "wait_user"
    WAIT_TIMER = "wait_timer"
    WAIT_HUMAN = "wait_human"
    WAIT_EVENT = "wait_event"

    END = "end"


STEP_TYPES = frozenset({NodeType.AGENT, NodeType.TOOL, NodeType.TEMPLATE})
WAIT_TYPES = frozenset(
    {
        NodeType.WAIT_USER,
        NodeType.WAIT_TIMER,
        NodeType.WAIT_HUMAN,
        NodeType.WAIT_EVENT,
    }
)

# 可执行节点必须为这些结果声明去向。
# retry_exhausted 不是步骤返回的动作，是引擎在重试次数用尽时自己产生的：
# 它表达「重试这条路已经走不通了」，必须有地方去，否则实例就卡在原地。
RETRY_EXHAUSTED = "retry_exhausted"
STEP_OUTCOMES = frozenset(action.value for action in StepAction) | {RETRY_EXHAUSTED}

TIMEOUT_OUTCOME = "timeout"
TIMER_FIRED_OUTCOME = "fired"

# 匹配「正整数 + 一个单位」，单位只能是 s/m/h/d，且必须占满整串：
#   24h 匹配；1.5h、24、24H、1w 都不匹配。
# 第 1 组是数字，第 2 组是单位，由 parse_duration 换算成秒。
_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> timedelta:
    """把 24h / 30d / 15m / 30s 解析成 timedelta。

    不接受浮点和其他单位：流程里的超时值是配置，格式越窄越不容易写错。
    """
    match = _DURATION_RE.match(value)
    if match is None:
        raise DefinitionError(f"时长格式不合法：{value!r}，应形如 24h / 30d / 15m")
    amount, unit = match.groups()
    return timedelta(seconds=int(amount) * _UNIT_SECONDS[unit])


@dataclass(frozen=True)
class Node:
    name: str
    type: NodeType
    # 结果 -> 下一节点。可执行节点是动作/retry_exhausted，
    # 挂起节点是唤醒类型或 timeout。
    next: Mapping[str, str] = field(default_factory=dict)
    max_attempts: int = 1
    timeout: timedelta | None = None
    # 可执行节点各自指向的东西：agent 用哪个步骤，tool 调哪个后端方法，
    # template 用哪套话术。引擎不解释这些名字，交给执行器。
    step: str | None = None
    tool: str | None = None
    template: str | None = None
    event: str | None = None

    @property
    def is_step(self) -> bool:
        """是否是可执行节点（agent / tool / template）。

        可执行节点必须返回 StepResult，加载器会要求它的 next 覆盖全部动作；
        等待节点不返回结果，由外部事件唤醒。
        """
        return self.type in STEP_TYPES

    @property
    def is_wait(self) -> bool:
        """是否是挂起节点（等用户 / 定时器 / 人工任务 / 外部事件）。

        挂起节点必须有 timeout 和对应的退出边，否则实例会永远留在表里。
        """
        return self.type in WAIT_TYPES


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: int
    start: str
    nodes: Mapping[str, Node]

    def node(self, name: str) -> Node:
        """按名字取节点；不存在时抛 DefinitionError。

        加载期已经保证每条边都指向存在的节点，所以运行期取不到只可能是
        调用方传错了名字。抛错而不是返回 None，让问题在这一行暴露，
        而不是在下一次属性访问时变成一个更难读的异常。
        """
        try:
            return self.nodes[name]
        except KeyError:
            raise DefinitionError(f"节点不存在：{name!r}") from None


# 每种节点允许出现的字段。写错字段名（比如把 timeout 拼成 time_out）
# 如果不拦，就会静默用默认值，等于超时兜底消失。
_ALLOWED_KEYS: dict[NodeType, set[str]] = {
    NodeType.AGENT: {"type", "next", "max_attempts", "step"},
    NodeType.TOOL: {"type", "next", "max_attempts", "tool"},
    NodeType.TEMPLATE: {"type", "next", "max_attempts", "template"},
    NodeType.WAIT_USER: {"type", "next", "timeout"},
    NodeType.WAIT_TIMER: {"type", "next", "timeout"},
    NodeType.WAIT_HUMAN: {"type", "next", "timeout"},
    NodeType.WAIT_EVENT: {"type", "next", "timeout", "event"},
    # next 列进来是为了能给出「结束节点不能有 next」这条明确错误，
    # 而不是被通用的「不认识的字段」拦下。
    NodeType.END: {"type", "next"},
}

_STEP_TARGET_FIELD = {
    NodeType.AGENT: "step",
    NodeType.TOOL: "tool",
    NodeType.TEMPLATE: "template",
}


def load_definition(data: Mapping[str, Any]) -> WorkflowDefinition:
    """校验并加载流程定义。任何一条不满足都直接抛 DefinitionError。"""
    if not isinstance(data, Mapping):
        raise DefinitionError("流程定义必须是映射")

    unknown = set(data) - {"name", "version", "start", "nodes"}
    if unknown:
        raise DefinitionError(f"流程定义里有不认识的字段：{sorted(unknown)}")

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise DefinitionError("流程定义缺少 name")

    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise DefinitionError("version 必须是 >= 1 的整数")

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, Mapping) or not raw_nodes:
        raise DefinitionError("nodes 必须是非空映射")

    nodes: dict[str, Node] = {}
    for node_name, raw in raw_nodes.items():
        if not isinstance(node_name, str) or not node_name:
            raise DefinitionError(f"节点名必须是非空字符串：{node_name!r}")
        if not isinstance(raw, Mapping):
            raise DefinitionError(f"节点 {node_name} 的定义必须是映射")
        nodes[node_name] = _load_node(node_name, raw)

    start = data.get("start")
    if not isinstance(start, str) or start not in nodes:
        raise DefinitionError(f"start 指向不存在的节点：{start!r}")

    _validate_edges(nodes)
    _validate_reachable(nodes, start)

    return WorkflowDefinition(
        name=name,
        version=version,
        start=start,
        nodes=MappingProxyType(nodes),
    )


def _load_node(name: str, raw: Mapping[str, Any]) -> Node:
    """把一个节点的原始映射转成 Node，并完成该类型专属的校验。

    校验顺序是：类型 -> 未知字段 -> next 结构 -> 类型专属字段。
    先查未知字段，是为了让 time_out 这类拼写错误得到明确报错，
    而不是被静默忽略、用默认值让「超时兜底」悄悄消失。
    """
    try:
        node_type = NodeType(raw.get("type"))
    except ValueError:
        raise DefinitionError(f"节点 {name} 的 type 不认识：{raw.get('type')!r}") from None

    unknown = set(raw) - _ALLOWED_KEYS[node_type]
    if unknown:
        raise DefinitionError(f"节点 {name}（{node_type}）有不认识的字段：{sorted(unknown)}")

    next_map = _load_next(name, raw.get("next"))
    if node_type is NodeType.END:
        if next_map:
            raise DefinitionError(f"结束节点 {name} 不能有 next")
        return Node(name=name, type=node_type)

    if node_type in STEP_TYPES:
        _require_step_coverage(name, next_map)
        target_field = _STEP_TARGET_FIELD[node_type]
        target = raw.get(target_field)
        if not isinstance(target, str) or not target:
            raise DefinitionError(f"节点 {name} 缺少 {target_field}")
        max_attempts = raw.get("max_attempts", 1)
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise DefinitionError(f"节点 {name} 的 max_attempts 必须是 >= 1 的整数")
        return Node(
            name=name,
            type=node_type,
            next=next_map,
            max_attempts=max_attempts,
            **{target_field: target},
        )

    # 挂起节点
    timeout = raw.get("timeout")
    if not isinstance(timeout, str):
        raise DefinitionError(f"等待节点 {name} 必须配置 timeout")
    _require_wait_coverage(name, node_type, next_map)
    event = raw.get("event")
    if node_type is NodeType.WAIT_EVENT and (not isinstance(event, str) or not event):
        raise DefinitionError(f"节点 {name} 缺少 event")
    return Node(
        name=name,
        type=node_type,
        next=next_map,
        timeout=parse_duration(timeout),
        event=event if isinstance(event, str) else None,
    )


def _load_next(name: str, raw: Any) -> Mapping[str, str]:
    """把 next 规整成只读的「结果 -> 目标节点」映射。

    None 视为空映射（end 节点可以不写 next）；其他非映射一律报错。
    包成 MappingProxyType，是为了让加载后的定义不能被运行时改写——
    否则一条正在跑的流程可能在半路被人改掉边。
    """
    if raw is None:
        return MappingProxyType({})
    if not isinstance(raw, Mapping):
        raise DefinitionError(f"节点 {name} 的 next 必须是映射")
    result: dict[str, str] = {}
    for outcome, target in raw.items():
        if not isinstance(outcome, str) or not isinstance(target, str):
            raise DefinitionError(f"节点 {name} 的 next 必须是 字符串->字符串")
        result[outcome] = target
    return MappingProxyType(result)


def _require_step_coverage(name: str, next_map: Mapping[str, str]) -> None:
    """检查可执行节点是否把六个结果都声明了去向。

    五个 StepAction 加上引擎产生的 retry_exhausted。少一个就意味着某种
    运行结果没有下一跳；这种情况在运行期只能抛异常或让实例卡死，
    不如在加载期直接拒绝，让定义作者一次改对。
    """
    missing = STEP_OUTCOMES - set(next_map)
    if missing:
        raise DefinitionError(f"节点 {name} 缺少这些结果的去向：{sorted(missing)}")
    extra = set(next_map) - STEP_OUTCOMES
    if extra:
        raise DefinitionError(f"节点 {name} 声明了不认识的结果：{sorted(extra)}")


def _require_wait_coverage(name: str, node_type: NodeType, next_map: Mapping[str, str]) -> None:
    """检查等待节点的退出方式是否完整。

    规则：
    - 定时节点的唯一正常出口是 fired；
    - 其余等待节点必须有 timeout 出口（没有超时的挂起会泄漏实例），
      并且不能只有 timeout（那样正常路径永远走不到，流程只剩兜底）。

    这两种错误在运行时都不会立刻报错，只会在很久之后表现为实例卡住，
    所以必须在加载时拦住。
    """
    outcomes = set(next_map)
    # 定时节点的到期就是它唯一的正常出口，结果叫 fired，不叫 timeout。
    if node_type is NodeType.WAIT_TIMER:
        if outcomes != {TIMER_FIRED_OUTCOME}:
            raise DefinitionError(f"定时节点 {name} 的 next 只能是 {TIMER_FIRED_OUTCOME}")
        return
    if TIMEOUT_OUTCOME not in outcomes:
        raise DefinitionError(f"等待节点 {name} 必须声明 timeout 的去向")
    if outcomes == {TIMEOUT_OUTCOME}:
        raise DefinitionError(f"等待节点 {name} 只有 timeout 一条边，永远走不到正常路径")


def _validate_edges(nodes: Mapping[str, Node]) -> None:
    """检查每条边指向的节点都存在。

    悬空的边如果在运行期才发现，那时实例可能已经挂了几个小时；
    在加载期拦下，错误信息还能直接指出是哪个节点的哪个结果。
    """
    for node in nodes.values():
        for outcome, target in node.next.items():
            if target not in nodes:
                raise DefinitionError(f"节点 {node.name} 的 {outcome} 指向不存在的节点 {target!r}")


def _validate_reachable(nodes: Mapping[str, Node], start: str) -> None:
    """从 start 做广度优先遍历，拒绝走不到的节点。

    走不到的节点要么是边写错了，要么是删改后留下的残留。两者都应尽早暴露：
    留在定义里会让人误以为流程有那条分支，而实际上永远不会经过。
    """
    seen = {start}
    queue = [start]
    while queue:
        node = nodes[queue.pop()]
        for target in node.next.values():
            if target not in seen:
                seen.add(target)
                queue.append(target)
    unreachable = sorted(set(nodes) - seen)
    if unreachable:
        raise DefinitionError(f"这些节点从 start 走不到：{unreachable}")
