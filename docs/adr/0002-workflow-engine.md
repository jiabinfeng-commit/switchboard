# ADR 0002：业务流程编排（状态机、等待与持久化）

- 状态：草案（切片 2a、2b 已完成，2c 未开始）
- 日期：2026-11-09
- 关联：ADR 0001（单步 Agent 的输出契约）

## 背景

第一条流程「报障受理」的步骤已拆完，每一步都标注了它在等谁：

| # | 步骤 | 做完在等谁 |
|---|---|---|
| 1 | 接收员工消息 | 代码继续 |
| 2 | 查资产、生成草稿 | 代码继续 |
| 3 | 用户确认 | 用户 |
| 3a | 用户要求修改 | 回到 2 |
| 3b | 确认超时 | 定时器 |
| 4 | 提交建单 | 代码继续（等 GLPI 响应） |
| 4a | 建单失败且不可重试 | 人工 |
| 5 | 给员工回执 | 代码继续 |
| 6 | 跟踪 GLPI 状态 | 外部系统 |
| 7 | 工程师在 GLPI 关单 | —— 流程结束 |

两个结论决定了引擎的形状：

1. **关单不是我们的步骤。** 关单由工程师在 GLPI 里做（共识第 57 行：关单必须人工），
   我们只观察到「已关闭」这个状态变化。所以终止条件是外部事件，不是一次调用。
2. **真正需要「挂起」的只有四种等待**：用户、定时器、人工任务、外部系统。
   「等代码继续」不挂起，只是同一轮推进里的下一步。

## 决策

### 1. 流程定义是数据，加载时校验穷尽性

流程定义（JSON）由通用引擎解释，不写成 if/else。

```json
{
  "name": "fault_report",
  "version": 1,
  "start": "draft",
  "nodes": {
    "draft":   { "type": "agent", "step": "draft_ticket", "max_attempts": 2,
                 "next": { "succeeded": "confirm", "succeeded_needs_review": "confirm",
                           "need_input_fix": "ask", "need_human": "manual",
                           "retryable": "draft", "retry_exhausted": "manual" } },
    "ask":     { "type": "wait_user", "timeout": "24h",
                 "next": { "answered": "draft", "timeout": "end" } },
    "confirm": { "type": "wait_user", "timeout": "24h",
                 "next": { "confirmed": "create", "revised": "draft", "timeout": "end" } },
    "create":  { "type": "tool", "tool": "create_ticket", "max_attempts": 2,
                 "next": { "succeeded": "receipt", "succeeded_needs_review": "receipt",
                           "retryable": "create", "retry_exhausted": "manual",
                           "need_input_fix": "manual", "need_human": "manual" } },
    "receipt": { "type": "template", "template": "ticket_created", "max_attempts": 2,
                 "next": { "succeeded": "track", "succeeded_needs_review": "track",
                           "retryable": "receipt", "retry_exhausted": "manual",
                           "need_input_fix": "manual", "need_human": "manual" } },
    "track":   { "type": "wait_event", "event": "ticket.closed", "timeout": "30d",
                 "next": { "closed": "end", "timeout": "end" } },
    "manual":  { "type": "wait_human", "timeout": "48h",
                 "next": { "existing": "track", "retry": "create",
                           "abandoned": "end", "timeout": "end" } },
    "end":     { "type": "end" }
  }
}
```

加载器必须校验：

- `agent` / `tool` / `template` 节点的 `next` 覆盖全部 5 个 `StepAction`，外加
  引擎产生的 `retry_exhausted`。可执行节点都要返回 `StepResult`，模板发送也可能失败，
  所以模板节点不能只有一条 `sent` 边。
- `wait_*` 节点的 `next` 覆盖该节点的全部退出方式（含 `timeout`）。
- 所有目标节点存在；`end` 是唯一没有 `next` 的类型。
- 每个 `wait_*` 必须有 `timeout`（见决策 3）。

