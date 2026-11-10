# ADR 0003：步骤执行记录、幂等键与崩溃恢复

- 状态：草案（切片 2b）
- 日期：2026-11-10
- 关联：ADR 0001（输出契约）、ADR 0002（流程编排）

## 背景

2a 的 runner 已经做到「可执行节点跑完立刻保存实例」，但没处理保存之前和
之后那两个窗口。进程可能在三个位置死掉：

| 崩溃点 | 实例状态 | 执行记录 | 后果 |
|---|---|---|---|
| `execute()` 执行中 | 停在原节点 | 没有记录 | 重跑一次，副作用可能已经发生 |
| 实例已保存、结果没落账 | 已到下一节点 | 没有记录 | 恢复时会被当作没执行过，重跑 |
| 实例保存后、记录收尾前 | 已到下一节点 | 有未收尾记录 | 同上，除非能识别 |

第一种只能靠下游幂等兜底；第二、三种是顺序问题，可以在引擎内部解决。

## 决策

### 1. 执行记录是崩溃恢复的唯一依据

`step_execution`：`key, instance_id, node, attempt, status, requested_at,
result, context_update, finished_at`。

状态三值：

- `started`：已经决定执行，结果没有落库。恢复时唯一需要特殊处理的状态。
- `completed`：结果和执行记录都落库。
- `recovered`：实例已经迁移，执行记录只是没来得及收尾。**恢复时不重跑。**

不用实例本身推断「这一步跑没跑过」：实例只保存当前节点，无法区分
「没开始」和「执行到一半」。执行记录是唯一能表达这个区别的地方。

### 2. 幂等键绑定一条执行记录，在 runner 里生成

生成时机：runner 决定要执行某节点、写入 `started` 记录时。
执行器从参数里拿到键，必须原样传给下游，**不能自己再生成**。

```
runner: key = uuid4()  ->  step_execution.begin(key=key)
        execute(node, instance, key)
        executor: backend.create_ticket(..., idempotency_key=key)
```

否决的两个方案：

- **执行器内部生成**：崩溃重跑时执行器会生成新键，下游看成第二次业务请求。
- **`instance_id + node + attempt` 拼键**：回环（确认 → 修改 → 回草稿）
  会把 attempt 重置为 1，第二轮回环和第一轮撞键，把两次不同的请求
  当成同一次重发。键的生命周期必须绑定「一次执行尝试」，而不是
  「某节点第几次 attempt」。

### 3. 先保存实例，再收尾执行记录

顺序不能反。分析：

- 先 `save(instance)`，再 `complete(row)`：
  崩在中间 → 实例已迁移、记录是 `started`。恢复时发现记录与实例的
  `(node, attempt)` 对不上，补记成 `recovered`，**不重跑**。
- 先 `complete(row)`，再 `save(instance)`：
  崩在中间 → 记录是 `completed`、实例还在原节点。恢复时没有未收尾记录，
  会当作没执行过，**重跑**。副作用已经发生，这就是重复建单。

代价：第一种顺序在崩溃窗口里会丢掉执行记录的 `result` 明细（结果只体现在
实例迁移上）。审计信息不完整，但副作用不会重复。这个取舍是刻意的。

### 4. 恢复动作按残留形态分派

| 残留 | 动作 |
|---|---|
| `running` + 未收尾记录且 `(node, attempt)` 与实例一致 | 用同一条记录的键重跑该节点 |
| `running` + 未收尾记录与实例不一致 | 补记 `recovered`，继续推进 |
| `suspended` + `reason=human` 且没有 open 任务 | 补建人工任务 |
| `suspended` + 其他 reason | 不动。唤醒只能来自对应的事件源或超时 |
| `completed` / `failed` | 不动 |

`recover_all()` 只扫描 `store.list_active()`（running + suspended），
不对已结束实例做任何事。

