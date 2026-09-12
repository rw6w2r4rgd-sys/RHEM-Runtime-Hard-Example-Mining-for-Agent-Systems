# Changelog

## [0.7.0] - 2026-09-12

### Added

- 新增默认关闭的 `HibernationManager`，支持观察期、低功耗冬眠、复发唤醒和人工回收候选；
- `set_hibernation_state` 补丁动作，以及 `hibernation_plan` 生命周期元数据；
- 规则冬眠时自动禁用；唤醒仅在冬眠曾自动禁用该规则时恢复，人工原本禁用的状态保持不变；别名和词条冬眠时退出默认复扫；
- 同类错误复发自动唤醒，以及 `wake_hibernated()` 人工唤醒；
- 回收候选只生成计划，必须通过 `recycle_hibernated()` 人工批准后才进入删除补丁；
- 漂移样本字段、冬眠专题文档与 7 项单元测试。

### Changed

- 白皮书第 7 章从五组件扩展为六组件，新增生命周期与退场定位；
- 会师论增加第三路军粮：失灵和漂移样本；
- README、CITATION、Zenodo 元数据、包版本和演示标题统一为 `v0.7.0`；
- 冬眠只调整记录生命周期，不改变业务对错、规则条件、知识定义或护栏域。
- 更新“先驱与绘图师”定位说明，明确判据转移、运行期范围与概念可融合、可超越。

### Compatibility

- 默认 `LearningEngine(store)` 行为与此前版本一致；
- 冬眠需要显式传入 `hibernation=HibernationManager()`；
- 自动冬眠不会删除记录，人工审批与人工唤醒不被冻结；
- 旧版本 DOI、阻尼、蒸馏和反哺接口保持兼容。

### Validation

- 54 项单元测试通过；
- 未完成生产 A/B、真实长尾生命周期阈值或高并发验证。

### DOI

- `v0.7.0` 尚未生成独立 Zenodo 版本 DOI；
- 版本归档前引用本版请使用概念 DOI `10.5281/zenodo.22681163`。

## [0.6.0] - 2026-09-12

### Added

- 新增默认关闭的 `DampingSuppressor`，支持死区、A-B-A 振荡检测、反悔率告警、冷却与到期解冻；
- `damping_state` 护栏域，以及 `set_damping_control`、`clear_damping_control` 两类可回滚补丁动作；
- 规则反悔时生成 `distill_rule` 降权动作，将规则降权并禁用；
- `damping_plan` 补丁元数据，保存指纹、原因、阻尼系数、冷却截止时间和证据摘要；
- 阻尼专题文档与 5 项单元测试。

### Changed

- 白皮书第 7 章从四组件扩展为五组件，新增稳压器定位；
- README、CITATION、Zenodo 元数据、包版本和演示标题统一为 `v0.6.0`；
- 阻尼只调整自动进化动作的时机和冷却时长，不改变业务对错、规则方向或护栏域。

### Compatibility

- 默认 `LearningEngine(store)` 行为与此前版本一致；
- 阻尼需要显式传入 `damping=DampingSuppressor()`；
- 冷却只冻结自动动作，人工审批不受影响；
- 旧版本 DOI 和既有补丁接口保持兼容。

### Validation

- 47 项单元测试通过；
- 未完成生产 A/B、真实长尾统计最优性或高并发验证。

### DOI

- `v0.6.0` 尚未生成独立 Zenodo 版本 DOI；
- 版本归档前引用本版请使用概念 DOI `10.5281/zenodo.22681163`。

## [0.5.1] - 2026-09-12

### Fixed

- 修正 `v0.5.0` Release 在版本元数据更新前发布，导致归档 ZIP 与 Zenodo 记录仍标识为 `0.4.0` 的问题；
- 将包版本、白皮书版本、CITATION、Zenodo 元数据和 CHANGELOG 统一为 `v0.5.1`。

### Compatibility

- 文档与元数据修正版本，运行行为没有变化；
- `v0.5.0` 的版本 DOI `10.5281/zenodo.22719697` 继续有效，保留为白皮书完整版的首次归档。

### DOI

- 版本 DOI：`10.5281/zenodo.22719758`；
- 概念 DOI：`10.5281/zenodo.22681163`。

## [0.5.0] - 2026-09-12

### Added

- 扩展 Agent 级难例挖掘白皮书，补充缘起、规则 Agent 与模型 Agent、Skill 化、自进化机制、四类难例、业内对照、已知局限与落地边界；
- 补充进化动力系统四组件、二阶进化闭环、共享护栏总纲和会师论；
- 补充可验证首发声明、对外口径，以及不冒充生产验证的边界说明；
- 修正白皮书中指向仓库专题文档的相对链接。

### Changed

- 包版本更新为 `0.5.0`；
- README、引用文件与 Zenodo 元数据更新为新版本信息。

### Compatibility

- 文档聚焦版本，运行行为没有变化；
- 现有四类难例、探针、门控、蒸馏、反哺、补丁与回滚接口保持兼容。

### DOI

- 版本 DOI：`10.5281/zenodo.22719697`；
- 概念 DOI：`10.5281/zenodo.22681163`。

## [0.4.0] - 2026-09-12

### Added

- 可选 `DistillateInducer`，只从 `core/high` 账本生成候选；
- 规则模板归纳：同一动作和多个条件形成可审计合并候选；
- 结构弱点候选：按 `structure_key` 或 family 根键汇总核心记录；
- 隐患预测候选：汇总高价值待处理难例的类别、来源和结构分布；
- `induction_findings` 账本，以及候选创建、接受、拒绝和补丁关联字段；
- 规则模板人工批准后通过 `upsert_rule` 补丁写入，结构和隐患候选只记录审查结论。

### Changed

- 别名、运行规则和词条记录可保留 `structure_key`，供后续结构归纳使用；
- 补丁元数据增加 `induction_plan`；
- `induction_findings` 纳入可编辑域，但候选不会绕过人工批准；
- 包版本更新为 `0.4.0`。

### Compatibility

- 默认 `LearningEngine(store)` 不启用反哺，原有行为保持不变；
- 反哺需要显式调用 `engine.induct()`；
- 未显式传入 `DistillateInducer` 时使用默认参数，但不会在 `ingest()` 中自动运行；
- 没有 `core/high` 证据时不生成候选。

### DOI

- 版本 DOI：`10.5281/zenodo.22719563`；
- 概念 DOI：`10.5281/zenodo.22681163`。

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

### DOI

- 版本 DOI：`10.5281/zenodo.22719509`；
- 概念 DOI：`10.5281/zenodo.22681163`。

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