**为什么不用 if/else 或代码 DSL：** 流程要按版本回放、要能在不改代码的前提下新增流程、
要能被评测按名字选取。代码里写的分支无法当作数据回放。
**为什么先不上 Temporal：** 生产环境里 Temporal 是更正确的选择，它把版本兼容、
任务队列、长时间运行、崩溃恢复都解决了。这里自研的理由是项目目标本身包含「掌握编排」，
代价必须明确记下：我们会缺它已经解决的部分（尤其是流程定义升级与运行中实例的兼容），
所以引擎接口要留出替换空间——推进逻辑是纯函数，存储是接口。

### 2. 不是每一步都挂模型

| 节点类型 | 用模型 | 例子 |
|---|---|---|
| `agent` | 是 | 生成建单草稿、澄清追问 |
| `tool` | 否 | 提交建单 |
| `template` | 否 | 发送回执 |
| `wait_*` | 否 | —— |

建单和回执没有判断空间：建单参数来自已确认的草稿，回执是固定话术。
在这两处用模型只会引入不确定性和成本。这与 ADR 0001 第 7 条同源——
对员工说的话由模板生成，不由模型生成。

### 3. 等待 = 持久化挂起状态 + 事件唤醒，不阻塞

四种挂起，各有唤醒源和超时：

| 等待 | 持久化 | 谁唤醒 | 超时兜底 |
|---|---|---|---|
| 用户 | `status=suspended, reason=user` | 员工消息入站 | 24h |
| 定时器 | `wake_at` | 调度器扫描到期 | 自身即超时 |
| 人工 | `human_task` 记录 | 人工任务提交 | 48h |
| 外部 | 订阅（工单 id + 目标状态） | `EventSource` | 30d |

**每个挂起必须带超时。** 没有超时的挂起就是实例泄漏：员工不回、工程师处理完却忘了关单，
实例会永远挂在表里，而且没有任何地方会报错。超时到期走 `timeout` 分支，
由流程定义决定是结束还是升级。

**为什么不用阻塞等待（`await` / `sleep` / 轮询循环）：** 等待跨度是小时到天，
进程重启、部署都会丢失内存里的等待。把等待写成表里的一行，进程谁活着都能推进。

### 4. 分支只认 outcome，引擎不解释 cause

`agent` / `tool` 节点的 outcome 就是 `StepResult.action`（ADR 0001 的五个动作）；
`wait_*` 节点有自己固定的退出方式。流程定义只写「outcome → 下一节点」。

- `cause` 只进人工任务和事件日志，引擎不读。
- `retryable` 由引擎重试，次数上限写在节点上，用尽后产生 `retry_exhausted`，
  再由流程定义决定去哪儿（通常转人工）。
- `may_have_committed` 不参与分支：只有 `need_human` 会带着它进人工任务，
  由人决定是否重发（切片 2b：同一幂等键重发还是放弃）。

**为什么要把重试上限放在节点上：** 否则重试次数散落在各步骤实现里，
流程定义上看不出一个节点最多会跑几次，回放和估算都不可靠。

### 5. 用户回复的解释：只允许认领「确认」，其余一律回落

用户确认节点的回复需要分类（确认 / 要求修改）。这里不引入新分支：

- 只有能确定为「确认」的回复才进入 `confirmed`；
- 模棱两可、解释失败、提到新的设备信息，一律当作 `revised` 送回 `draft`。

即模型只能认领一个安全分支，不能发明新分支。这与 ADR 0001 的
「允许选择，禁止生成」同一个思路：危险动作的门槛是「确定」，不确定就走安全路径。

### 6. 实例与并发

- `workflow_instance`：`id, workflow_name, workflow_version, status, current_node,
  attempt, suspend_reason, wake_at, context, context_owners, row_version,
  created_at, updated_at`。
