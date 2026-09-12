# RHEM 双阶段图探针：BFS 圈范围，DFS 追根因

## 结论

RHEM 图探针不是“让程序自动理解任意日志”，而是一套确定性的图遍历层：

- `BFS`：从错误节点出发，在有限深度内圈定影响范围和相关问题簇；
- `DFS`：沿 `caused_by` / `depends_on` 向上游追踪根因候选；
- 两者都在门控之前执行；
- 图分析结果先归并为 `cluster_key`，再决定是否需要人工；
- 只有图结构清晰、门控通过且类别允许时，才会进入原有无重训补丁链路。

当前实现只处理调用方显式提供的图。图从哪里来、节点和边是否可信，由接入方负责。

## 为什么放在门控之前

如果同一根因连续触发多个症状，按 `family` 分别计数会把一个根因误判成多个独立证据。

例如同一会话中：

```text
symptom-a --caused_by--> shared-lock
symptom-b --caused_by--> shared-lock
symptom-c --caused_by--> shared-lock
```

三个症状可以有不同的 `family`，但它们的根因候选是同一个。图探针先把它们归入同一 `cluster_key`，门控再按独立证据计数。

默认去重键为：

```text
root_candidate@session_id
```

因此：

- 同一根因、同一会话中的多个症状，只算一个独立证据；
- 同一根因、不同会话，可以分别形成独立证据；
- 没有 `session_id` 时，退化为 `root_candidate@source`。

这不是概率模型，也不会“证明”根因一定正确。它只防止同源症状在计数层被重复放大。

## BFS：圈定影响范围

BFS 使用无向可达性，目的是找出“附近还有哪些问题或组件可能有关”，而不是判定因果。

默认策略：

```python
TraversalPolicy(
    max_bfs_depth=2,
    max_dfs_depth=8,
    min_edge_confidence=0.6,
    upstream_relations=("caused_by", "depends_on"),
)
```

BFS 输出：

- `scope_node_ids`：有限深度内访问到的节点；
- `related_incident_ids`：范围内标记为 `incident`、`error` 或 `failure` 的节点；
- `affected_domains`：范围内标记为 `component`、`domain`、`service`、`tool` 或 `step` 的节点；
- `levels`：内部遍历深度，目前用于范围控制。

它回答的是：

> 这个错误可能波及哪些附近节点？

它不回答：

> 这些节点一定都是根因。

## DFS：追踪上游根因

DFS 只沿指定上游关系前进，默认是：

```text
caused_by
depends_on
```

边的方向定义为：

```text
症状节点 -> 上游原因节点
```

例如：

```json
{
  "source": "symptom-a",
  "target": "root:shared-lock",
  "relation": "caused_by",
  "confidence": 0.96
}
```

DFS 选择当前可走路径中最长的一条作为 `cause_path`，末端节点作为 `root_candidate`。平局时，高置信度分支会先被访问。

输出包括：

- `root_candidate`：当前最有代表性的上游根因候选；
- `cause_path`：从现场错误到候选根因的路径；
- `cycle_detected`：路径中出现循环；
- `truncated`：追踪达到 `max_dfs_depth`；
- `low_confidence_edge`：观察到低于阈值的相关边；
- `needs_human`：以上任一风险成立时为 `true`。

`root_candidate` 是候选，不是经人工确认的最终根因。如果唯一可用上游边低于置信度阈值，且没有形成其他有效路径，`root_candidate` 会返回 `null`，避免把起点伪装成确认过的根因。

## 图输入格式

图放在 `Incident.evidence["graph"]`：

```json
{
  "session_id": "session-1",
  "cluster_key": "root:shared-lock",
  "start_node": "symptom-a",
  "nodes": [
    {
      "id": "symptom-a",
      "kind": "error",
      "ref_id": "incident-a",
      "metadata": {}
    },
    {
      "id": "root:shared-lock",
      "kind": "root_cause",
      "ref_id": "lock:orders",
      "metadata": {
        "cluster_key": "root:shared-lock"
      }
    }
  ],
  "edges": [
    {
      "source": "symptom-a",
      "target": "root:shared-lock",
      "relation": "caused_by",
      "confidence": 0.96
    }
  ]
}
```

