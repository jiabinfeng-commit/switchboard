"""测试共用夹具。流程定义本身在 switchboard/workflows/ 下，测试不另抄一份。"""

from __future__ import annotations

import copy

import pytest

from switchboard.workflow.definition import load_definition
from switchboard.workflows.fault_report import FAULT_REPORT


@pytest.fixture
def flow_data() -> dict:
    """返回流程定义的深拷贝，供用例改成各种非法形状。

    每个用例拿到的都是独立副本，修改不会污染其他用例。
    """
    return copy.deepcopy(FAULT_REPORT)


@pytest.fixture
def definition():
    """加载一份合法的报障受理流程定义，供推进相关的用例使用。"""
    return load_definition(copy.deepcopy(FAULT_REPORT))
