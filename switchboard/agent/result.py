"""单步 Agent 的输出契约。

上游（流程引擎）只根据 StepResult 就能决定下一步做什么，不需要知道
下游是 GLPI、检索还是别的工具。所以这里只暴露「动作」，不暴露错误码。

两条核心约束：

1. 动作是唯一的分支依据；cause 只用于人工处置和统计。
2. 在结果不确定时，may_have_committed 取最坏情况（True），
   让所有消费方自动保守——宁可多转一次人工，也不能重复建单。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class StepAction(StrEnum):
    """一个步骤对上游交回的五类结果。

    这五类是按「上游下一步做什么」划分的，不是按失败原因划分的。
    新增错误码时只应该扩充适配层的映射，不应该扩充这个枚举。
    """

    # 输入不合法，外部请求根本没发出去。上游应回到对话追问或修正参数。
    NEED_INPUT_FIX = "need_input_fix"

    # 失败，且确定没有副作用（请求没发出，或明确被拒且未落库）。
    # 只有满足这个前提的结果才允许重试，由下面的不变量强制保证。
    RETRYABLE = "retryable"

    # 需要人工介入。两种情况都归这里：
    #   - 请求没发出去但自动处理不了（如权限不足）
    #   - 请求已发出但结果未知（如超时），是否已建单无法确定
    # 两者的区别由 may_have_committed 表达，由人工决定怎么核对。
    NEED_HUMAN = "need_human"

    # 成功，且结果与预期一致（如 GLPI 算出的优先级等于我们提取的）。
    SUCCEEDED = "succeeded"

    # 成功，但结果与预期不一致（如 GLPI 按规则算出了不同的优先级）。
    # 不能当成纯成功吞掉，也不能当失败重试——副作用已经发生。
    SUCCEEDED_NEEDS_REVIEW = "succeeded_needs_review"


# 每个动作对 may_have_committed 的硬性要求。
# None 表示该动作不约束这个值。
#
# 这里只放「放过去会出事」的组合，不放「正常情况应该如此」的组合：
#   RETRYABLE 必须未提交 —— 否则重试会重复建单
#   NEED_INPUT_FIX 必须未提交 —— 输入都没过校验，不可能已经发出请求
#   NEED_HUMAN 两种都可能 —— 发出前被拒（403）或发出后结果未知（超时）
#   SUCCEEDED 两种都可能 —— 只读步骤（查资产）成功也没有副作用，
#                            写步骤（建单）成功则一定有
_REQUIRED_COMMITTED: dict[StepAction, bool | None] = {
    StepAction.NEED_INPUT_FIX: False,
    StepAction.RETRYABLE: False,
    StepAction.NEED_HUMAN: None,
    StepAction.SUCCEEDED: None,
    StepAction.SUCCEEDED_NEEDS_REVIEW: None,
}


@dataclass(frozen=True)
class StepResult:
    """一步执行的结果。

    frozen=True：结果一旦构造就是事实，不允许中途修改。
    这样做是为了让「记录下来的结果」和「上游看到的结果」永远是同一个东西。
    """

    action: StepAction

    # 副作用是否「可能已经发生」。注意是可能性，不是事实：
    # 请求已发出但结果未知时取 True，因为重试的风险取决于最坏情况。
    # 未发出、明确被拒、确定建成这三种情况都是可观测的，按事实取。
    may_have_committed: bool

    # 失败原因，给人看、给统计用，不参与任何分支判断。
    # 展示层按 action 选文案档位，原因只作为文案里的细节附加。
    cause: str | None = None

    # 模型产出的值来自哪些工具返回项。校验器用它来判断
    # 模型是「从证据里选的」还是「自己编的」。
    # TODO: 目前是字符串元组，工具调用记录的形状待定（见 ADR 0001 未决 1）。
    evidence: tuple[str, ...] = ()

    # 动作对应的业务数据，例如建单成功后的工单 id、草稿内容。
    payload: Any = None

    def __post_init__(self) -> None:
        """构造时检查动作和副作用标记的组合是否违反不变量。

        例如 retryable 却声称可能已提交：放过去会让上游安全重试，最终重复建单。
        调用方可能会忘记检查，构造函数不会，所以把校验放在这里，
        让非法结果根本无法表示。
        """
        # 非法组合在构造时就抛错，而不是交给上游用 if 检查。
        # 调用方会忘记检查，而构造函数不会——这是「让非法状态无法表示」的做法。
        required = _REQUIRED_COMMITTED[self.action]
        if required is not None and self.may_have_committed != required:
            raise ValueError(
                f"{self.action} 要求 may_have_committed={required}，"
                f"实际为 {self.may_have_committed}"
            )
