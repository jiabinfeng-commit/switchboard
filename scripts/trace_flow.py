"""按时间顺序打印一次流程执行：每一步做了什么、落库版本怎么变。

只用于学习，不参与生产。包装了实例存储和执行记录存储，在每次
begin / save / complete 时打印一行；执行器是假的，不调模型和 GLPI。

用法：
    python scripts/trace_flow.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from switchboard.agent.result import StepAction, StepResult
from switchboard.workflow.definition import load_definition
from switchboard.workflow.execution import InMemoryStepExecutionStore
from switchboard.workflow.runner import Execution, WorkflowRunner
from switchboard.workflow.store import InMemoryInstanceStore
from switchboard.workflows.fault_report import FAULT_REPORT

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)


class TraceStore(InMemoryInstanceStore):
    """每次实例落库后打印新状态和版本号。"""

    def save(self, instance, expected_version):
        result = super().save(instance, expected_version)
        print(
            f"  [save]     node={result.current_node} "
            f"status={result.status} v={result.row_version}"
        )
        return result


class TraceExecutions(InMemoryStepExecutionStore):
    """每次执行记录开始和收尾时打印。"""

    def begin(self, execution):
        row = super().begin(execution)
        print(f"  [begin]    node={row.node} attempt={row.attempt} key={row.key[:6]}")
        return row

    def complete(self, key, result, context_update, finished_at):
        row = super().complete(key, result, context_update, finished_at)
        print(f"  [complete] key={key[:6]} action={row.result.action}")
        return row


def execute(node, instance, idempotency_key):
    """假执行器：按节点名返回固定结果，打印收到的幂等键。"""
    print(
        f"  [execute]  node={node.name} "
        f"attempt={instance.attempt} key={idempotency_key[:6]}"
    )
    if node.name == "draft":
        return Execution(StepResult(StepAction.SUCCEEDED, False), {"asset_id": 1})
    if node.name == "create":
        return Execution(StepResult(StepAction.SUCCEEDED, True), {"ticket_id": 9})
    return Execution(StepResult(StepAction.SUCCEEDED, False))


def hook(node, instance):
    print(
        f"  [suspend]  node={node.name} "
        f"reason={instance.suspend_reason} v={instance.row_version}"
    )


def main() -> None:
    runner = WorkflowRunner(
        load_definition(FAULT_REPORT),
        TraceStore(),
        execute,
        TraceExecutions(),
        clock=lambda: NOW,
        on_suspend=hook,
    )

    print("=== start ===")
    instance = runner.start("i-1")
    print(f"  => node={instance.current_node} status={instance.status} v={instance.row_version}")

    print("=== resume confirmed ===")
    instance = runner.resume("i-1", "confirmed")
    print(f"  => node={instance.current_node} status={instance.status} v={instance.row_version}")


if __name__ == "__main__":
    main()
