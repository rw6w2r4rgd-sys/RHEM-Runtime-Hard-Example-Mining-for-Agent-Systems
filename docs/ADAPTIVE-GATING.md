# RHEM 自适应门控 v0.2.0

## 结论

`v0.2.0` 新增可选的 `AdaptiveGatePolicy`，把固定次数门控扩展为按近期复发压力调档：

- 快速复发：门槛可从默认 `3` 降到 `2`，更快收敛；
- 长期安静：保持基准门槛 `3`；
- 高危、手动锁定或复查失败：锁到高档 `5`，达到阈值也不能自动落补丁，必须人工处理；
- 每次门控判断都写入 `events.jsonl`；
- 由门控触发并通过审计的知识或流程变更，会把 `gate_decision` 一起写进提案、补丁和补丁元数据，仍可回滚；
- 默认 `LearningEngine(store)` 继续使用固定 `3` 次门控，自适应策略需要显式启用。

这不是“证明自适应一定优于固定门控”。它只是提供一个可试点、可观测、可回退的参考实现。

## 为什么不直接替换默认门控

补丁说明明确要求“先试点再放量”。因此 `v0.2.0` 采用兼容策略：

```python
from rhem import AdaptiveGatePolicy, LearningEngine, RhemStore

store = RhemStore("./rhem_store")
engine = LearningEngine(
    store,
    gate=AdaptiveGatePolicy(),
)
```

未显式传入 `AdaptiveGatePolicy` 时，行为与此前一致：

```python
engine = LearningEngine(store)  # GatePolicy(min_occurrences=3)
```

这样可以在单个现场先观察，不会因为升级代码就自动改变所有部署的门控行为。

## 输入信号

门控只读取两类信号。

### 1. 复发压力

`RhemStore` 为每个 `family` 和 `cluster` 保存：

- `occurrence_times`：最近最多 200 次发生的 UTC 时间；
- `first_seen` / `last_seen`；
- `occurrences`：独立证据数；
- `sources`：来源集合。

升级前已经存在的旧记录没有真实 `occurrence_times`，`v0.2.0` 不会根据
`first_seen` / `last_seen` 反推历史分布；这些记录从升级后的新事件开始积累时间信号。
固定门控仍可直接处理旧记录。

`AdaptiveGatePolicy` 默认使用：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `min_occurrences` | `3` | 常规基准门槛 |
| `fast_min_occurrences` | `2` | 快速复发时可降到的门槛 |
| `high_risk_min_occurrences` | `5` | 高危或复查失败时的门槛 |
| `recurrence_window_days` | `7.0` | 近期复发观察窗口 |
| `fast_recent_occurrences` | `2` | 达到快速档所需的近期次数 |
| `decay_days` | `7.0` | 时间衰减尺度 |

内部灵敏度 `sensitivity` 在 `[0, 1]` 连续变化，最终门槛由它映射为可执行的整数：

```text
sensitivity 越高 -> 有效门槛越低
sensitivity 越低 -> 有效门槛回到基准
```

因此输出同时保留连续信号 `sensitivity` 和实际执行门槛 `effective_min_occurrences`，不把浮点评分伪装成现实中的确定性结论。

### 2. 风险锁

事件证据中可以提供：

```json
{
  "gate": {
    "high_risk": true,
    "manual_hold": true,
    "recheck_failed": false
  }
}
```

说明：

| 字段 | 效果 |
|---|---|
| `high_risk` | 锁高档，达到阈值后仍需人工 |
| `manual_hold` | 人工手动锁高档 |
| `recheck_failed` | 复扫或复查指出此前修复未生效，锁高档并要求重新归因 |

只写显式出现的字段会更新记录；再次传入 `false` 可以清除对应锁，避免锁永久无法退出。
`GateDecision.lock_reason` 会明确记录命中的风险锁；风险锁不会篡改
`sensitivity`，因此复发压力和风险处置在审计中保持分离。

## 门控判断结果

`GateDecision` 会随 `engine.ingest()` 返回：

```json
{
  "allowed": true,
  "mode": "adaptive",
  "reason": "fast_recurrence",
  "effective_min_occurrences": 2,
  "effective_min_sources": 1,
  "occurrences": 2,
  "sources": 2,
  "sensitivity": 1.0,
  "recurrence_per_day": 24.0,
  "recent_occurrences": 2,
  "requires_human": false,
  "lock_reason": null,
  "evaluated_at": "2026-09-10T01:00:00Z"
}
```

`allowed` 只表示是否跨过本轮门槛，不表示变更内容一定正确。

## 人工接管

对于识别错误和流程错误：

- `requires_human=False` 且门控通过：走原有自动补丁；
- `requires_human=True` 且门控通过：返回 `needs_human_gate`，不自动落补丁；
- 状态记为 `needs_human`。

对于规则缺失和知识缺口：

- 原本就必须人工批准；
- 门控通过后仍生成提案；
- `gate_decision` 写入提案，批准后跟随补丁归档。

## 补丁与审计

每次 `ingest()` 都会记录：

```text
events.jsonl
  kind=gate_evaluated
  record_id=<family 或 cluster_key>
  decision=<GateDecision>
```

跨过门控并产生变更时：

- 自动补丁的 `patch.gate_decision` 保存本轮判断；
- `patch_meta[patch_id].gate_decision` 保存同一份摘要；
- 人工批准提案时，提案保存的 `gate_decision` 会写入最终补丁；
- 回滚仍由原有补丁治理完成。

门控判断本身是派生决策，不是永久写入的护栏域配置。真正改变长期记忆或流程配置的动作仍必须生成补丁。

## 为什么不是“带病加速”

自适应门控只调节“多快进入处理流程”，不会改写：

- 对错标准；
- 护栏域；
- 模型权重；
- 系统提示词；
- 业务规则的真实性判断。

高危、手动锁定和复查失败会覆盖快速复发信号，强制高档和人工复核。门控更敏感，不等于降低正确性约束。

## 使用示例

```python
from rhem import AdaptiveGatePolicy, ErrorCategory, Incident, LearningEngine, RhemStore

store = RhemStore("./rhem_store")
engine = LearningEngine(
    store,
    gate=AdaptiveGatePolicy(),
)

outcome = engine.ingest(Incident(
    category=ErrorCategory.RECOGNITION,
    family="slot:city:x",
    message="城市槽位再次误判",
    source="task:order-201",
    evidence={
        "alias": "X",
        "canonical": "x",
    },
))

print(outcome["gate_decision"])
```

## 已验证与未验证

当前测试验证：

- 一小时内快速复发时，门槛从 `3` 降到 `2` 并自动应用；
- 长时间后再复发时，保持基准门槛 `3`；
- 高危错误锁到 `5`，达到后转 `needs_human_gate`；
- `recheck_failed` 强制人工重新归因；
- 规则提案和最终补丁保留 `gate_decision`；
- `gate_evaluated` 审计事件可读取；
- `Incident` 的 ID 与时间戳可完整往返序列化；
- 原固定门控测试保持通过。

当前不声称：

- 自适应策略已经在真实生产长尾上优于固定门控；
- 默认参数适合所有业务；
- 连续灵敏度已经完成概率校准；
- `2/3/5` 门槛经过统计最优性证明；
- 自动门控能够替代回归测试、评估集或人工复核。

`v0.2.0` 提供的是可试点的运行机制和审计基础，不是效果结论。