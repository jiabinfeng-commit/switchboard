from __future__ import annotations

import pytest

from switchboard.workflow.definition import (
    DefinitionError,
    NodeType,
    load_definition,
    parse_duration,
)

STEP_NODE = "draft"


def load(data):
    """加载流程定义的简写，让各用例把注意力放在被改坏的字段上。"""
    return load_definition(data)


def test_valid_flow_loads(flow_data):
    definition = load(flow_data)
    assert definition.name == "fault_report"
    assert definition.start == "draft"
    assert definition.node("confirm").type is NodeType.WAIT_USER
    assert definition.node("track").timeout.days == 30


def test_missing_step_outcome_rejected(flow_data):
    del flow_data["nodes"][STEP_NODE]["next"]["need_human"]
    with pytest.raises(DefinitionError, match="need_human"):
        load(flow_data)


def test_retry_exhausted_is_required(flow_data):
    del flow_data["nodes"][STEP_NODE]["next"]["retry_exhausted"]
    with pytest.raises(DefinitionError, match="retry_exhausted"):
        load(flow_data)


def test_unknown_outcome_rejected(flow_data):
    flow_data["nodes"][STEP_NODE]["next"]["something_else"] = "end"
    with pytest.raises(DefinitionError, match="不认识的结果"):
        load(flow_data)


def test_unknown_target_rejected(flow_data):
    flow_data["nodes"]["confirm"]["next"]["confirmed"] = "nowhere"
    with pytest.raises(DefinitionError, match="不存在的节点"):
        load(flow_data)


def test_wait_without_timeout_rejected(flow_data):
    del flow_data["nodes"]["confirm"]["timeout"]
    with pytest.raises(DefinitionError, match="必须配置 timeout"):
        load(flow_data)


def test_wait_without_timeout_edge_rejected(flow_data):
    del flow_data["nodes"]["confirm"]["next"]["timeout"]
    with pytest.raises(DefinitionError, match="timeout 的去向"):
        load(flow_data)


def test_wait_with_only_timeout_edge_rejected(flow_data):
    flow_data["nodes"]["confirm"]["next"] = {"timeout": "end"}
    with pytest.raises(DefinitionError, match="只有 timeout"):
        load(flow_data)


def test_timer_next_must_be_exactly_fired(flow_data):
    flow_data["nodes"]["hold"] = {
        "type": "wait_timer",
        "timeout": "10m",
        "next": {"fired": "end", "timeout": "end"},
    }
    flow_data["nodes"]["confirm"]["next"]["confirmed"] = "hold"
    with pytest.raises(DefinitionError, match="只能是 fired"):
        load(flow_data)


def test_unreachable_node_rejected(flow_data):
    flow_data["nodes"]["orphan"] = {
        "type": "wait_user",
        "timeout": "1h",
        "next": {"answered": "end", "timeout": "end"},
    }
    with pytest.raises(DefinitionError, match="走不到"):
        load(flow_data)


def test_unknown_field_rejected(flow_data):
    flow_data["nodes"]["confirm"]["time_out"] = "24h"
    with pytest.raises(DefinitionError, match="不认识的字段"):
        load(flow_data)


def test_end_must_not_have_next(flow_data):
    flow_data["nodes"]["end"]["next"] = {"closed": "end"}
    with pytest.raises(DefinitionError, match="不能有 next"):
        load(flow_data)


def test_agent_without_step_rejected(flow_data):
    del flow_data["nodes"]["draft"]["step"]
    with pytest.raises(DefinitionError, match="缺少 step"):
        load(flow_data)


def test_bad_max_attempts_rejected(flow_data):
    flow_data["nodes"]["draft"]["max_attempts"] = 0
    with pytest.raises(DefinitionError, match="max_attempts"):
        load(flow_data)


def test_start_must_exist(flow_data):
    flow_data["start"] = "unknown"
    with pytest.raises(DefinitionError, match="start"):
        load(flow_data)


@pytest.mark.parametrize(
    ("value", "expected_seconds"),
    [("30s", 30), ("15m", 900), ("24h", 86400), ("30d", 2592000)],
)
def test_parse_duration(value, expected_seconds):
    assert parse_duration(value).total_seconds() == expected_seconds


@pytest.mark.parametrize("value", ["24", "24H", "1.5h", "-1h", "1w"])
def test_parse_duration_rejects_bad_format(value):
    with pytest.raises(DefinitionError):
        parse_duration(value)
