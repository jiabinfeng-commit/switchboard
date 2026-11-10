from __future__ import annotations

import pytest

from switchboard.workflow.definition import load_definition
from switchboard.workflow.engine import InstanceStatus, start_instance
from switchboard.workflow.store import (
    ConcurrentUpdate,
    DuplicateInstance,
    InMemoryInstanceStore,
    InstanceNotFound,
)

DEFINITION = load_definition(
    {
        "name": "stub",
        "version": 1,
        "start": "end",
        "nodes": {"end": {"type": "end"}},
    }
)


def make_instance():
    """创建一个 id 固定为 i-1 的实例，供存储层的用例重复使用。"""
    return start_instance(DEFINITION, "i-1")


def test_create_bumps_initial_version():
    store = InMemoryInstanceStore()
    stored = store.create(make_instance())
    assert stored.row_version == 1
    assert stored.status is InstanceStatus.RUNNING


def test_duplicate_create_rejected():
    store = InMemoryInstanceStore()
    store.create(make_instance())
    with pytest.raises(DuplicateInstance):
        store.create(make_instance())


def test_load_missing_rejected():
    store = InMemoryInstanceStore()
    with pytest.raises(InstanceNotFound):
        store.load("nope")


def test_save_bumps_version():
    store = InMemoryInstanceStore()
    stored = store.create(make_instance())
    saved = store.save(stored, expected_version=stored.row_version)
    assert saved.row_version == 2
    assert store.load("i-1").row_version == 2


def test_stale_version_rejected():
    store = InMemoryInstanceStore()
    stored = store.create(make_instance())
    store.save(stored, expected_version=1)
    with pytest.raises(ConcurrentUpdate):
        store.save(stored, expected_version=1)
