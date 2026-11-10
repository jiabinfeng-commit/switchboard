"""流程推进：输入实例和结果，输出下一个实例。

这个模块不碰数据库、不调模型、不读系统时间——时间由调用方传入。
好处是同一段推进逻辑可以在测试里用固定时间重放，也能被事件日志回放，
排查「为什么走到这一步」时不需要复现当时的环境。

推进分两步，中间必须落库：

    next_action()  ->  RunStep / Suspend / Finish
    （执行可执行节点）
    apply_result() ->  新实例

挂起后的恢复走 apply_outcome()。两者最后都汇到 _move()，
所以「动作 -> 结果 -> 下一节点」的规则只有一份。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.definition import (
    RETRY_EXHAUSTED,
    TIMEOUT_OUTCOME,
    TIMER_FIRED_OUTCOME,
    Node,
    NodeType,
    WorkflowDefinition,
)


class WorkflowError(RuntimeError):
    """推进过程中遇到无法继续的情况。"""


class IllegalState(WorkflowError):
    """实例当前状态不允许这个操作（例如对未挂起的实例调用 resume）。"""


class InvalidOutcome(WorkflowError):
    """传进来的结果是流程定义里没声明的。不能猜，只能拒绝。"""


class ContextConflict(WorkflowError):
    """两个节点往上下文里写同一个 key。覆盖会让后一步悄悄吃掉前一步的数据。"""


INITIAL_OWNER = "<start>"


class InstanceStatus(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"


class WaitReason(StrEnum):
    USER = "user"
    TIMER = "timer"
    HUMAN = "human"
    EVENT = "event"


_WAIT_REASON = {
    NodeType.WAIT_USER: WaitReason.USER,
    NodeType.WAIT_TIMER: WaitReason.TIMER,
    NodeType.WAIT_HUMAN: WaitReason.HUMAN,
    NodeType.WAIT_EVENT: WaitReason.EVENT,
}


@dataclass(frozen=True)
class Instance:
    """一个流程实例。

    frozen=True 不等于「不可变」：状态迁移靠 replace 产生新值。
    这样做是为了让「推进前」和「推进后」两个状态都能被记录下来，
    崩溃恢复和事件回放才有依据。
    """

    id: str
    workflow_name: str
    workflow_version: int
    status: InstanceStatus
    current_node: str
    attempt: int = 1
    suspend_reason: WaitReason | None = None
    deadline: datetime | None = None
    # default_factory 而不是 {}：可变默认值会在实例之间共享，
    # 一个实例往里写数据，另一个实例也会看到。
    context: Mapping[str, Any] = field(default_factory=dict)
    # 每个 key 最后由哪个节点写入。用来区分两种同名写入：
    # 同一个节点重跑（如用户要求修改后重新生成草稿）允许覆盖，
    # 不同节点写同一个 key（如后一步把前一步的证据字段悄悄改掉）必须报错。
    context_owners: Mapping[str, str] = field(default_factory=dict)
    row_version: int = 0


@dataclass(frozen=True)
class RunStep:
    """需要调用执行器跑一个可执行节点。"""

    node: Node
    attempt: int


@dataclass(frozen=True)
class Suspend:
    """需要把实例挂起，等外部唤醒。"""

    node: Node
    reason: WaitReason
    deadline: datetime


@dataclass(frozen=True)
class Finish:
    node: Node
    status: InstanceStatus


Plan = RunStep | Suspend | Finish


def start_instance(
    definition: WorkflowDefinition,
    instance_id: str,
    *,
    context: Mapping[str, Any] | None = None,
) -> Instance:
    """创建一个停在 start 节点、尚未推进的实例。

    context 是调用方给的初始数据（会话 id、已确认字段等），会被标记为
    <start> 写入，任何流程节点都不能覆盖它。
    这个函数不落库，也不执行任何节点；落库和推进由 runner 负责。
    """
    initial = dict(context or {})
    return Instance(
        id=instance_id,
        workflow_name=definition.name,
        workflow_version=definition.version,
        status=InstanceStatus.RUNNING,
        current_node=definition.start,
        context=initial,
        # 初始上下文是调用方的输入，不属于任何流程节点。
        # 标记成 <start> 之后，任何节点想覆盖它都会触发冲突。
        context_owners={key: INITIAL_OWNER for key in initial},
    )


def next_action(
    definition: WorkflowDefinition,
    instance: Instance,
    now: datetime,
) -> Plan:
    """当前节点接下来要做什么。不修改实例。"""
    if instance.status is not InstanceStatus.RUNNING:
        raise IllegalState(
            f"实例 {instance.id} 当前是 {instance.status}，不能继续推进"
        )

    node = definition.node(instance.current_node)

    if node.type is NodeType.END:
        return Finish(node=node, status=InstanceStatus.COMPLETED)

    if node.is_step:
        return RunStep(node=node, attempt=instance.attempt)

    if node.timeout is None:
        # 加载器已经拦过这种情况，这里只是防御：没有超时的挂起会泄漏实例。
        raise IllegalState(f"等待节点 {node.name} 没有超时")
    return Suspend(
        node=node,
        reason=_WAIT_REASON[node.type],
        deadline=now + node.timeout,
    )


def apply_result(
    definition: WorkflowDefinition,
    instance: Instance,
    result: StepResult,
    *,
    context_update: Mapping[str, Any] | None = None,
) -> Instance:
    """把一个可执行节点的结果落成状态迁移。"""
    node = definition.node(instance.current_node)
    if not node.is_step:
        raise IllegalState(f"节点 {node.name} 不可执行，不能接收步骤结果")

    outcome = result.action.value
    # 重试次数由节点上的 max_attempts 决定，而不是步骤自己决定重试几次。
    # 用尽后 outcome 变成 retry_exhausted，由流程定义决定转人工还是别的去处。
    if result.action is StepAction.RETRYABLE and instance.attempt >= node.max_attempts:
        outcome = RETRY_EXHAUSTED

    return _move(definition, instance, node, outcome, context_update)


def apply_outcome(
    definition: WorkflowDefinition,
    instance: Instance,
    outcome: str,
    *,
    context_update: Mapping[str, Any] | None = None,
) -> Instance:
    """把一个外部唤醒结果落成状态迁移（用户回复、人工处置、事件、超时）。"""
    node = definition.node(instance.current_node)
    if not node.is_wait:
        raise IllegalState(f"节点 {node.name} 不是等待节点，不能按外部结果推进")
    return _move(definition, instance, node, outcome, context_update)


def timeout_outcome(node: Node) -> str:
    """这个等待节点到期时应该用哪个结果唤醒。

    定时节点本身就是「时间到了」，结果叫 fired；其余等待节点到期叫 timeout。
    两者不能混用，否则流程定义里两个分支会互相覆盖。
    """
    return TIMER_FIRED_OUTCOME if node.type is NodeType.WAIT_TIMER else TIMEOUT_OUTCOME


def _move(
    definition: WorkflowDefinition,
    instance: Instance,
    node: Node,
    outcome: str,
    context_update: Mapping[str, Any] | None,
) -> Instance:
    """所有状态迁移的唯一出口。

    负责查边、计算重试次数、合并上下文、清掉挂起信息，并返回新实例。
    可执行节点和等待节点都走这里，保证两边的迁移规则不会各自漂移。
    outcome 没有声明时抛 InvalidOutcome，绝不猜一个「最可能」的去处。
    """
    target_name = node.next.get(outcome)
    if target_name is None:
        raise InvalidOutcome(
            f"节点 {node.name} 没有声明结果 {outcome!r} 的去向"
            f"（已声明：{sorted(node.next)}）"
        )

    # 回到自己身上算「同一次尝试的重试」，次数加一；
    # 走到别的节点就是新的一步，次数重置。
    attempt = instance.attempt + 1 if target_name == node.name else 1

    context, owners = _merge_context(
        instance.context,
        instance.context_owners,
        context_update,
        writer=node.name,
    )

    return replace(
        instance,
        current_node=target_name,
        attempt=attempt,
        status=InstanceStatus.RUNNING,
        suspend_reason=None,
        deadline=None,
        context=context,
        context_owners=owners,
    )


def _merge_context(
    base: Mapping[str, Any],
    owners: Mapping[str, str],
    update: Mapping[str, Any] | None,
    *,
    writer: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """合并节点产出的数据，并维护每个 key 的写入者。

    规则：
    - 同一个节点重跑可以覆盖自己写过的 key（回环重跑是正常行为）；
    - 不同节点写同一个 key 直接报错（防止后一步悄悄改掉前一步的证据）；
    - 初始数据属于 <start>，任何节点都不能覆盖。

    返回新的 context 和 owners，二者必须一起更新；只更新 context 会让归属
    信息过期，之后的冲突判断就失去依据。
    """
    if not update:
        return dict(base), dict(owners)
    conflict = {
        key for key in update if key in owners and owners[key] != writer
    }
    if conflict:
        raise ContextConflict(
            f"节点 {writer} 想覆盖这些 key，但它们属于别的节点：{sorted(conflict)}"
        )
    merged = {**base, **update}
    merged_owners = {**owners, **{key: writer for key in update}}
    return merged, merged_owners
