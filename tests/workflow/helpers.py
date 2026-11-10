"""测试辅助，不属于生产代码。"""

from __future__ import annotations


def step_next(node: str, *, success: str, input_fix: str = "manual") -> dict[str, str]:
    """可执行节点的边。六个结果一个都不能少，加载器会检查。"""
    return {
        "succeeded": success,
        "succeeded_needs_review": success,
        "need_input_fix": input_fix,
        "retryable": node,
        "need_human": "manual",
        "retry_exhausted": "manual",
    }
