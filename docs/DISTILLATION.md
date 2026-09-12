# RHEM 难例蒸馏 v0.3.0

## 结论

`v0.3.0` 新增可选的两道难例蒸馏工序：

1. **并库前分馏**：门控通过后，再按复发压力、来源覆盖和风险标记分为
   `high` / `medium` / `low`，只放行高价值样本进入并库动作；
2. **库内回炼**：按命中数、场景覆盖、失败反馈和闲置时间，把记录升为
   `core`、降为 `downweighted`，或通过补丁删除长期无价值记录。

蒸馏默认关闭。未显式传入 `HardExampleDistiller` 时，`v0.2.0` 的固定
门控、自适应门控和原并库行为保持不变。

```python
from rhem import HardExampleDistiller, LearningEngine, RhemStore

store = RhemStore("./rhem_store")
engine = LearningEngine(
    store,
    distiller=HardExampleDistiller(),
)
```

这不是“证明蒸馏能提升生产效果”。它提供的是一套可观测、可回滚、
可试点关闭的运行机制。

## 工序 1：并库前分馏

门控和蒸馏的职责不同：

- 门控回答“证据量够不够”；
- 蒸馏回答“这条证据值不值得进入并库动作”。

蒸馏不会绕过门控。即使风险锁把样本列为高优先级，也必须先达到门控阈值。
达到门控后，只有 `high` 样本继续走原有出口。

| 档位 | 判断条件 | 行为 |
|---|---|---|
| `high` | 高危锁；或跨来源复发达到阈值 | 允许进入原有并库或提案流程 |
| `medium` | 单一来源已反复出现，或来源覆盖开始增加 | 继续留在待学习区 |
| `low` | 孤例或尚未形成复发证据 | 不并库 |

判断只使用现有账本：

- `occurrences`：独立证据数量；
- `sources`：来源覆盖数量；
- `recent_occurrences`：近期复发数量；
- `sensitivity`：自适应门控计算出的近期压力；
- `high_risk` / `manual_hold` / `recheck_failed`：风险锁。

`distillation_decision` 会随 `engine.ingest()` 返回，并写入
`hard_examples` / `clusters` 的蒸馏字段与 `events.jsonl`。

## 工序 2：库内回炼

回炼只读取账本，不调用 LLM 判断语义，也不改护栏域。

### 反馈账本

每条别名、运行规则和词条记录都保存：

```json
{
  "distillation": {
    "quality_tier": "standard",
    "status": "active",
    "hits": 0,
    "misses": 0,
    "consecutive_misses": 0,
    "scenario_sources": [],
    "last_hit_at": null,
    "last_miss_at": null,
    "last_feedback_at": null,
    "superseded_by": null,
    "superseded_at": null
  }
}
```

可通过两种方式写入反馈：

```python
store.record_feedback(
    "alias",
    "shanghai",
    "checkout",
    resolved=True,
)
```

或让复扫显式回填账本：

```python
engine.rescan(record_feedback=True)
```

标准 `rescan()` 默认不写反馈，保持旧行为兼容。

如果外部验证器确认某条记录已被新规则替代，可以显式写替代账本：

```python
store.mark_superseded("alias", "old-alias", "new-alias")
```

蒸馏器不自行推断语义覆盖关系；只有账本出现 `superseded_by` 时才按替代降权。

### 回炼规则

默认参数如下：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `core_min_hits` | `3` | 升为 `core` 所需命中数 |
| `core_min_scenarios` | `2` | 升为 `core` 所需场景覆盖数 |
| `stale_after_days` | `30` | 长期未命中后降权时间 |
| `cull_after_days` | `90` | 从未命中且长期闲置后的清除时间 |
| `conflict_misses` | `1` | 连续失败反馈达到几次后降权 |

执行逻辑：

- 多场景累计命中达到门槛：升为 `core`；
- 连续失败反馈达到门槛：降为 `downweighted`；
- 显式标记被新记录替代：降为 `downweighted`；
- 长期没有命中的普通记录：降为 `downweighted`；
- 从未命中且超过清除周期：生成删除动作；
- 后续成功反馈会把 `consecutive_misses` 清零，历史 `misses` 仍保留审计，
  因此降权记录可以重新升回 `core`。

## 手动或定时回炼

参考实现不内置调度器。可由人工、cron 或外部任务定期调用。

先看计划，不落补丁：

```python
plan = engine.redistill(now="2026-09-12T00:00:00Z")
print(plan["summary"])
```

确认后再落补丁：

```python
applied = engine.redistill(
    apply=True,
    actor="human:owner",
    now="2026-09-12T00:00:00Z",
)
```

`apply=True` 必须提供执行者。所有动作都进入原有补丁系统，保存
before/after 快照和 `distillation_plan`，仍可按原回滚顺序恢复。

## 实际影响范围

| 动作 | 实际效果 |
|---|---|
| `core` | 质量账本标记为 `core`；规则重新启用 |
| `downweighted` | 规则 `enabled=False`；别名和词条的默认复扫不再把它们视为有效修复 |
| `cull` | 通过 `delete_alias`、`delete_rule` 或 `remove_term` 删除，回滚可恢复 |

蒸馏不会修改：

- 对错标准和护栏域；
- 模型权重和系统提示词；
- 规则条件、规则动作或知识定义本身；
- `before` / `after` 之外的业务内容。

## 已验证与未验证

当前测试验证：

- 不传 `HardExampleDistiller` 时，旧行为保持不变；
- 单来源重复停留待学习区；
- 跨来源复发可以进入原有并库流程；
- 多场景命中可以把记录升为 `core`；
- 失败反馈可以降权，成功反馈可以清除连续失败状态；
- 长期未命中记录可以删除，并可回滚恢复；
- `rescan(record_feedback=True)` 可以回填命中账本；
- 显式 `superseded_by` 记录会被回炼计划降权；
- 回炼计划、补丁和回滚继续工作。

当前不声称：

- 蒸馏已经概率校准或统计最优；
- `30 / 90` 天阈值适合所有业务；
- `core` 记录一定长期正确；
- 自动回炼可以替代人工抽查、评估集或回归测试；
- 参考实现已经包含生产级定时调度和高并发事务。

蒸馏解决“质量是否值得留”，不负责“长期失活后如何退场并复出”。`v0.7.0` 的 [冬眠机制](HIBERNATION.md) 单独处理生命周期、唤醒和人工回收，二者可以由同一账本驱动，但职责保持分离。
