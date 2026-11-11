"""单步执行：把模型、工具、校验串成一个循环。

一次执行内部可以有多轮「模型要调工具 → 代码执行 → 结果回给模型」，
也可以有多轮「模型交草稿 → 校验不通过 → 打回去重选」。

两层重试必须分清：
    这里是**内部循环**，对上游不可见；
    StepResult 里的 retryable 是给上游的承诺，意味着整个步骤可以重跑。
内部循环用尽后才选一个动作交给上游。

安全边界（对应「结构分离 + 效果兜底」）：
    - 模型只能拿到 ModelView，身份和实体范围在代码侧，进不了 prompt
    - 工具名必须在 view.tools 白名单里才执行
    - 工具参数里出现身份字段直接拒绝执行
    - 模型给的每个值必须带出处，校验不过就打回重选
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from jsonschema import Draft202012Validator

from switchboard.agent.input import ModelView, StepInput, ToolSideEffect, ToolSpec
from switchboard.agent.questions import (
    AskUser,
    referenced_calls,
    render_question,
    validate_ask,
)
from switchboard.agent.result import StepAction, StepResult
from switchboard.agent.tool import ToolInvocation
from switchboard.agent.verify import ValueRef, verify_ref


@dataclass(frozen=True)
class ToolCallRequest:
    """模型提出的一次工具调用请求。它只是请求，模型自己没有执行权。"""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Draft:
    """模型产出的结构化草稿。

    本切片要求所有字段都是 ValueRef：每个值都必须能追溯到某次工具返回。
    TODO: 从员工原话里提取的值（如「影响度=高」）出处是对话消息而非工具结果，
    这类校验需要另一套规则，留到后续切片。
    """

    fields: Mapping[str, ValueRef] = field(default_factory=dict)


ModelOutput = ToolCallRequest | Draft | AskUser


class ModelError(Exception):
    """模型输出无法解析成 ToolCallRequest 或 Draft。打回重选，不算执行失败。"""


class Model(Protocol):
    def respond(
        self,
        view: ModelView,
        invocations: Mapping[str, ToolInvocation],
        feedback: str | None,
    ) -> ModelOutput:
        """根据可见内容和已有调用记录，给出下一步。

        feedback 非空时表示上一轮被拒的原因，模型据此修正。
        """
        ...


ToolExecutor = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True)
class RuntimeLimits:
    max_model_turns: int = 6
    max_verify_retries: int = 2


# 模型不得通过工具参数传入身份字段。身份由代码注入，
# 允许模型传 user_id 就等于允许它替别人查数据。
FORBIDDEN_ARGUMENT_KEYS = frozenset({"user_id", "owner_id", "requester_id"})


def validate_tool_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> str | None:
    """按工具的参数 schema 校验。返回错误摘要，通过时返回 None。"""
    validator = Draft202012Validator(spec.parameters)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.path))
    if not errors:
        return None
    # 只回前三条：模型需要知道哪里错了，不需要一份完整报告
    return "; ".join(error.message for error in errors[:3])


@runtime_checkable
class AgentRuntime(Protocol):
    """一步 agent 执行的契约，外层流程引擎只认识它（ADR 0004）。

    实现可以是这里的单步骤循环，也可以是 LangGraph 图；共同点是：
    - 输入只有 StepInput，不知道实例、时间、挂起；
    - 输出恰好一个 StepResult，内部循环对上游不可见；
    - 必须一次返回，不能把「等人」「等事件」藏在内部。
    """

    def run(self, step_input: StepInput, idempotency_key: str) -> StepResult:
        """执行一步。

        idempotency_key 由外层执行记录提供（ADR 0003）。当前 agent 内部
        工具只读，所以实现暂时不消费它；接口保留是为了将来允许幂等写操作
        时不用改契约。
        """
        ...


def require_read_only_tools(tool_specs: Mapping[str, ToolSpec]) -> None:
    """检查 agent 内部工具是否全部只读。

    写工具放进 agent 会让整步重跑不再安全：执行记录停在 started 时，
    恢复会重跑，写操作就发生两次。写操作放外层 tool 节点，由外层
    幂等键和重试策略管（ADR 0004 决策 3）。
    """
    writing = sorted(
        name
        for name, spec in tool_specs.items()
        if spec.side_effect is not ToolSideEffect.READ
    )
    if writing:
        raise RuntimeError(f"agent 内部只允许只读工具：{writing}")


def execute_tool_call(
    request: ToolCallRequest,
    call_id: str,
    tool_specs: Mapping[str, ToolSpec],
    tools: Mapping[str, ToolExecutor],
) -> ToolInvocation:
    """执行一次工具调用，无论成败都返回 ToolInvocation 记录。

    依次检查：工具在白名单里、参数不含身份字段、参数符合 JSON Schema。
    任何一项不过都不执行，而是记一条带 error 的调用。这样做有两个目的：
    模型能看到自己错在哪并改正；事后审计能还原它尝试过什么。
    执行器抛异常也收敛成记录，不让一次工具故障打断整个循环。
    """
    # 以下四种情况都写成 ToolInvocation：它们都是「模型尝试过一次」的事实，
    # 不记录就查不出模型在乱试什么。代价是这类记录没有返回值，
    # 引用它的 ValueRef 会因为 CALL_FAILED 被拒——这正是期望行为。
    spec = tool_specs.get(request.tool_name)
    if spec is None:
        return ToolInvocation(
            call_id=call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            error="tool_not_allowed",
        )

    forbidden = FORBIDDEN_ARGUMENT_KEYS & set(request.arguments)
    if forbidden:
        return ToolInvocation(
            call_id=call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            error=f"forbidden_argument:{sorted(forbidden)}",
        )

    # 参数不符合 schema 就不执行。否则执行器读不到参数会返回空结果，
    # 模型把它当成「查无记录」，一路错下去且没人发现。
    argument_error = validate_tool_arguments(spec, request.arguments)
    if argument_error:
        return ToolInvocation(
            call_id=call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            error=f"invalid_arguments:{argument_error}",
        )

    try:
        result = tools[request.tool_name](request.arguments)
    except Exception as exc:  # 工具实现的问题不应让整个执行崩掉
        return ToolInvocation(
            call_id=call_id,
            tool_name=request.tool_name,
            arguments=dict(request.arguments),
            error=f"execution_error:{exc}",
        )

    return ToolInvocation(
        call_id=call_id,
        tool_name=request.tool_name,
        arguments=dict(request.arguments),
        result=result,
    )


def verify_draft(
    draft: Draft,
    invocations: Mapping[str, ToolInvocation],
) -> list[str]:
    """逐个字段校验草稿的出处，返回问题列表（空列表表示全部通过）。

    每个字段必须是 ValueRef，且路径能在指定调用的 result 里解析出相等的值。
    这里检查的是「值从哪来」，不是「值对不对」——语义正确性由模型负责，
    代码只保证它没有凭空生成。这个边界写在 ADR 0001 第 5 条。
    """
    problems: list[str] = []
    for name, ref in draft.fields.items():
        if not isinstance(ref, ValueRef):
            problems.append(f"{name}: 缺少出处，所有字段都必须是 ValueRef")
            continue
        verified = verify_ref(ref, invocations)
        if not verified.ok:
            problems.append(f"{name}: {verified.rejection} {verified.detail}")
    return problems


def need_human(cause: str) -> StepResult:
    """构造「需要人工」的结果。

    内部预算用尽、追问结构不合法、草稿始终无法追溯来源，都属于系统侧
    处理不了的情况；让员工补信息也没用，只能交人工。
    本切片没有写操作，所以 may_have_committed 固定为 False；
    接入建单后要按实际是否发出请求来填。
    """
    return StepResult(
        action=StepAction.NEED_HUMAN,
        may_have_committed=False,
        cause=cause,
    )


class SingleAgentRuntime:
    """用代码循环实现的单步运行时，冻结在切片 3b，作为对照与回退。

    不再增加新能力；并行、专家子图等结构只加进 LangGraphRuntime
    （ADR 0004 决策 10）。这里允许的改动只有修 bug 和跟随共用函数重构。
    """

    def __init__(
        self,
        model: Model,
        tools: Mapping[str, ToolExecutor],
        limits: RuntimeLimits = RuntimeLimits(),
    ) -> None:
        """保存模型、工具执行器和循环预算。

        tools 只包含「允许执行」的工具；model 能提出什么由 view.tools 决定，
        两边对不上会在 run 开始时直接报错，属于装配问题，不是运行时该兜的。
        _seq 用来生成 call_id，每次 run 都重置，保证一次执行内 id 稳定可读。
        """
        self._model = model
        self._tools = dict(tools)
        self._limits = limits
        self._seq = 0

    def run(self, step_input: StepInput, idempotency_key: str) -> StepResult:
        """执行一步，返回恰好一个 StepResult。

        当前工具只读，idempotency_key 不参与逻辑；它属于外层契约
        （ADR 0003），保留参数是为了让 LangGraph 实现和写工具接入时
        不需要再改接口。

        循环里处理三种模型输出：
        - tool_call：执行后把记录喂回模型，继续下一轮；
        - ask_user：结构校验通过就渲染成追问并返回 need_input_fix；
        - draft：逐个字段校验出处，通过就返回 succeeded，否则打回重选。

        两条预算各自独立：max_model_turns 限制总轮数，max_verify_retries
        限制「校验失败」的次数。解析失败不占校验预算，因为它不是模型在赌，
        只是格式坏了。任何预算用尽都返回 need_human，而不是抛异常——
        上游只认动作，不认异常。
        """
        tool_specs = {tool.name: tool for tool in step_input.view.tools}
        require_read_only_tools(tool_specs)
        missing = tool_specs.keys() - self._tools.keys()
        if missing:
            # 白名单里有工具但没实现，是装配错误，不是运行时该处理的情况
            raise RuntimeError(f"以下工具没有执行器：{sorted(missing)}")

        self._seq = 0
        invocations: dict[str, ToolInvocation] = {}
        feedback: str | None = None
        verify_failures = 0

        for _ in range(self._limits.max_model_turns):
            try:
                output = self._model.respond(step_input.view, invocations, feedback)
            except ModelError as exc:
                # 输出无法解析：打回给模型重选，不消耗校验重试次数
                feedback = f"上一步输出无法解析：{exc}"
                continue

            if isinstance(output, ToolCallRequest):
                self._seq += 1
                invocation = execute_tool_call(
                    output, f"c-{self._seq}", tool_specs, self._tools
                )
                invocations[invocation.call_id] = invocation
                feedback = None if invocation.ok else f"工具调用失败：{invocation.error}"
                continue

            if isinstance(output, AskUser):
                ask_problems = validate_ask(
                    output,
                    invocations,
                    set(step_input.view.output_schema),
                )
                if ask_problems:
                    verify_failures += 1
                    feedback = "这次追问不成立：\n" + "\n".join(ask_problems)
                    if verify_failures > self._limits.max_verify_retries:
                        return need_human("invalid_ask")
                    continue

                # 句子由代码渲染，事实槽位只来自工具结果；
                # 证据取「本次执行的全部调用」和「追问直接引用的调用」的并集，
                # 让事后审计能还原它当时看到了什么。
                evidence = tuple(
                    sorted(set(invocations) | set(referenced_calls(output)))
                )
                return StepResult(
                    action=StepAction.NEED_INPUT_FIX,
                    may_have_committed=False,
                    cause="model_asked_user",
                    evidence=evidence,
                    payload=render_question(output, invocations),
                )

            problems = verify_draft(output, invocations)
            if not problems:
                return StepResult(
                    action=StepAction.SUCCEEDED,
                    # 本切片的步骤只读（查资产、生成草稿），成功也没有副作用。
                    # 写步骤的提交语义（建单）留到切片 2。
                    may_have_committed=False,
                    evidence=tuple(sorted({ref.call_id for ref in output.fields.values()})),
                    payload=output,
                )

            verify_failures += 1
            feedback = "以下字段无法追溯到工具结果，请重新选择出处：\n" + "\n".join(problems)
            if verify_failures > self._limits.max_verify_retries:
                # 每个终止路径自己说明原因，不靠一个被反复覆盖的变量推断
                return need_human("verification_failed")

        return need_human("turn_budget_exhausted")
