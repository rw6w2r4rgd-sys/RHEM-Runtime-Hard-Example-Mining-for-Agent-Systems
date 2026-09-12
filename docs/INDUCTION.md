# RHEM 蒸馏反哺 v0.4.0

## 结论

`v0.4.0` 新增可选的 `DistillateInducer`：

- 只读取已经达到 `core` 的记录，以及待处理的高价值 `high` 难例；
- 归纳三类候选：规则模板、结构弱点、隐患预测；
- 规则模板必须人工批准后，才通过 `upsert_rule` 补丁写入；
- 结构弱点和隐患预测只生成可回滚的人工复核结论，不直接改设计或探针；
- 归纳只使用账本字段，不调用 LLM，不自行推断语义覆盖关系。

它不是“自动变聪明”。它只把高纯账本里已经存在的重复、覆盖和聚集关系整理成候选，交由人决定是否采用。

## 为什么需要反哺

难例蒸馏回答的是：

> 哪些记录纯度更高，值得继续保留或提升？

蒸馏反哺继续问：

> 多条高纯记录放在一起后，能否看出重复动作、结构集中点或下一批风险？

前者减少低价值记忆，后者从高价值记忆中生成人工审查材料。两者都不替代评估集、回归测试或生产验证。

## 输入边界

### 规则模板

只使用：

- `distillation.quality_tier == "core"`；
- `distillation.status == "active"`；
- 同一个结构根因（显式 `structure_key` 或 family 根键）；
- 同一个 `action`；
- 至少两个不同的 `condition`。

生成的是一条候选规则，条件是原始条件的确定性 `OR` 组合。它可能过宽，所以不会自动写入，也不会绕过人工批准。

### 结构弱点

只使用：

- 已达到 `core` 的记录；
- 显式 `structure_key`，或从 `family` 推导出的前两段结构键；
- 至少三条记录集中在同一结构键。

结果只是“建议做设计复核”，例如观察日期槽位是否缺锚点。参考实现不自动改代码、不改模型、不改护栏。

### 隐患预测

只使用：

- `distillation_tier == "high"`；
- 状态仍为 `pending` 或 `needs_human`；
- 同一类别和结构键下的累计证据。

结果只是“建议人检查是否扩大探针或复查范围”。这里没有概率校准，`priority_score` 只是排序分，不是发生概率。

## 使用方式

```python
from rhem import DistillateInducer, HardExampleDistiller, LearningEngine

engine = LearningEngine(
    store,
    distiller=HardExampleDistiller(),
    inducer=DistillateInducer(),
)

plan = engine.induct(now="2026-09-12T00:00:00Z")
print(plan["summary"])
```

`induct()` 默认把候选保存到 `induction_findings` 账本，状态为 `proposed`。使用 `persist=False` 可以只做只读演练。

规则模板批准：

```python
finding = plan["findings"][0]
patch = engine.approve_induction(
    finding["finding_id"],
    approver="human:owner",
)
```

结构弱点和隐患预测批准后只记录审查结论：

```python
finding_id = next(
    item["finding_id"]
    for item in plan["findings"]
    if item["kind"] == "structural_weakness"
)
engine.approve_induction(finding_id, approver="human:owner")
```

拒绝候选：

```python
engine.reject_induction(
    finding_id,
    reason="条件边界不成立",
    approver="human:owner",
)
```

批准和拒绝都通过统一补丁系统记录，可回滚到 `proposed`。

## 防炼过头

参考实现采用以下硬边界：

1. 脏数据和高风险未处理样本不能作为规则模板输入；
2. `medium` / `low` 记录不能进入规则模板和结构弱点归纳；
3. 所有候选必须人工处理；
4. 规则模板只能写成普通运行规则，不能写护栏域；
5. 结构弱点和隐患预测没有自动执行动作；
6. 不引入 LLM 主观脑补，不做置信度伪装；
7. 所有状态变化保留补丁、审计事件和回滚路径。

## 已验证与未验证

当前测试验证：

- 没有 `core` 记录时不会产生候选；
- 两条 `core` 规则才能生成规则模板候选；
- 候选保存后不会自动改变运行库；
- 规则模板人工批准后才写入，并可回滚；
- 结构弱点只记录接受结论，不新增运行规则；
- 拒绝结论可回滚；
- 重复调用会复用相同的待处理候选；
- 高价值待处理难例可生成结构级隐患预测候选。

当前不声称：

- 蒸馏反哺已经提升真实生产准确率或泛化能力；
- `priority_score` 是概率或统计置信度；
- 结构弱点和隐患预测一定正确；
- 规则模板自动具备正确条件边界；
- 参考实现已经可以替代人工设计评审、探针规划或回归测试。

## 白皮书落点

- “欠泛化”不会被这一版彻底解决；本版只是提供了从高纯难例抽取候选的窄入口；
- Roadmap 中新增 `v0.4.0` 的蒸馏反哺试点；
- 只有取得真实评估集、2 至 3 周试点数据或可复现实验结果后，才允许提高效果声明。
