"""流程实例的存储。

先定义接口，内存实现和 Postgres 实现都挂在它后面：
流程语义的测试不依赖数据库，而同一套用例可以原样跑两遍。

并发控制用乐观锁而不是悲观锁：`save` 带期望版本号，版本对不上就拒绝。
这样两个事件同时到达时，后到的那个自然失败，不会把同一步走两遍。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from switchboard.workflow.engine import Instance, InstanceStatus


class StoreError(RuntimeError):
    pass


class DuplicateInstance(StoreError):
    pass


class InstanceNotFound(StoreError):
    pass


class ConcurrentUpdate(StoreError):
    """期望版本号已过期，说明有别人先推进了。调用方应重新加载而不是重试写入。"""


class InstanceStore(Protocol):
    def create(self, instance: Instance) -> Instance:
        """落一个新实例，返回带初始版本号的那个。"""
        ...

    def load(self, instance_id: str) -> Instance:
        """按 id 取实例；不存在时抛 InstanceNotFound。

        不返回 None：调用方几乎总是立刻要用这个实例，返回 None 只会把
        「没找到」推迟成下一次属性访问的 AttributeError，丢掉真正的错误信息。
        """
        ...

    def save(self, instance: Instance, expected_version: int) -> Instance:
        """版本号匹配才写入。返回写入后的实例（row_version 已加一）。"""
        ...

    def list_active(self) -> list[Instance]:
        """列出所有未结束的实例（running 或 suspended）。

        崩溃恢复只能从这里开始找：completed/failed 的实例不会再有推进，
        扫全表既浪费又容易在恢复逻辑里误碰已经结束的流程。
        """
        ...


class InMemoryInstanceStore:
    """单进程实现。测试和本地跑流程用，不能用于多进程部署。"""

    def __init__(self) -> None:
        """创建一个空的实例表，key 是 instance_id。"""
        self._rows: dict[str, Instance] = {}

    def create(self, instance: Instance) -> Instance:
        """插入新实例，把初始 row_version 记为 1。

        用版本号而不是时间戳做并发控制：时间戳可能重复，版本号严格递增，
        save 时只要比对是否相等就能判断有没有人先改过。
        同一个 id 重复创建直接拒绝，避免两次 start 产生两个并行的实例。
        """
        if instance.id in self._rows:
            raise DuplicateInstance(f"实例已存在：{instance.id}")
        stored = replace(instance, row_version=1)
        self._rows[instance.id] = stored
        return stored

    def load(self, instance_id: str) -> Instance:
        """读实例；不存在时抛 InstanceNotFound。

        直接返回内存里的对象。Instance 是 frozen 的，调用方无法原地修改，
        只能通过 replace 产生新值再 save，所以不需要额外拷贝。
        """
        try:
            return self._rows[instance_id]
        except KeyError:
            raise InstanceNotFound(f"实例不存在：{instance_id}") from None

    def save(self, instance: Instance, expected_version: int) -> Instance:
        """带乐观锁的写入。

        expected_version 是调用方读取时的版本。和当前版本不一致，说明
        另一个执行者已经推进过，本次写入放弃并抛 ConcurrentUpdate。
        成功时返回新实例，row_version 加一。
        """
        current = self.load(instance.id)
        if current.row_version != expected_version:
            raise ConcurrentUpdate(
                f"实例 {instance.id} 期望版本 {expected_version}，"
                f"实际版本 {current.row_version}"
            )
        stored = replace(instance, row_version=expected_version + 1)
        self._rows[instance.id] = stored
        return stored

    def list_active(self) -> list[Instance]:
        """按创建顺序返回未结束的实例，供启动时的崩溃恢复扫描。"""
        return [
            instance
            for instance in self._rows.values()
            if instance.status in (InstanceStatus.RUNNING, InstanceStatus.SUSPENDED)
        ]
