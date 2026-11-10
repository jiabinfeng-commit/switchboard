"""用真实模型跑一步，观察设计在真实模型面前的表现。

用法：
    python3 scripts/demo.py                         # 默认案例
    python3 scripts/demo.py "我电脑开不了机"          # 自定义员工输入
    python3 scripts/demo.py --show-prompt "..."      # 同时打印发给模型的内容
    python3 scripts/demo.py --trace "..."            # 打印每一轮模型看到了什么、做了什么
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from switchboard.agent.input import ModelView, StepInput, ToolSpec
from switchboard.agent.llm import OpenAIChatModel
from switchboard.agent.runtime import SingleAgentRuntime

ASSETS = [
    {
        "id": 12345,
        "serial": "SN12345",
        "model": "ThinkPad T14",
        "owner": {"name": "张三", "employee_no": "E1001"},
        "warranty_end": "2027-03-01",
    },
    {
        "id": 12346,
        "serial": "SN12346",
        "model": "ThinkPad T14",
        "owner": {"name": "李四", "employee_no": "E1002"},
        "warranty_end": "2026-12-15",
    },
]

KB = [
    {"id": 7, "title": "笔记本无法开机排查步骤", "updated_at": "2026-09-02"},
]


def asset_search(arguments):
    """演示用的资产查询：按序列号或员工姓名做子串匹配。

    返回原始记录列表；查询词为空时返回空列表，不返回全部资产。
    """
    q = str(arguments.get("q", "")).strip().lower()
    if not q:
        return []
    return [
        asset
        for asset in ASSETS
        if q in asset["serial"].lower() or q in asset["owner"]["name"]
    ]


def kb_search(arguments):
    """演示用的知识库查询：按标题子串匹配，返回匹配的文章列表。"""

    q = str(arguments.get("q", "")).strip()
    return [item for item in KB if q in item["title"]]


class TracingModel:
    """包一层，把每一轮的真实输入与输出打出来。不改变行为，只观测。

    注意它包在 runtime 和真实模型之间：runtime 每轮调用 respond 时，
    它先打印本轮实际发给模型的内容，再转发给真实模型。
    这样看到的 prompt 就是模型真正拿到的那一份，包括工具结果和打回原因。
    """

    def __init__(self, inner, show_prompt: bool = False):
        """保存被包装的模型，初始化轮次计数和 prompt 打印开关。

        show_prompt 只在该包装器开启时生效，默认只打印每轮的摘要和模型输出。
        """

        self._inner = inner
        self._show_prompt = show_prompt
        self._turn = 0

    def respond(self, view, invocations, feedback):
        """打印本轮实际输入，再把请求原样转发给真实模型。

        打印的 prompt 就是模型真正拿到的那一份，包括工具结果和打回原因；
        包装器不改变请求和返回，所以观察到的行为就是 runtime 的真实行为。
        """
        from switchboard.agent.prompt import build_data_prompt, build_system_prompt

        self._turn += 1
        print(
            f"--- 第 {self._turn} 轮 | 已有调用: {list(invocations) or '无'} "
            f"| 被打回: {'是' if feedback else '否'}"
        )
        if self._show_prompt:
            if self._turn == 1:
                print("=" * 20, "system prompt", "=" * 20)
                print(build_system_prompt(view))
            print("=" * 20, "data prompt", "=" * 20)
            print(build_data_prompt(view, invocations, feedback))
        if feedback:
            print(f"    打回原因: {feedback}")
        output = self._inner.respond(view, invocations, feedback)
        print(f"    模型输出: {output}")
        return output


def main() -> None:
    """解析命令行参数，装配 runtime，跑一步并打印结果。

    支持 --trace（打印每轮摘要）和 --show-prompt（额外打印完整 prompt）；
    第一个非标志参数作为员工输入，缺省使用内置案例。
    """
    flags = {"--show-prompt", "--trace"}
    args = [arg for arg in sys.argv[1:] if arg not in flags]
    show_prompt = "--show-prompt" in sys.argv
    trace = "--trace" in sys.argv
    user_message = args[0] if args else "我电脑开不了机，序列号 SN12345，麻烦帮我建个单"

    load_dotenv()
    model = OpenAIChatModel.from_env()
    if trace or show_prompt:
        model = TracingModel(model, show_prompt=show_prompt)

    view = ModelView(
        user_message=user_message,
        tools=(
            ToolSpec(
                name="asset_search",
                description="按序列号或员工姓名查资产，返回资产记录列表",
                parameters={
                    "type": "object",
                    "properties": {
                        "q": {
                            "type": "string",
                            "description": "序列号或员工姓名",
                        },
                    },
                    "required": ["q"],
                    "additionalProperties": False,
                },
            ),
            ToolSpec(
                name="kb_search",
                description="按关键词查知识库文章，返回匹配的文章列表",
                parameters={
                    "type": "object",
                    "properties": {
                        "q": {"type": "string", "description": "关键词"},
                    },
                    "required": ["q"],
                    "additionalProperties": False,
                },
            ),
        ),
        output_schema={
            "asset_id": "资产 id（整数），只能来自 asset_search 的返回",
            "asset_model": "设备型号，只能来自 asset_search 的返回",
            "asset_serial": "设备序列号，只能来自 asset_search 的返回",
        },
    )
    step_input = StepInput(view=view, user_id="u-1001", allowed_entities=(3,))

    runtime = SingleAgentRuntime(
        model,
        tools={"asset_search": asset_search, "kb_search": kb_search},
    )
    result = runtime.run(step_input, "demo-1")

    print(f"动作: {result.action}")
    print(f"原因: {result.cause}")
    print(f"副作用可能已发生: {result.may_have_committed}")
    print(f"证据: {result.evidence}")
    print(f"产出: {result.payload}")


if __name__ == "__main__":
    main()