- 乐观锁：推进时 `UPDATE ... WHERE id = ? AND row_version = ?`，失败即放弃本次推进。
- **单写者**：同一实例同一时刻只允许一个推进者。两个事件同时到达时，后到的那个
  读到的 `row_version` 已变，自然放弃，不会重复走一步。
- 实例创建时锁定定义版本，运行中不迁移定义；定义升级只对新实例生效。
  运行中实例的迁移是另一件事，本切片不做。

### 7. 步骤之间的数据传递

`context` 是一个 JSON dict。步骤读它、返回一个 dict，引擎负责 merge。

每个 key 记录最后写入它的节点（`context_owners`）：

- **同一个节点重跑可以覆盖自己写过的 key。** 用户要求修改后回到 `draft`
  重新生成 `asset_id`，这是正常覆盖，不是冲突。
- **不同节点写同一个 key 报错。** 防止后一步把前一步的证据字段悄悄改掉。
- 初始上下文记为 `<start>` 写入，任何节点都不能覆盖。

写 ADR 时只写了「key 冲突时报错」，跑演示的第一条回环（确认 → 修改 → 回草稿）
就撞上了。这条规则的用意是拦住跨节点覆盖，不是禁止节点更新自己。

**为什么先这样做：** 步骤少，显式输入输出映射（`inputs: {param: "$.field"}`）
带来的收益还没出现，而它需要一套路径语法（ADR 0001 已经为证据写过一小套，
不想再造一套）。升级路径明确：当出现第一个「两个步骤往同一个 key 写不同意思」
的真实案例时再引入映射。

### 8. 事件源可替换

```python
class EventSource(Protocol):
    def poll(self) -> list[TicketEvent]: ...
```

- `FakeEventSource`：测试直接投喂事件。
- `GlpiEventSource`：轮询 GLPI 工单状态。
- GLPI 11 是否内置 webhook 尚未验证；可能要走插件。接口不变，实现后补。

## 切片划分

| 切片 | 内容 | 状态 |
|---|---|---|
| 2a | 流程定义 + 加载校验 + 纯推进函数 + 实例存储（内存版与 Postgres 同一接口） | 完成（Postgres 实现未写） |
| 2b | 步骤执行记录、幂等键、崩溃恢复、人工任务 | 完成，见 ADR 0003 |
| 2c | 定时器调度、事件日志与回放、事件源 | 未开始 |

2a 先做内存存储，是为了让流程语义的测试不依赖数据库；
Postgres 实现放在同一接口后面，测试用同一套用例跑两遍。
2b 同样先用内存实现把恢复语义测透，PG 的事务边界留到实现时补（ADR 0003）。

## 未决

1. 流程定义格式最终是 JSON、YAML 还是 Python 数据类（当前 JSON + 加载成数据类）。
2. 崩溃时步骤执行到一半的恢复语义——已由 ADR 0003 落定。
3. 幂等键在哪一层生成——已由 ADR 0003 落定：runner 写入执行记录时生成，
   执行器只消费。
4. 运行中实例的定义迁移。
5. 人工任务的处置结果由谁决定——已由 ADR 0003 落定：人工任务提交时带
   明确的 outcome，不是勾「已完成」。

## 实现

- `switchboard/workflow/definition.py`：加载与校验（穷尽性、超时、可达性）
- `switchboard/workflow/engine.py`：纯推进（`next_action` / `apply_result` / `apply_outcome`）
- `switchboard/workflow/store.py`：实例存储接口 + 内存实现（乐观锁）
- `switchboard/workflow/execution.py`：执行记录与幂等键（2b）
- `switchboard/workflow/human.py`：人工任务（2b）
- `switchboard/workflow/runner.py`：驱动循环、落库时机、崩溃恢复、人工任务
- `switchboard/workflows/fault_report.py`：报障受理定义（本 ADR 的 JSON 示例）
- `scripts/workflow_demo.py`：跑一遍看迁移、崩溃恢复和转人工
- 测试 `tests/workflow/`
