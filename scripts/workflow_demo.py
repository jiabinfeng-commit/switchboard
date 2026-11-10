"""把「报障受理」流程跑一遍，看每一步的迁移和落库版本。

执行器是假的：不调模型、不调 GLPI，只按固定剧本返回结果。
目的是观察引擎本身的行为——什么时候停、停在哪里、在等谁。

用法：
    python3 scripts/workflow_demo.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.definition import load_definition
from switchboard.workflow.execution import InMemoryStepExecutionStore
from switchboard.workflow.human import InMemoryHumanTaskStore
from switchboard.workflow.runner import Execution, WorkflowRunner
from switchboard.workflow.store import InMemoryInstanceStore
from switchboard.workflows.fault_report import FAULT_REPORT

NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def scripted(drafts: int = 1):
    """按节点名返回执行结果，并按调用顺序消耗。"""
    draft = Execution(
        StepResult(StepAction.SUCCEEDED, False),
        {"asset_id": 12345, "asset_serial": "SN12345"},
    )
    queue = {
        "draft": [draft] * drafts,
        "create": [
            Execution(
                StepResult(StepAction.SUCCEEDED, True),
                {"ticket_id": 9001},
            )
        ],
        "receipt": [Execution(StepResult(StepAction.SUCCEEDED, False))],
    }

    def execute(node, instance, idempotency_key):
        """按节点名取下一份剧本结果，打印实际执行了哪个节点。

        剧本是预先排好的；如果某个节点被多调了一次，队列会空掉并抛
        IndexError，这本身就是流程走错的可观测信号。
        幂等键打出来，是为了看到「同一节点重试」拿到的是不同的键。
        """

        result = queue[node.name].pop(0)
        print(
            f"  执行节点 {node.name}（{node.type}） -> {result.result.action}"
            f"，幂等键 {idempotency_key[:8]}"
        )
        return result

    return execute


def hook(node, instance):
    """挂起回调：打印停在哪个节点、等谁、截止时间以及落库版本。"""

    print(
        f"  挂起在 {node.name}（{instance.suspend_reason}），"
        f"截止 {instance.deadline:%Y-%m-%d %H:%M}，row_version={instance.row_version}"
    )


def show(title, instance):
    """打印一个实例的关键状态，用来对照迁移过程和上下文变化。"""

    print(f"\n[{title}]")
    print(
        f"  节点={instance.current_node} 状态={instance.status} "
        f"上下文={dict(instance.context)}"
    )


def main() -> None:
    """跑四条路径：正常确认、用户要求修改、进程崩溃恢复、转人工。

    它们共用同一份流程定义，只有执行结果和外部唤醒不同；
    用它观察「同样的定义，不同输入如何落到不同分支」。
    """
    definition = load_definition(FAULT_REPORT)
    store = InMemoryInstanceStore()
    runner = WorkflowRunner(
        definition,
        store,
        scripted(),
        InMemoryStepExecutionStore(),
        human_tasks=InMemoryHumanTaskStore(),
        clock=lambda: NOW,
        on_suspend=hook,
    )

    print("开始：生成草稿 -> 等用户确认")
    show("start", runner.start("demo-1"))

    print("\n用户确认 -> 建单 -> 回执 -> 等 GLPI 关单")
    show("确认", runner.resume("demo-1", "confirmed", {"user_answer": "对"}))

    print("\nGLPI 工单关闭 -> 流程结束")
    show("关闭", runner.resume("demo-1", "closed"))

    print("\n另一条路径：用户要求修改")
    runner2 = WorkflowRunner(
        definition,
        InMemoryInstanceStore(),
        scripted(drafts=2),
        InMemoryStepExecutionStore(),
        human_tasks=InMemoryHumanTaskStore(),
        clock=lambda: NOW,
    )
    runner2.start("demo-2")
    show("修改", runner2.resume("demo-2", "revised", {"last_user_message": "不是这台"}))

    print("\n崩溃恢复：草稿执行到一半进程退出")
    store3 = InMemoryInstanceStore()
    exec3 = InMemoryStepExecutionStore()
    runner3 = WorkflowRunner(
        definition,
        store3,
        crash_once(scripted()),
        exec3,
        clock=lambda: NOW,
    )
    try:
        runner3.start("demo-3")
    except RuntimeError:
        pass
    open_row = exec3.find_open("demo-3")
    print(f"  留下未收尾的记录：节点={open_row.node} 幂等键={open_row.key[:8]}")
    runner4 = WorkflowRunner(
        definition, store3, scripted(), exec3, clock=lambda: NOW
    )
    recovered = runner4.recover("demo-3")
    show("恢复", recovered)
    print(f"  重跑用的是同一个幂等键：{exec3.list_for_instance('demo-3')[0].key == open_row.key}")

    print("\n建单结果未知 -> 转人工")
    tasks5 = InMemoryHumanTaskStore()
    runner5 = WorkflowRunner(
        definition,
        InMemoryInstanceStore(),
        manual_script(),
        InMemoryStepExecutionStore(),
        human_tasks=tasks5,
        clock=lambda: NOW,
    )
    runner5.start("demo-4")
    show("用户确认", runner5.resume("demo-4", "confirmed"))
    task = tasks5.find_open("demo-4")
    print(
        f"  人工任务：原因={task.cause} 可能已提交={task.may_have_committed} "
        f"可选处置={sorted(task.allowed_outcomes)}"
    )
    show("工程师标记工单已存在", runner5.submit_human_task(task.id, "existing"))


def crash_once(inner):
    """第一次执行就抛异常，模拟进程死在节点中间。"""

    state = {"crashed": False}

    def execute(node, instance, idempotency_key):
        if not state["crashed"]:
            state["crashed"] = True
            print(f"  执行节点 {node.name} 时崩溃（幂等键 {idempotency_key[:8]}）")
            raise RuntimeError("模拟进程崩溃")
        return inner(node, instance, idempotency_key)

    return execute


def manual_script():
    """草稿成功，建单返回 need_human（结果未知，可能已建单）。"""
    queue = [
        Execution(
            StepResult(StepAction.SUCCEEDED, False),
            {"asset_id": 12345, "asset_serial": "SN12345"},
        ),
        Execution(StepResult(StepAction.NEED_HUMAN, True, cause="glpi_timeout")),
    ]

    def execute(node, instance, idempotency_key):
        result = queue.pop(0)
        print(f"  执行节点 {node.name}（{node.type}） -> {result.result.action}")
        return result

    return execute


if __name__ == "__main__":
    main()
