# Changelog

## [0.3.0] - 2026-09-12

### Added

- 可选 `HardExampleDistiller`，支持并库前 `high` / `medium` / `low` 分馏；
- 单来源低价值样本拦截，跨来源复发样本放行；
- 库内命中数、场景覆盖、累计失败与连续失败反馈账本；
- `record_feedback()` 与 `rescan(record_feedback=True)` 反馈回填；
- `mark_superseded()` 显式替代账本，支持被新规则覆盖后的降权；
- 可审计的库内回炼计划，以及 `core`、`downweighted`、`cull` 三类结果；
- 蒸馏专用补丁动作、before/after 快照、回滚恢复和 `distillation_plan` 元数据。

### Changed

- 别名、运行规则和词条记录自动补齐 `distillation` 质量账本；
- 运行规则降权后会设置 `enabled=False`；
- 别名和词条降权后，默认复扫不再把它们视为有效修复；
- 包版本更新为 `0.3.0`。

### Compatibility

- 默认 `LearningEngine(store)` 不启用蒸馏，原门控与并库行为保持不变；
- 蒸馏需要显式传入 `distiller=HardExampleDistiller()`；
- `rescan()` 默认不写反馈，仍保持旧行为。

## [0.2.0] - 2026-09-12

### Added

- 可选 `AdaptiveGatePolicy`，基于近期复发压力动态调整门控门槛；
- 连续 `sensitivity` 信号与实际 `effective_min_occurrences` 分离输出；
- 快速复发、长期安静、高危锁定和复查失败的人工接管路径；
- `GateDecision` 审计事件，以及提案、补丁、补丁元数据中的 `gate_decision`；
- `Incident` 的 ID 和时间戳完整往返序列化；
- 自适应门控文档、演示和单元测试。

### Changed

- `RhemStore` 在 `family` 与 `cluster` 中保存 `occurrence_times` 和 `gate_flags`；
- 门控判断统一通过 `decide()` 返回完整结果，`reached()` 保留为兼容入口；
- 包版本更新为 `0.2.0`。

### Compatibility

- 默认 `LearningEngine(store)` 仍使用固定 `3` 次门控；
- 自适应策略需要显式传入 `gate=AdaptiveGatePolicy()`；
- 原有知识、流程、规则、图探针、补丁和回滚接口保持兼容。

## [0.1.2] - 2026-09-10

### Added

- BFS/DFS 双阶段图探针；
- 图簇独立证据去重与门控；
- 循环、深度截断、低置信度边的人工接管；
- 图簇状态、提案、补丁和回滚联动。