"""把纯推进函数和存储、执行器接起来。

引擎不知道「agent 步骤怎么跑」「工具怎么调」，Runner 也不知道，
它只负责：按计划驱动、每次迁移都落库、记录每次执行尝试、挂起时做副作用。

落库时机很关键：可执行节点跑完立刻保存，而不是等整轮推进结束。
否则步骤的副作用已经发生、结果却没落库，崩溃后重跑会重复执行。
2b 加了执行记录和幂等键之后，重跑至少是可识别、可安全重发的；
真正的安全性仍取决于下游是否按幂等键去重。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import uuid4

from switchboard.agent.result import StepResult
from switchboard.workflow.definition import (
    TIMEOUT_OUTCOME,
    Node,
    WorkflowDefinition,
)
from switchboard.workflow.engine import (
    Finish,
    IllegalState,
    Instance,
    InstanceStatus,
    RunStep,
    Suspend,
    WaitReason,
    apply_outcome,
    apply_result,
    next_action,
    start_instance,
    timeout_outcome,
)
from switchboard.workflow.execution import (
    StepExecution,
    StepExecutionStatus,
    StepExecutionStore,
)
from switchboard.workflow.human import HumanTask, HumanTaskStatus, HumanTaskStore
from switchboard.workflow.store import InstanceStore


class WorkflowLoop(RuntimeError):
    """一轮推进里执行了太多次可执行节点。

    正常流程要么走到某个等待节点，要么走到结束。出现这种情况说明定义里
    有一串互相跳转、中间没有挂起的节点，或者出现了一个永远成功又回到自己的环。
    宁可报错，也不让请求线程转下去。
    """


@dataclass(frozen=True)
class Execution:
    """执行器的返回值。结果和「往上下文里写什么」分开：

    引擎只认识 StepResult，不知道 payload 的业务形状；
    哪些字段该进上下文由执行器决定。
    """

    result: StepResult
    context_update: Mapping[str, Any] | None = None


# 执行器拿到幂等键。它必须原样传给下游，不能自己再生成一个：
# 崩溃重跑时 runner 会复用同一条执行记录的键，执行器换键就等于换了请求。
Executor = Callable[[Node, Instance, str], Execution]
# 挂起时做副作用：发确认提示、订阅事件。
# 副作用失败不应该丢掉挂起状态，所以它在落库之后执行。
# 人工任务不在这里创建——它是流程数据而不是外部副作用，runner 自己负责。
SuspendHook = Callable[[Node, Instance], None]


def _utcnow() -> datetime:
    """默认时钟，返回带时区的 UTC 时间。

    不用本地时间：截止时间会落库，服务器时区一变，同一个 deadline
    就会被解释成另一个时刻。测试通过注入 clock 替代它。
    """
    return datetime.now(timezone.utc)


class WorkflowRunner:
    def __init__(
        self,
        definition: WorkflowDefinition,
        store: InstanceStore,
        execute: Executor,
        executions: StepExecutionStore,
        *,
        human_tasks: HumanTaskStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
        on_suspend: SuspendHook | None = None,
        max_steps: int = 100,
    ) -> None:
        """装配推进循环。

        execute 是唯一执行可执行节点的地方（跑 agent、调工具、发模板），
        executions 记录每次尝试并承载幂等键，on_suspend 在挂起落库之后
        调用，负责发提示、订阅事件。引擎本身不认识这些副作用。
        human_tasks 不传时，wait_human 节点只挂起、不产生可处置的任务，
        适合只测流程语义的用例；生产装配必须传。
        clock 可注入，让截止时间在测试里可预测；max_steps 防止定义里
        出现没有挂起的环，把请求线程一直占着。
        """
        self._definition = definition
        self._store = store
        self._execute = execute
        self._executions = executions
        self._human_tasks = human_tasks
        self._clock = clock
        self._on_suspend = on_suspend
        self._max_steps = max_steps

    def start(self, instance_id: str, context: Mapping[str, Any] | None = None) -> Instance:
        """创建并推进一个新实例，直到它挂起或结束。

        返回推进后的实例；调用方看 status 就知道它停在哪里。
        id 重复会让存储层拒绝（DuplicateInstance），避免两次 start
        产生两个并行的实例。
        """
        instance = start_instance(self._definition, instance_id, context=context)
        return self._drive(self._store.create(instance))

    def resume(
        self,
        instance_id: str,
        outcome: str,
        context_update: Mapping[str, Any] | None = None,
    ) -> Instance:
        """用外部结果唤醒一个挂起的实例，然后继续推进到下一次挂起或结束。

        外部结果指用户回复、人工处置、GLPI 事件；outcome 必须在该等待节点的
        next 里声明过，否则抛 InvalidOutcome，引擎不会猜「大概是确认」。
        实例不是挂起状态时抛 IllegalState——重复唤醒不能把同一步走两遍。
        """
        instance = self._store.load(instance_id)
        if instance.status is not InstanceStatus.SUSPENDED:
            raise _illegal(instance)
        moved = apply_outcome(
            self._definition, instance, outcome, context_update=context_update
        )
        saved = self._store.save(moved, instance.row_version)
        return self._drive(saved)

    def resume_timeout(self, instance_id: str) -> Instance:
        """调度器到期时调用。等定时器的节点到期就是 fired，其余是 timeout。"""
        instance = self._store.load(instance_id)
        if instance.status is not InstanceStatus.SUSPENDED:
            raise _illegal(instance)
        node = self._definition.node(instance.current_node)
        return self.resume(instance_id, timeout_outcome(node))

    def submit_human_task(
        self,
        task_id: str,
        outcome: str,
        context_update: Mapping[str, Any] | None = None,
    ) -> Instance:
        """人工处置提交后唤醒实例。

        outcome 是人的明确选择（existing / retry / abandoned），不是布尔
        「已完成」。先校验实例仍挂在这个人工节点上，再标记任务、唤醒实例：
        顺序反过来会留下「任务已提交、实例其实早已超时结束」的孤儿记录。
        即便如此，校验和提交之间仍有窗口；内存实现单进程可接受，
        Postgres 实现要把两步放进同一事务。
        """
        if self._human_tasks is None:
            raise IllegalState("runner 没有配置人工任务存储，无法提交人工处置")
        task = self._human_tasks.load(task_id)
        instance = self._store.load(task.instance_id)
        if (
            instance.status is not InstanceStatus.SUSPENDED
            or instance.suspend_reason is not WaitReason.HUMAN
            or instance.current_node != task.node
        ):
            raise IllegalState(
                f"实例 {instance.id} 当前不在人工节点 {task.node} 上"
                f"（{instance.status}/{instance.current_node}）"
            )
        self._human_tasks.submit(task_id, outcome, self._clock())
        return self.resume(task.instance_id, outcome, context_update)

    def recover(self, instance_id: str) -> Instance:
        """进程重启后恢复一个实例。

        只处理两种残留：
        - running：崩溃在执行节点中途，_drive 会复用未收尾记录的幂等键重跑；
        - suspended + human：挂起已落库但人工任务没建出来，补建。

        其余状态原样返回。恢复不能推进挂起实例，否则会把「等人」误解成
        「等到了」。
        """
        instance = self._store.load(instance_id)
        if instance.status is InstanceStatus.RUNNING:
            return self._drive(instance)
        if (
            instance.status is InstanceStatus.SUSPENDED
            and instance.suspend_reason is WaitReason.HUMAN
        ):
            self._ensure_human_task(instance)
        return instance

    def recover_all(self) -> list[Instance]:
        """启动时扫描全部未结束实例。返回值按存储的列出顺序排列。

        单个实例恢复失败会直接抛出，不跳过继续。启动阶段宁可失败，
        也不带着「有一次未知状态的恢复没做成」继续服务：那会让后面的
        唤醒把实例推到更远的地方，掩盖真正的问题。
        """
        return [self.recover(instance.id) for instance in self._store.list_active()]

    def _drive(self, instance: Instance) -> Instance:
        """推进循环：执行节点、落库、遇到挂起或结束就返回。

        落库时机是这段代码的核心决定：每执行完一个可执行节点立刻保存，
        不等整轮推进结束。副作用已经发生、结果却没落库，崩溃后重跑会
        重复执行同一步。
        挂起时先落库再调 on_suspend，保证副作用失败时实例仍是挂起状态，
        可以重做；反过来则可能出现「消息已发、状态没存」。
        """
        for _ in range(self._max_steps):
            # 先对齐执行记录：实例已经迁移、记录却没来得及收尾的情况，
            # 再执行一次会重复副作用，只能补记。这一步放在每次循环开头，
            # 是为了让「上一步收尾失败、实例已保存」的残留也能被下一次
            # 恢复扫到，而不是永远留一条 STARTED。
            self._reconcile_open_execution(instance)

            plan = next_action(self._definition, instance, self._clock())

            if isinstance(plan, RunStep):
                row = self._open_or_begin_execution(instance, plan)
                execution = self._execute(plan.node, instance, row.key)
                moved = apply_result(
                    self._definition,
                    instance,
                    execution.result,
                    context_update=execution.context_update,
                )
                # 顺序不能反：先写实例，再收尾执行记录。
                # 反过来的话，崩溃在两次写之间会留下「记录说做完了、
                # 实例还停在原节点」，恢复时会被当成没执行过而重跑——
                # 副作用已经发生，这就是重复建单。
                instance = self._store.save(moved, instance.row_version)
                self._executions.complete(
                    row.key,
                    execution.result,
                    execution.context_update,
                    self._clock(),
                )
                continue

            if isinstance(plan, Suspend):
                suspended = replace(
                    instance,
                    status=InstanceStatus.SUSPENDED,
                    suspend_reason=plan.reason,
                    deadline=plan.deadline,
                )
                instance = self._store.save(suspended, instance.row_version)
                # 先落库再做副作用：副作用失败时实例仍然是挂起状态，
                # 可以重做；反过来则可能出现「消息已发、状态没存」。
                if plan.reason is WaitReason.HUMAN:
                    self._ensure_human_task(instance)
                if self._on_suspend is not None:
                    self._on_suspend(plan.node, instance)
                return instance

            assert isinstance(plan, Finish)
            completed = replace(
                instance,
                status=plan.status,
                suspend_reason=None,
                deadline=None,
            )
            return self._store.save(completed, instance.row_version)

        raise WorkflowLoop(
            f"实例 {instance.id} 一轮推进超过 {self._max_steps} 步，疑似没有挂起的环"
        )

    def _reconcile_open_execution(self, instance: Instance) -> None:
        """把「实例已经走过、记录还开着」的残留补记成 RECOVERED。

        判断依据是节点和 attempt 都一致才算同一步。只看节点不够：
        retryable 自环会回到同名节点，但 attempt 已经加一，
        那是新的一次尝试，不能复用旧键。
        """
        row = self._executions.find_open(instance.id)
        if row is None:
            return
        if row.node == instance.current_node and row.attempt == instance.attempt:
            return
        self._executions.mark_recovered(row.key, self._clock())

    def _open_or_begin_execution(self, instance: Instance, plan: RunStep) -> StepExecution:
        """取当前这一步的执行记录；没有就新建。

        存在未收尾记录且节点、attempt 都对得上，说明上次进程死在
        execute 期间。复用那条记录（也就是复用幂等键）才是安全的；
        新建记录会换一个键，下游会把重跑当成第二次业务请求。
        """
        row = self._executions.find_open(instance.id)
        if row is not None:
            return row
        return self._executions.begin(
            StepExecution(
                key=uuid4().hex,
                instance_id=instance.id,
                node=plan.node.name,
                attempt=plan.attempt,
                status=StepExecutionStatus.STARTED,
                requested_at=self._clock(),
            )
        )

    def _ensure_human_task(self, instance: Instance) -> None:
        """挂起在人工节点时保证有一条可处置的任务。

        cause 和 may_have_committed 来自最近一次收尾的执行记录，而不是
        从实例上下文里猜：这两个字段只用于人工判断，放上下文会污染
        步骤之间的数据契约。找不到记录时留空，宁可不完整也不编。
        """
        if self._human_tasks is None:
            return
        if self._human_tasks.find_open(instance.id) is not None:
            return
        node = self._definition.node(instance.current_node)
        last = self._executions.last_completed(instance.id)
        result = last.result if last is not None else None
        now = self._clock()
        self._human_tasks.create(
            HumanTask(
                id=uuid4().hex,
                instance_id=instance.id,
                node=node.name,
                cause=result.cause if result is not None else None,
                may_have_committed=(
                    result.may_have_committed if result is not None else False
                ),
                # timeout 是调度器的出口，不是人能选的处置方式。
                allowed_outcomes=frozenset(
                    outcome for outcome in node.next if outcome != TIMEOUT_OUTCOME
                ),
                status=HumanTaskStatus.OPEN,
                created_at=now,
                deadline=instance.deadline if instance.deadline is not None else now,
            )
        )


def _illegal(instance: Instance) -> IllegalState:
    """构造统一的 IllegalState，避免每处重复拼状态描述。"""
    return IllegalState(f"实例 {instance.id} 当前是 {instance.status}，不能按外部结果推进")
