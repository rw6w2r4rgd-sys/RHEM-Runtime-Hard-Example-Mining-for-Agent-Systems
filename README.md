# RHEM — Runtime Hard-Example Mining for Agent Systems

[![Version](https://img.shields.io/badge/version-0.5.1-blue.svg)](CHANGELOG.md)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22719758.svg)](https://doi.org/10.5281/zenodo.22719758)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

RHEM 是一套面向 Agent 系统的**运行期难例挖掘参考实现**。

它关注的不是“再训一次模型”，而是：当 Agent 在真实任务里犯了一次错，能不能在不重训、不发版的前提下，把这次错误变成下一次不会再犯的系统记忆。

本仓库刻意把边界写清楚：

- 这里公开的是方法论、定位说明和 MIT 参考实现；
- 不包含生产环境的防篡改、私有探针、内部部署脚本；
- 当前没有 benchmark、线上 A/B 或生产性能数据，因此不声称加速倍数、准确率提升或行业领先；
- 目前能证明的是：四类难例链路、BFS/DFS 图探针、固定/自适应门控、难例蒸馏、蒸馏反哺、人工批准、护栏、补丁、回滚和复扫，在参考实现与单元测试中可运行。

## 为什么需要 RHEM

训练阶段的难例挖掘回答的是：

> 哪些训练样本更难，值得让模型多学几遍？

运行期 Agent 面对的是另一类问题：

> 这次任务为什么会失败？错误属于识别、流程、规则还是知识？下次如何立刻避免？

RHEM 把“错误现场”当作可回收资产。错误不是只写进日志，而是带着来源、证据和处置路径进入待学习区；累计达到门控后还要经过可选蒸馏，才允许进入并库动作，避免一次误报直接污染长期记忆。

一句话概括：

> 难例是当下的守卫，也是未来的燃料。

## 与训练界难例挖掘的关系

RHEM 不否认训练界难例挖掘的先行价值。它是在既有“难样本值得挖”的实践上，继续把范围扩展到完整 Agent 运行期。

| 维度 | 训练界难例挖掘 | RHEM |
|---|---|---|
| 对象 | 模型训练样本 | 运行期全类型错误 |
| 时机 | 训练阶段 | 任务运行期 |
| 生效 | 重训、评估、发版 | 反射式并库或受控补丁 |
| 节奏 | 批量训练 | 逐单回流、累计门控 |
| 主要产物 | 更难样本集 | 别名、规则、词条、流程修复与审计记录 |

更具体的先后关系与定位，见 [先驱与绘图师定位说明](docs/PIONEER-AND-CARTOGRAPHER.md)。

## 为什么前期优先使用 RHEM

在运营前期，团队往往还没有足够的训练样本、评估集和训练发布闭环，但已经拥有真实错误、领域判断和现场修正信号。RHEM 先把这些已知痛点转成运行期可加载的别名、规则、流程配置或词条，让可扩展 Agent 先在真实运营中适配。

它主要解决“同类错误不再复发”；难例发掘则利用积累起来的难例提升模型泛化能力。两者互补：RHEM 管当下，难例发掘管未来。

详细边界、适用条件和两条路线的分工见 [适用阶段与分工](docs/APPLICABILITY-AND-STAGING.md)。

## RHEM 处理什么

参考实现把运行期难例分成四类，每一类走不同出口。

| 类别 | 典型错误 | 处置方式 | 是否需人工 |
|---|---|---|---|
| 识别错误 | 槽位串位、别名误判、意图混淆 | 写入别名库，后续识别即时命中 | 否 |
| 流程错误 | 死锁、无限重试、错误重试风暴 | 修改运行配置，做结构性修复，不进知识库 | 否 |
| 规则缺失 | 某类输入没有规则约束 | 决策器开“药方”，人工批准后写入规则库 | 是 |
| 知识缺口 | 未收录词、品名、SKU、术语 | 上报待归因，人工给出标准值后写入词条库 | 是 |

### 1. 识别错误

例：订单文本写的是“发往上海”，系统却把“上海”填进了发件城市。

前两次只进入待学习区并累计；第三次同类错误达到门控后，把：

```text
上海 -> shanghai
```

写入别名库。后续识别器读取该表即可使用，不需要重新训练模型。

### 2. 流程错误

例：调用 `fetch_orders` 时一直重试，最终形成类似死锁的长等待。

这类问题不该写进“知识库”，因为缺的不是知识，而是流程约束。参考实现会通过补丁修改运行配置，例如：

```json
{
  "max_retries": 2,
  "deadlock_detection_s": 3.0
}
```

结构性修复会留下补丁，但不会伪装成一条业务规则。

### 3. 规则缺失

例：收件人字段为空时，系统仍然猜测收件人并继续发货。

同类问题达到门控后，决策器生成一条待批准药方，例如：

```text
收件人缺失时不得自动猜测；暂停执行并询问用户
```

在人工批准之前，规则库保持为空。批准后规则才写入并生效。

### 4. 知识缺口

例：仓库单据出现未收录的 `ZL-9`，系统无法判断它代表什么。

RHEM 不替人猜标准答案，只上报：

```text
未识别词条：ZL-9
需要归因字段：canonical / definition / kind
```

人工给出：

```json
{
  "canonical": "ZL-9-BLK",
  "definition": "黑色 9 号重型支架",
  "kind": "product_sku"
}
```

之后才写入词条库。

## 双阶段图探针

当现场能提供错误图时，RHEM 在门控前先做两轮确定性遍历：

| 阶段 | 作用 | 不做什么 |
|---|---|---|
| BFS | 从错误出发，在有限深度内圈定影响范围和相关问题簇 | 不证明范围内节点都是根因 |
| DFS | 沿 `caused_by` / `depends_on` 追踪上游根因候选 | 不替代人工因果判断 |

两者共同生成 `cluster_key`。同一根因、同一会话里的多个症状只算一个独立证据；不同会话仍可分别累计。这样可以把“同根因的多个症状”和“真正来自不同现场的独立证据”区分开。

遇到上游循环、深度截断或低置信度边时，图分析返回 `needs_human_graph`，识别错误和流程错误也不会自动应用补丁。完整输入格式、去重规则与边界见 [双阶段图探针](docs/GRAPH-PROBE.md)。

## 运行机制

```text
现场错误或用户纠错
        |
        v
 Incident + 来源 + 证据 + 可选错误图
        |
        v
 前置图探针（有图时）
   BFS：有限范围
   DFS：上游根因候选
        |
        v
 生成 cluster_key；无图时退回 family
        |
        v
 待学习区
        |
        v
 图簇优先、family 兜底的独立证据检查
 可选自适应门控：常规 3、快速复发 2、高危 5
        |
        +---- 未达门控 ----------> 继续等待，不改库
        |
        v
     门控通过
        |
        +--> 可选蒸馏：high 放行 / medium, low 继续等待
        |
        +--> 识别错误 -> 别名补丁 --------> 即时生效
        +--> 流程错误 -> 结构性配置补丁 --> 即时生效
        +--> 规则缺失 -> 药方 -> 人工批准 -> 规则补丁
        +--> 知识缺口 -> 上报 -> 人工归因 -> 词条补丁
        |
        v
 所有变更统一进入补丁治理
        |
        +--> before/after 快照
        +--> 顺序回滚
        +--> 历史难例复扫
        +--> 可选库内回炼：core / downweighted / cull
        +--> 可选反哺：core/high 归纳候选 -> 人工批准
```

### 门控

门控的作用不是判断“这条反馈一定是真的”，而是避免单次误报直接进入长期记忆。

默认请求仍使用固定 `3` 次门控，保持旧行为兼容。`v0.2.0` 新增可显式启用的 `AdaptiveGatePolicy`：

- 有图时优先按 `cluster_key` 统计独立证据，无图时退回同一 `family`；
- 同一根因、同一会话的多个症状只算一个证据，不同会话可分别累计；
- 快速复发时可把门槛降到 `2`，长期安静时保持基准 `3`；
- 高危、手动锁定或复查失败时锁到 `5`，达到阈值后仍需人工处理；
- 连续 `sensitivity` 信号与实际 `effective_min_occurrences` 分开输出；
- 每次门控判断写入 `events.jsonl`，实际变更随补丁保存 `gate_decision`；
- 图结构不明确时，不自动应用识别或流程补丁，转人工复核。

```python
from rhem import AdaptiveGatePolicy, LearningEngine

engine = LearningEngine(
    store,
    gate=AdaptiveGatePolicy(),
)
```

自适应策略仍只调节“收得快慢”，不改变对错标准、护栏域或知识真实性。默认参数和试点流程见 [自适应门控](docs/ADAPTIVE-GATING.md)。

### 难例蒸馏

`v0.3.0` 新增可选的 `HardExampleDistiller`，默认关闭。显式启用后：

- 并库前按复发压力、跨来源证据和风险锁分为 `high` / `medium` / `low`；
- 只有高价值样本在门控通过后继续进入原有并库或提案流程；
- 库内按命中数、场景覆盖、连续失败反馈和闲置时间做回炼；
- 多场景共证可升为 `core`，长期无价值记录可降权或删除；
- 回炼先给只读计划，再通过补丁应用，仍可回滚。

```python
from rhem import HardExampleDistiller, LearningEngine

engine = LearningEngine(
    store,
    distiller=HardExampleDistiller(),
)
```

蒸馏只决定“哪些记录值得留”，不碰对错标准和护栏域。完整边界与已验证范围见 [难例蒸馏](docs/DISTILLATION.md)。

### 蒸馏反哺

`v0.4.0` 新增可选的 `DistillateInducer`，默认关闭。它把已经蒸馏出的高纯证据整理成人工审查候选：

- 规则模板：多条 `core` 规则共享同一结构根因和动作时，生成条件合并候选；
- 结构弱点：多条 `core` 记录集中在同一 `structure_key` 时，生成设计复核候选；
- 隐患预测：高价值待处理难例集中在同一结构附近时，生成探针复查候选。

归纳只使用质量等级、反馈账本、来源覆盖、`family` 和 `structure_key`。它不调用 LLM，不推断语义覆盖，也没有自动改写运行库。

```python
from rhem import DistillateInducer, HardExampleDistiller, LearningEngine

engine = LearningEngine(
    store,
    distiller=HardExampleDistiller(),
    inducer=DistillateInducer(),
)

plan = engine.induct(now="2026-09-12T00:00:00Z")
```

规则模板必须人工批准后才通过补丁写入；结构弱点和隐患预测只接受或拒绝审查结论，不会自动改设计、代码或探针。完整边界见 [蒸馏反哺](docs/INDUCTION.md)。

### 人工批准

规则缺失和知识缺口不会自动补答案。

- 规则：决策器可以起草药方，但必须人工批准后才写入规则库；
- 知识：系统只上报未识别项，人工提供 `canonical`、`definition`、`kind` 后才写词条；
- 识别与流程：达到门控后可自动应用，但仍必须生成补丁。

### 补丁治理

所有自动变更都走统一补丁：

- 每次变更保存 `before` 和 `after` 快照；
- 变更动作和触发难例写入补丁文件；
- 回滚只能从最新活跃补丁开始，避免旧补丁覆盖较新的状态；
- 回滚后可运行复扫，检查历史难例是否再次被当前运行态解决。

### 护栏域

运行时补丁只允许写以下域：

- `alias_rules`
- `operational_rules`
- `terms`
- `induction_findings`
- `process_settings`

以下域被参考实现视为保护域：

- `guardrails`
- `model_weights`
- `system_prompt`
- `rhem_code`
- `source_records`

也就是说，进化的是记忆层，不是模型权重和硬约束本身，并且变更可审计、可回滚。

## 快速运行

要求 Python 3.9+，只使用标准库。

```powershell
# 完整离线演示
py -X utf8 -m rhem

# 运行测试
py -X utf8 -m unittest discover -s tests -p "test_*.py" -v
```

当前 demo 会依次演示：

1. 识别错误在第三次同族反馈后自动进入别名库；
2. BFS 圈定错误影响范围，DFS 找到共享根因候选，图簇门控只生成一个流程补丁；
3. 规则缺失先生成药方，人工批准后才写规则库；
4. 知识缺口先上报，人工归因后才写词条库；
5. 护栏域拒绝带保护目标的运行期补丁；
6. 最新补丁回滚后，复扫结果发生变化；
7. 显式启用自适应门控后，快速复发将门槛从 `3` 降到 `2`，并把门控依据写入补丁；
8. 显式启用难例蒸馏后，单来源重复会停留待定，跨来源复发可并库，多场景命中可升为 `core`；
9. 显式启用蒸馏反哺后，从 `core` 规则归纳候选，人工批准后才写入可回滚补丁。

demo 使用合成数据，例如 `order-104`、`ZL-9`，不包含真实订单、店名、坐标或客户信息。

## 代码地图

```text
rhem/
  __init__.py   公开入口
  models.py     Incident、ErrorCategory、HardExampleGroup 与异常
  graph.py      BFS/DFS 图探针、根因候选、图簇发现
  distillation.py  并库前分馏、反馈账本、库内回炼与补丁动作
  induction.py  从 core/high 账本归纳规则模板、结构弱点与隐患候选
  store.py      JSON 记忆库、蒸馏与反哺账本、图簇、护栏域、补丁、回滚、审计日志
  engine.py     图探针、固定/自适应门控、蒸馏与反哺接入、四类出口、人工批准、默认复扫
  demo.py       四类难例、图探针、门控、蒸馏与反哺的离线演示
tests/          单元测试
```

## 接入示例

### 识别错误

```python
from rhem import ErrorCategory, Incident, LearningEngine, RhemStore

store = RhemStore("./rhem_store")
engine = LearningEngine(store)

outcome = engine.ingest(Incident(
    category=ErrorCategory.RECOGNITION,
    family="slot:city:shanghai",
    message="发往上海，但上海被填进了发件城市",
    source="task:order-104",
    evidence={
        "text": "把货发往上海",
        "alias": "上海",
        "canonical": "shanghai",
    },
))
```

前两次通常返回 `waiting`。第三次达到默认门控后，返回 `auto_applied`，并创建别名补丁。

### BFS/DFS 图探针

把图放在 `Incident.evidence["graph"]`。下面的例子表示多个症状指向同一个上游锁冲突：

```python
outcome = engine.ingest(Incident(
    category=ErrorCategory.PROCESS,
    family="process:retry:fetch_orders:symptom-a",
    message="fetch_orders 在 session-1 中反复重试",
    source="task:session-1",
    evidence={
        "fix": {"max_retries": 2, "deadlock_detection_s": 3.0},
        "graph": {
            "session_id": "session-1",
            "cluster_key": "root:shared-lock",
            "start_node": "symptom-a",
            "nodes": [
                {"id": "symptom-a", "kind": "error"},
                {"id": "root:shared-lock", "kind": "root_cause"},
            ],
            "edges": [{
                "source": "symptom-a",
                "target": "root:shared-lock",
                "relation": "caused_by",
                "confidence": 0.96,
            }],
        },
    },
))
```

返回的 `graph_finding` 包含 `scope_node_ids`、`cause_path`、`root_candidate` 和风险标记；完整格式见 [双阶段图探针](docs/GRAPH-PROBE.md)。

### 规则药方与批准

```python
proposal = store.list_prescriptions(status="proposed")[0]
patch = engine.approve(proposal["id"], approver="徐应生")
```

规则缺失没有人工批准不会生效。

### 知识归因

```python
patch = engine.approve(
    proposal_id,
    approver="徐应生",
    attribution={
        "canonical": "ZL-9-BLK",
        "definition": "黑色 9 号重型支架",
        "kind": "product_sku",
    },
)
```

### 回滚与复扫

```python
before = engine.rescan()
engine.rollback(patch["id"])
after = engine.rescan()
```

`rescan()` 返回每条历史难例是否被当前运行态解决。它不是效果基准测试，只是确认补丁应用或回滚后的行为。

## 数据落盘

`RhemStore(root)` 会生成：

```text
<root>/
  memory.json        难例、别名、规则、词条、运行配置、补丁元数据
  guardrails.json    冻结的护栏清单
  events.jsonl       只追加的审计日志
  patches/pNNNN.json 每次自动变更的 before/after 快照与 actions
```

适用边界：

- 适合单进程演示、测试和行为验证；
- 不是面向高并发生产环境设计的数据库；
- 真实验证器、权限系统和分布式存储不在本仓库。

## 当前验证与未验证

已经验证：

- 四类难例各自可到达对应出口；
- BFS 能限制影响范围，DFS 能沿上游关系找到根因候选；
- 同会话同根因的多个症状只计一个独立证据，跨会话证据可累计；
- 循环、深度截断和低置信度边会转人工；
- 默认门控需要累计次数；
- 快速复发可触发自适应 `2` 次门槛；
- 长期安静保持基准 `3`，高危和复查失败锁到 `5` 并转人工；
- 自适应门控决策写入审计日志，并随提案、补丁和补丁元数据保存；
- 可要求多个来源后才并库；
- 难例蒸馏可拦截单来源低价值样本；
- 多场景命中可升为 core，连续失败可降权，长期闲置可清除并可回滚；
- 反馈账本可由 record_feedback 或显式 rescan(record_feedback=True) 回填；
- 蒸馏反哺只从 core/high 账本生成候选，人工批准与拒绝均可回滚；
- 规则缺失和知识缺口必须人工步骤；
- 补丁带 before/after 快照；
- 回滚保持补丁顺序约束；
- 回滚后复扫数量发生变化；
- 护栏域目标会被拒绝；
- 数据在重新加载 `RhemStore` 后仍然存在。

尚未验证，因此不声称：

- 对真实生产任务能提高多少准确率；
- 能节省多少成本或延迟；
- 蒸馏阈值和 30/90 天周期在真实长尾上的统计最优性；
- 蒸馏反哺对真实泛化、准确率或探针收益的提升幅度；
- 门控阈值在长尾分布上的统计最优性；
- LLM 置信度校准已经可靠；
- 可在多进程、多租户或高并发环境下直接使用。

这些是 roadmap，而不是既成事实。

## Roadmap

- [x] 带来源标记的待学习区
- [x] 同族累计门控
- [x] 可选自适应门控 `v0.2.0`
- [x] 可选难例蒸馏 `v0.3.0`
- [x] 可选蒸馏反哺 `v0.4.0`
- [x] 白皮书完整版 `v0.5.0`
- [x] 归档元数据修正 `v0.5.1`
- [x] BFS/DFS 双阶段图探针与图簇门控
- [x] 识别错误自动进入别名库
- [x] 流程错误结构化修复
- [x] 规则药方与人工批准
- [x] 知识缺口人工归因
- [x] before/after 快照与顺序回滚
- [x] 历史难例复扫基线
- [x] 护栏域运行期禁写
- [ ] 接入真实 Agent 轨迹与验证器
- [ ] 建立离线评估集、回归集与冲突检测
- [ ] 将自适应门控接入真实评估集与冲突验证
- [ ] 输出可供未来模型训练的已标注难例数据

## 版本

当前参考实现版本为 `v0.5.1`，日期 `2026-09-12`。本版修正 `v0.5.0` 发布时未同步版本元数据的问题，使归档内部版本、白皮书版本和 Zenodo 记录保持一致；运行行为没有变化。完整变更见 [CHANGELOG.md](CHANGELOG.md)。

## 文档

- [白皮书](WHITEPAPER.md)：方法、四类难例、业内对照、局限与 Roadmap
- [版本变更](CHANGELOG.md)：`v0.5.1`、`v0.5.0`、`v0.4.0`、`v0.3.0`、`v0.2.0` 与 `v0.1.2` 的版本记录
- [先驱与绘图师](docs/PIONEER-AND-CARTOGRAPHER.md)：RHEM 与难例发掘的先后定位
- [适用阶段与分工](docs/APPLICABILITY-AND-STAGING.md)：前期优先使用 RHEM 的理由、边界与分工
- [双阶段图探针](docs/GRAPH-PROBE.md)：BFS/DFS 语义、图格式、图簇门控、人工接管条件与局限
- [自适应门控](docs/ADAPTIVE-GATING.md)：连续复发压力、2/3/5 门槛、风险锁、审计与试点边界
- [难例蒸馏](docs/DISTILLATION.md)：并库前分馏、反馈账本、库内回炼、补丁边界与试点说明
- [蒸馏反哺](docs/INDUCTION.md)：候选归纳、人工批准、结构复核、隐患排序与未验证边界

## 引用

概念 DOI，代表项目整体：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (概念 DOI).
Zenodo. https://doi.org/10.5281/zenodo.22681163
```

`v0.5.1` 的版本 DOI，固定对应本次归档元数据修正版本：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (v0.5.1).
Zenodo. https://doi.org/10.5281/zenodo.22719758
```

`v0.5.0` 的版本 DOI，固定对应白皮书完整版的首次归档：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (v0.5.0).
Zenodo. https://doi.org/10.5281/zenodo.22719697
```

`v0.4.0` 的版本 DOI，固定对应蒸馏反哺版本：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (v0.4.0).
Zenodo. https://doi.org/10.5281/zenodo.22719563
```

`v0.3.0` 的版本 DOI，固定对应难例蒸馏版本：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (v0.3.0).
Zenodo. https://doi.org/10.5281/zenodo.22719509
```

引用 `v0.5.1` 时使用 `10.5281/zenodo.22719758`；引用 `v0.5.0` 时使用 `10.5281/zenodo.22719697`；引用 `v0.4.0` 时使用 `10.5281/zenodo.22719563`；引用 `v0.3.0` 时使用 `10.5281/zenodo.22719509`；引用项目所有版本时使用概念 DOI `10.5281/zenodo.22681163`。

首个版本 DOI，固定对应 `v2026-09-10` 的存档：

```text
徐应生. (2026). RHEM — Runtime Hard-Example Mining for Agent Systems (v2026-09-10).
Zenodo. https://doi.org/10.5281/zenodo.22681164
```

## License 与边界

MIT License，见 [LICENSE](LICENSE)。

方法与参考实现公开；生产环境中的防篡改、私有探针、内部部署脚本和未公开验证材料不在本仓库。欢迎通过 Issues 提交复现、反例或更早的同形出处。