字段说明：

| 字段 | 作用 |
|---|---|
| `session_id` | 独立证据去重的会话边界 |
| `cluster_key` | 可选的显式图簇标识，根因路径不明确时尤其重要 |
| `start_node` | 本次从哪个节点开始遍历 |
| `nodes[].id` | 图内唯一节点 ID |
| `nodes[].kind` | `error`、`root_cause`、`tool`、`step` 等语义标签 |
| `nodes[].ref_id` | 指回真实业务对象的可选 ID |
| `nodes[].metadata.cluster_key` | 根因节点级图簇提示 |
| `edges[].relation` | `caused_by`、`depends_on` 或普通关系 |
| `edges[].confidence` | `[0, 1]` 范围内的边置信度 |

`cluster_key` 的优先级为：

1. 图级 `graph.cluster_key`；
2. 根因节点 `metadata.cluster_key`；
3. 根因节点 `ref_id`；
4. 根因节点 `id`；
5. 原难例 `family`。

图级 `cluster_key` 很重要：当低置信度边导致无法确认根因时，它仍能让相关症状进入同一人工复核簇。

## 与门控的关系

没有图时，门控仍按原 `family` 工作，兼容旧数据。固定门控和 `v0.2.0` 的可选 `AdaptiveGatePolicy` 都可以读取图簇记录。

有图时：

1. 先构建 `GraphFinding`；
2. 写入或更新 `Incident.cluster_key`；
3. `RhemStore` 同时维护 `hard_examples` 和 `clusters`；
4. 门控优先使用图簇的独立证据数和来源数；
5. 图簇状态会同步到同簇内的不同 `family`；
6. 补丁、提案和回滚都保留 `cluster_key`。

`clusters` 记录包含：

```text
cluster_key
category
occurrences
symptom_count
sources
families
incident_ids
evidence_keys
occurrence_times
gate_flags
status
patch_id
proposal_id
```

`occurrences` 是去重后的独立证据数，`symptom_count` 是实际症状条数。两者不等，正是图探针的作用。

## 人工接管条件

出现以下任一情况时，图分析会给出 `needs_human=True`：

- 上游路径存在循环；
- DFS 达到深度上限；
- 观察到低于 `min_edge_confidence` 的边；
- BFS 或 DFS 无法形成清晰路径。

达到门控后，识别错误和流程错误也不会自动应用补丁，而是返回：

```text
needs_human_graph
```

规则缺失和知识缺口原本就需要人工步骤；图结构不明确时同样先转人工复核。

原因是：图遍历只能处理显式结构，不能替人证明现实中的因果。

## 代码入口

核心实现：

```text
rhem/graph.py
```

主要类型：

- `GraphNode`
- `GraphEdge`
- `IncidentGraph`
- `TraversalPolicy`
- `GraphFinding`
- `GraphAnalyzer`

接入引擎：

```python
from rhem import GraphAnalyzer, LearningEngine

engine = LearningEngine(
    store,
    graph_analyzer=GraphAnalyzer(),
)
```

带图的难例仍然通过：

```python
outcome = engine.ingest(incident)
```

返回的 `outcome` 增加：

- `cluster_key`
- `symptom_count`
- `graph_finding`
- `family_occurrences`

## 已验证与未验证

当前单元测试验证：

- BFS 深度限制；
- DFS 上游路径和根因候选；
- 循环检测；
- 深度截断；
- 低置信度边转人工；
- 同会话同根因去重；
- 跨会话独立证据累计；
- 图簇门控只生成一个补丁；
- 提案、状态同步和回滚后的图簇状态。

当前不声称：

- 能自动从任意日志中还原完整因果图；
- 能识别所有隐藏根因；
- 低置信度边阈值对所有系统都合适；
- `cluster_key` 的划分策略已经在生产长尾上得到统计验证；
- 图探针可以替代人工根因分析、回归测试或评估集。

RHEM 图探针只负责把显式图结构压缩成可门控、可审计、可回滚的运行时证据。