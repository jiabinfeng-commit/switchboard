"""打印一次 agent 步骤的两层轨迹：外层流程引擎 + 内层 LangGraph。

同一行里的 key 是外层 `step_execution.key`。它把「流程推进到哪一步」
和「图里为什么这么走」关联起来：
    - 外层看到：run 了哪个节点、返回什么动作、实例落库到哪个版本；
    - 内层看到：模型每轮输出什么、工具调用成功还是被拒、草稿为什么被打回。

只看 StepResult 无法区分「流程走错」还是「模型判断错」，所以两层要一起看。
用法：
    python scripts/trace_agent.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.trace_flow import NOW, TraceExecutions, TraceStore, hook
from switchboard.agent.input import ModelView, StepInput, ToolSideEffect, ToolSpec
from switchboard.agent.langgraph_runtime import LangGraphRuntime
from switchboard.agent.result import StepAction, StepResult
from switchboard.agent.runtime import Draft, ToolCallRequest
from switchboard.agent.verify import ValueRef
from switchboard.workflow.definition import load_definition
from switchboard.workflow.runner import Execution, WorkflowRunner
from switchboard.workflows.fault_report import FAULT_REPORT

ASSETS = [{"id": 12345, "model": "ThinkPad T14"}]


class ScriptedModel:
    """按脚本返回模型输出：先查资产，再编一个 id，被拒后改正。"""

    def __init__(self) -> None:
        self._outputs = [
            ToolCallRequest("asset_search", {"q": "SN12345"}),
            Draft({"asset_id": ValueRef(999, "c-1", "[0].id")}),
            Draft({"asset_id": ValueRef(12345, "c-1", "[0].id")}),
        ]

    def respond(self, view, invocations, feedback):
        return self._outputs.pop(0)


def asset_search(arguments):
    """假工具：不管参数，永远返回同一条资产。"""
    return ASSETS


def build_step_input() -> StepInput:
    """构造 draft 节点收到的输入：一个只读工具、一个输出 schema。"""
    return StepInput(
        view=ModelView(
            user_message="电脑开不了机，序列号 SN12345",
            tools=(
                ToolSpec(
                    name="asset_search",
                    description="按序列号查资产",
                    parameters={
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                        "additionalProperties": False,
                    },
                    side_effect=ToolSideEffect.READ,
                ),
            ),
            output_schema={"asset_id": "资产 id"},
        ),
        user_id="u-1",
        allowed_entities=(3,),
    )


def report_node(key: str, node: str, update: dict) -> None:
    """打印图内一次节点执行的结果，行首带上外层幂等键。"""
    detail = ""
    if node == "tool":
        invocation = list(update["invocations"].values())[-1]
        detail = (
            f" {invocation.call_id} {invocation.tool_name} "
            f"{'ok' if invocation.ok else invocation.error}"
        )
    elif update.get("result") is not None:
        result = update["result"]
        detail = f" 终止 -> {result.action} cause={result.cause}"
    elif "output" in update and update["output"] is not None:
        detail = f" -> {type(update['output']).__name__}"
    elif update.get("feedback"):
        detail = f" 打回：{update['feedback'].splitlines()[0]}"

    print(f"    [inner] {key[:8]} {node:<6}{detail}")


def main() -> None:
    """跑一次 start，draft 节点由 LangGraphRuntime 执行。"""
    model = ScriptedModel()

    def execute(node, instance, idempotency_key):
        """外层执行器：draft 交给 agent，其余节点用固定结果。"""
        print(f"  [outer]    node={node.name} key={idempotency_key[:8]}")
        if node.name != "draft":
            return Execution(StepResult(StepAction.SUCCEEDED, False))

        runtime = LangGraphRuntime(
            model,
            {"asset_search": asset_search},
            on_node=lambda name, update: report_node(idempotency_key, name, update),
        )
        return Execution(runtime.run(build_step_input(), idempotency_key), {"asset_id": 12345})

    runner = WorkflowRunner(
        load_definition(FAULT_REPORT),
        TraceStore(),
        execute,
        TraceExecutions(),
        clock=lambda: NOW,
        on_suspend=hook,
    )

    print("=== 外层流程 start，draft 内部跑 LangGraph ===")
    instance = runner.start("i-1")
    print(f"  => node={instance.current_node} status={instance.status} v={instance.row_version}")


if __name__ == "__main__":
    main()
