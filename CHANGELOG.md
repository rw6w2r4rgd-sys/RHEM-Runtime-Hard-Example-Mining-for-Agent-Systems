# Changelog

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