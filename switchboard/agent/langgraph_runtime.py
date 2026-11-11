"""用 LangGraph 图实现的单步运行时，与 `SingleAgentRuntime` 行为等价。

图只描述一次 `run()` 内部的循环，不跨挂起、不带 checkpointer：
    START -> model --tool_call--> tool -> model
                  --ask_user---> ask  -> END 或 model
                  --draft------> draft -> END 或 model

为什么状态里只有 `view` 而没有整个 `StepInput`：user_id、allowed_entities
属于外层身份边界，模型永远看不到，也没有理由让它们进入图状态。图状态里
只有本次步骤的临时数据，进程重启后整步重跑（ADR 0004 决策 3、4）。
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, TypedDict

from langgraph.graph import END, START, StateGraph

from switchboard.agent.input import ModelView, StepInput
from switchboard.agent.questions import (
    AskUser,
    referenced_calls,
    render_question,
    validate_ask,
)
from switchboard.agent.result import StepAction, StepResult
from switchboard.agent.runtime import (
    Draft,
    Model,
    ModelError,
    ModelOutput,
    RuntimeLimits,
    ToolCallRequest,
    ToolExecutor,
    execute_tool_call,
    need_human,
    require_read_only_tools,
    verify_draft,
)
from switchboard.agent.tool import ToolInvocation


class _StepState(TypedDict, total=False):
    """一次运行内的临时状态。刻意不放实例 id、身份和流程上下文。"""

    view: ModelView
    invocations: dict[str, ToolInvocation]
    feedback: str | None
    turns: int
    verify_failures: int
    seq: int
    output: ModelOutput | None
    result: StepResult | None


NodeObserver = Callable[[str, Mapping[str, Any]], None]


class LangGraphRuntime:
    """把单步循环画成图，节点职责与代码循环版本一一对应。"""

    def __init__(
        self,
        model: Model,
        tools: Mapping[str, ToolExecutor],
        limits: RuntimeLimits = RuntimeLimits(),
        on_node: NodeObserver | None = None,
    ) -> None:
        """保存依赖并编译图。

        图本身无状态，每次 run 传入新的初始状态；因此可以直接复用，
        不需要为每次执行重新 compile。on_node 只用于观察轨迹，
        不参与决策——需要它是因为图里的循环对调用方不可见。
        """
        self._model = model
        self._tools = dict(tools)
        self._limits = limits
        self._on_node = on_node
        self._graph = self._build_graph()

    def run(self, step_input: StepInput, idempotency_key: str) -> StepResult:
        """执行一步，返回恰好一个 StepResult。

        idempotency_key 与外层执行记录对应，当前工具只读，因此不消费它
        （ADR 0004 决策 2）。用 stream 而不是 invoke 是为了让 on_node
        能看到每个节点的更新，轨迹才能和 StepResult 对账。
        """
        tool_specs = {tool.name: tool for tool in step_input.view.tools}
        require_read_only_tools(tool_specs)
        missing = tool_specs.keys() - self._tools.keys()
        if missing:
            raise RuntimeError(f"以下工具没有执行器：{sorted(missing)}")

        state = _initial_state(step_input.view)
        result: StepResult | None = None
        for chunk in self._graph.stream(state, stream_mode="updates"):
            for node, update in chunk.items():
                if self._on_node is not None:
                    self._on_node(node, update)
                if update.get("result") is not None:
                    result = update["result"]

        if result is None:
            # 所有终止路径都必须显式写 result；走到这里说明图装配错了
            raise RuntimeError("图结束时没有产生 StepResult")
        return result

    # ---- 图装配 ----

    def _build_graph(self):
        """装配节点和边。路由只读状态，不调用模型，保证可重复。"""
        builder = StateGraph(_StepState)
        builder.add_node("model", self._model_node)
        builder.add_node("tool", self._tool_node)
        builder.add_node("ask", self._ask_node)
        builder.add_node("draft", self._draft_node)

        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model",
            _route_after_model,
            {"tool": "tool", "ask": "ask", "draft": "draft", "model": "model", END: END},
        )
        builder.add_edge("tool", "model")
        for node in ("ask", "draft"):
            builder.add_conditional_edges(
                node, _route_after_verify, {END: END, "model": "model"}
            )
        return builder.compile()

    # ---- 节点 ----

    def _model_node(self, state: _StepState) -> dict[str, Any]:
        """调用一次模型。

        预算检查放在调用之前：图已经在循环里，不能在边上设次数上限，
        否则要么漏掉「最后一次输出有效」的情况，要么多调一次模型。
        解析失败不消耗校验预算，只把错误作为 feedback 交给下一轮。
        """
        turns = state.get("turns", 0)
        if turns >= self._limits.max_model_turns:
            return {"result": need_human("turn_budget_exhausted")}

        try:
            output = self._model.respond(
                state["view"],
                state.get("invocations", {}),
                state.get("feedback"),
            )
        except ModelError as exc:
            return {
                "turns": turns + 1,
                "output": None,
                "feedback": f"上一步输出无法解析：{exc}",
            }
        return {"turns": turns + 1, "output": output}

    def _tool_node(self, state: _StepState) -> dict[str, Any]:
        """执行一次工具调用，把记录和失败原因写回状态。

        call_id 由 seq 生成而不是 uuid：一次运行内按 c-1、c-2 递增，
        轨迹里可以直接看出模型第几次尝试，和代码循环版本保持一致。
        """
        request = state["output"]
        assert isinstance(request, ToolCallRequest)
        seq = state.get("seq", 0) + 1
        invocation = execute_tool_call(
            request,
            f"c-{seq}",
            {tool.name: tool for tool in state["view"].tools},
            self._tools,
        )
        invocations = dict(state.get("invocations", {}))
        invocations[invocation.call_id] = invocation
        return {
            "seq": seq,
            "invocations": invocations,
            "output": None,
            "feedback": None if invocation.ok else f"工具调用失败：{invocation.error}",
        }

    def _ask_node(self, state: _StepState) -> dict[str, Any]:
        """校验追问意图；通过就渲染成句子并结束，否则打回模型。"""
        ask = state["output"]
        assert isinstance(ask, AskUser)
        invocations = state.get("invocations", {})
        problems = validate_ask(
            ask,
            invocations,
            set(state["view"].output_schema),
        )
        if not problems:
            evidence = tuple(
                sorted(set(invocations) | set(referenced_calls(ask)))
            )
            return {
                "result": StepResult(
                    action=StepAction.NEED_INPUT_FIX,
                    may_have_committed=False,
                    cause="model_asked_user",
                    evidence=evidence,
                    payload=render_question(ask, invocations),
                )
            }
        return self._reject(state, "这次追问不成立：\n" + "\n".join(problems), "invalid_ask")

    def _draft_node(self, state: _StepState) -> dict[str, Any]:
        """校验草稿出处；通过就结束，否则打回模型。"""
        draft = state["output"]
        assert isinstance(draft, Draft)
        invocations = state.get("invocations", {})
        problems = verify_draft(draft, invocations)
        if not problems:
            return {
                "result": StepResult(
                    action=StepAction.SUCCEEDED,
                    may_have_committed=False,
                    evidence=tuple(
                        sorted({ref.call_id for ref in draft.fields.values()})
                    ),
                    payload=draft,
                )
            }
        return self._reject(
            state,
            "以下字段无法追溯到工具结果，请重新选择出处：\n" + "\n".join(problems),
            "verification_failed",
        )

    def _reject(self, state: _StepState, feedback: str, cause: str) -> dict[str, Any]:
        """登记一次校验失败：未超预算就带着 feedback 回模型。"""
        failures = state.get("verify_failures", 0) + 1
        if failures > self._limits.max_verify_retries:
            return {"verify_failures": failures, "result": need_human(cause)}
        return {"verify_failures": failures, "output": None, "feedback": feedback}


def _initial_state(view: ModelView) -> _StepState:
    """每次 run 的初始状态。只包含本次步骤的临时数据。"""
    return {
        "view": view,
        "invocations": {},
        "feedback": None,
        "turns": 0,
        "verify_failures": 0,
        "seq": 0,
        "output": None,
        "result": None,
    }


def _route_after_model(state: _StepState) -> str:
    """模型节点之后走哪条边。

    output 为 None 表示上一轮解析失败，直接回模型，不消耗校验预算。
    """
    if state.get("result") is not None:
        return END
    output = state.get("output")
    if output is None:
        return "model"
    if isinstance(output, ToolCallRequest):
        return "tool"
    if isinstance(output, AskUser):
        return "ask"
    return "draft"


def _route_after_verify(state: _StepState) -> str:
    """追问/草稿校验之后：有终态就结束，否则回模型重选。"""
    return END if state.get("result") is not None else "model"


__all__ = ["LangGraphRuntime"]
