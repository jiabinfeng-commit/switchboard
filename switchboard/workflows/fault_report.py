"""报障受理流程定义。

对应 ADR 0002。可执行节点的六个结果必须全部声明：
五个 StepAction 加上引擎产生的 retry_exhausted，漏一条加载器会拒绝。

流程本身是数据，这里只是它的存放位置。
"""

from __future__ import annotations

FAULT_REPORT = {
    "name": "fault_report",
    "version": 1,
    "start": "draft",
    "nodes": {
        "draft": {
            "type": "agent",
            "step": "draft_ticket",
            "max_attempts": 2,
            "next": {
                "succeeded": "confirm",
                "succeeded_needs_review": "confirm",
                "need_input_fix": "ask",
                "retryable": "draft",
                "need_human": "manual",
                "retry_exhausted": "manual",
            },
        },
        "ask": {
            "type": "wait_user",
            "timeout": "24h",
            "next": {"answered": "draft", "timeout": "end"},
        },
        "confirm": {
            "type": "wait_user",
            "timeout": "24h",
            "next": {"confirmed": "create", "revised": "draft", "timeout": "end"},
        },
        "create": {
            "type": "tool",
            "tool": "create_ticket",
            "max_attempts": 2,
            "next": {
                "succeeded": "receipt",
                "succeeded_needs_review": "receipt",
                "need_input_fix": "manual",
                "retryable": "create",
                "need_human": "manual",
                "retry_exhausted": "manual",
            },
        },
        "receipt": {
            "type": "template",
            "template": "ticket_created",
            "max_attempts": 2,
            "next": {
                "succeeded": "track",
                "succeeded_needs_review": "track",
                "need_input_fix": "manual",
                "retryable": "receipt",
                "need_human": "manual",
                "retry_exhausted": "manual",
            },
        },
        "track": {
            "type": "wait_event",
            "event": "ticket.closed",
            "timeout": "30d",
            "next": {"closed": "end", "timeout": "end"},
        },
        "manual": {
            "type": "wait_human",
            "timeout": "48h",
            "next": {
                "existing": "track",
                "retry": "create",
                "abandoned": "end",
                "timeout": "end",
            },
        },
        "end": {"type": "end"},
    },
}
