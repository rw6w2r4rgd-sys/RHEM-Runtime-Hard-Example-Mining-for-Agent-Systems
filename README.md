# RHEM — Runtime Hard-Example Mining for Agent Systems

本仓库包含方法论文档、定位说明与 MIT 参考实现。

## 文档

- [白皮书：RHEM 方法、四类难例与局限](WHITEPAPER.md)
- [先驱与绘图师：RHEM 与难例发掘的先后定位](docs/PIONEER-AND-CARTOGRAPHER.md)

本目录把《RHEM — Runtime Hard-Example Mining for Agent Systems》方法论文档
实现为一套可离线运行的 Python 参考实现。它演示的不是“大模型重训”，而是
**运行期难例的免重训闭环**：错误进待学习区，同类累计达标后才并库；
自动变更全部以补丁方式落地，可快照、可回滚、可复扫。

## 运行

在仓库根目录（包含 `rhem/` 的目录）执行：

```powershell
# 跑完整离线演示：四类难例、门控、人批准、护栏拦截、回滚复扫
py -X utf8 -m rhem

# 跑单元测试
py -X utf8 -m unittest discover -s rhem\tests -p "test_*.py" -v
```

依赖只有 Python 3.9+ 标准库，不需要网络、API Key 或第三方包。

## 四类难例的落地方式

| 类别 | 参考出口 | 是否需要人工 |
|---|---|---|
| 识别错误 | 别名/纠错并库，运行期即时生效 | 否，自动 |
| 流程错误 | 结构性修复，修改运行配置，不入知识库 | 否，自动 |
| 规则缺失 | 决策器开“药方”，人工批准后写规则库 | 是 |
| 知识缺口 | 上报待归因，人工给出 canonical/definition 后写词条库 | 是 |

所有出口共用同一个门控：同一 `family` 累计 `min_occurrences` 次
（默认 3）才进入判断；`GatePolicy.min_sources` 还可要求来自多个来源，
防止单点重复反馈污染。

## 目录与数据文件

```text
rhem/
  __init__.py      包入口
  models.py        Incident、HardExampleGroup、错误类型
  store.py         持久化记忆库、护栏域、补丁治理
  engine.py        门控、自动并库、决策器、人工批准、复扫
  demo.py          可离线演示
  tests/           单元测试
```

运行后，默认记忆库写入你传入的 `RhemStore(root)`：

```text
<root>/
  memory.json       难例、别名、规则、词条、运行配置
  guardrails.json   护栏域，运行期冻结
  events.jsonl      只追加审计日志
  patches/p0001.json 自动变更的 before/after 快照与 actions
```

## 最小接入示例

```python
from rhem import Incident, ErrorCategory, LearningEngine, RhemStore

store = RhemStore("./rhem_store")
engine = LearningEngine(store)

outcome = engine.ingest(Incident(
    category=ErrorCategory.RECOGNITION,
    family="slot:city:shanghai",
    message="用户说发往上海，系统把上海填进发件城市",
    source="task:order-104",
    evidence={
        "text": "把货发往上海",
        "alias": "上海",
        "canonical": "shanghai",
    },
))
```

第三次同族难例达标后，`alias_rules` 里会出现
`shanghai <= 上海`，后续解析器读取该表即可免重训生效。

## 补丁与回滚语义

- 自动并库、结构修复、人工批准后的规则/词条写入都会生成补丁。
- 补丁文件保存 before/after 全量快照和 actions。
- 回滚只允许按顺序从最新补丁开始，避免旧补丁覆盖后来的状态。
- 回滚后对应难例族回到 `rolled_back`，`engine.rescan()` 可复扫历史难例，
  观察有多少条再次被当前运行态解决。

## 护栏域

`guardrails.json` 把 `model_weights`、`system_prompt`、`guardrails`、
`source_records` 等列为保护域。运行时补丁只能写白名单域：
`alias_rules`、`operational_rules`、`terms`、`process_settings`。
参考实现直接拒绝带 `target=guardrails` 等护栏区目标的 commit。

## 诚实声明

这是**参考实现/行为基线**，不是生产级 Agent 框架：

1. 门控仍以次数为主，README 中的 F1/冲突验证升级尚未实现；
2. `store` 使用单文件 JSON，适合单进程演示，不适合高并发生产；
3. 默认 `rescan` 是确定性规则复扫，不替代真实回归测试集；
4. 护栏维护只留在人工通道，代码层没有提供 UI；
5. 真实 Agent 接入时，需要在 `evidence` 中注入可信的现场轨迹、修正值和验证器。

## License

MIT；本目录为方法论的开源参考实现。

## 验证与反驳

欢迎通过 [Issues](https://github.com/rw6w2r4rgd-sys/RHEM-Runtime-Hard-Example-Mining-for-Agent-Systems/issues)
提交验证结果、复现用例或更早的同形出处。对 RHEM 的质疑不需要“礼貌确认”，
只要能给出可复现证据，就是这篇方法论文档需要的活证。