「`(node, attempt)` 一致才复用记录」这个判断依赖一个前提：实例不会在没有
收尾记录的情况下跨节点迁移多步。当前实现里收尾失败会立刻抛异常终止推进，
恢复又会在推进前先对齐记录，所以该前提成立。以后若引入「收尾失败继续推进」，
这条判断需要重新设计。

### 5. 引擎只保证「可识别的重发」，不保证下游安全

同键重跑是否安全，取决于下游：

- 支持幂等键：按键去重，返回第一次的结果。
- 不支持：适配层必须自己查重（例如按幂等键查 GLPI 工单），
  或者返回 `need_human`，把「是否重发」交给人。

引擎无法判断下游有没有副作用，也不试图猜。ADR 0001 的
`may_have_committed` 在这里仍然只是事实描述，不参与分支。

### 6. 人工任务是一条有状态记录，不是勾选框

`human_task`：`id, instance_id, node, cause, may_have_committed,
allowed_outcomes, status, created_at, deadline, submitted_outcome,
submitted_at`。

- 状态只有 `open -> submitted`，单向。重复提交会被存储层拒绝，
  并发下两个请求都通过实例检查时，后到的那个在这里失败。
- `allowed_outcomes` 来自流程定义里该节点的 `next` 去掉 `timeout`。
  人只能在已声明的边上选，不能发明新的处置结果。
- `cause` 和 `may_have_committed` 取自最近一次收尾的执行记录，
  不塞进流程上下文——它们只服务人工判断，进上下文会污染步骤间的数据契约。
- 提交前先校验实例仍挂在该人工节点上，避免唤醒一个已经超时结束的实例。
  校验和标记之间仍有窗口；内存实现单进程可接受，Postgres 实现要把
  两步放进同一事务。

## 内存实现与 Postgres 的差距

2b 先用内存实现把语义测透，PG 推迟。这带来两处已知差异，写 PG 时必须补上：

1. **原子性**：内存版做不到「实例保存 + 记录收尾」原子提交，只能靠
   顺序（先实例后记录）加恢复补偿。PG 应该把两次写放进同一事务，
   崩溃窗口直接消失，`recovered` 状态的触发概率降到接近零。
2. **提交人工任务与实例状态**：内存版先校验实例状态、再标记任务、
   再唤醒实例，三步之间都有窗口。崩在「任务已标记、实例未唤醒」之间时，
   实例仍是 suspended+human，恢复会建一条新任务，而人的选择留在旧任务的
   `submitted_outcome` 里。PG 应把三步放进同一事务，或恢复时识别
   「最近一条任务已提交但实例未迁移」并重放该 outcome。
3. **执行权认领**：存储层用「同一实例只能有一条未收尾记录」防止两个推进者
   同时执行一步；但恢复时两个进程可能同时读到同一条 `started` 记录并都去
   重跑。内存实现单进程无此问题，PG 需要在取记录时加行锁或
   `SELECT ... FOR UPDATE SKIP LOCKED` 认领，不能只靠唯一约束。

## 未决

1. 执行记录和人工任务的保留策略（按时间清理还是跟随实例归档）。
2. `context_update` 与 `StepResult.payload` 的序列化格式（PG 落 JSONB 时定）。
3. 下游不支持幂等键时的统一查重接口（属于 TicketBackend 契约）。
4. 过期人工任务的清理（2c 随定时器一起做）。
5. 跨实例的幂等（同一个员工消息重复入站如何处理）。

## 实现

- `switchboard/workflow/execution.py`：执行记录与存储
- `switchboard/workflow/human.py`：人工任务与存储
- `switchboard/workflow/runner.py`：执行记录接入、恢复入口、人工任务创建与提交
- `switchboard/workflow/store.py`：`list_active`
- 测试：`tests/workflow/test_execution.py`、`test_recovery.py`、`test_human_task.py`
- 演示：`scripts/workflow_demo.py`（崩溃恢复与转人工两条路径）
