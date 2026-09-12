# -*- coding: utf-8 -*-
"""RHEM 可离线演示：把四类难例、门控与蒸馏跑成闭环。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

from .distillation import HardExampleDistiller
from .engine import AdaptiveGatePolicy, LearningEngine
from .models import CATEGORY_LABELS, ErrorCategory, GuardrailViolation, Incident
from .store import RhemStore


def _feed(
    engine: LearningEngine,
    category: ErrorCategory,
    family: str,
    source: str,
    message: str,
    evidence: Dict[str, Any],
    expected: str | None = None,
    actual: str | None = None,
    occurred_at: str | None = None,
) -> Dict[str, Any]:
    incident_data = {
        "category": category,
        "family": family,
        "message": message,
        "source": source,
        "evidence": evidence,
        "expected": expected,
        "actual": actual,
    }
    if occurred_at:
        incident_data["occurred_at"] = occurred_at
    incident = Incident(**incident_data)
    return engine.ingest(incident)


def _print_outcome(step: str, outcome: Dict[str, Any]) -> None:
    status = outcome.get("event", "waiting")
    note = outcome.get("message", "")
    print(f"    [{step}] 第 {outcome.get('occurrences')} 次 | {status}")
    print(f"      {note}")
    if outcome.get("cluster_key"):
        print(f"      图簇: {outcome['cluster_key']}")
    finding = outcome.get("graph_finding")
    if finding:
        print(
            f"      根因候选: {finding['root_candidate']} | "
            f"路径: {' -> '.join(finding['cause_path'])}"
        )
        print(f"      影响范围: {finding['scope_node_ids']}")
    if outcome.get("patch_id"):
        print(f"      补丁: {outcome['patch_id']}")
    if outcome.get("proposal_id"):
        print(f"      药方: {outcome['proposal_id']}")

def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="rhem_demo_"))
    store = RhemStore(root)
    engine = LearningEngine(store)

    print("=" * 74)
    print("RHEM v0.3.0 参考实现演示：错一次，教会一类；免重训，当场生效")
    print("=" * 74)
    print(f"持久化目录: {store.root}")
    print(f"门控: 同一图簇或同族 {engine.gate.min_occurrences} 个独立证据后进入并库判断")
    print()

    # ---------- 1. 识别错误 → 别名库 ----------
    print("--- 1) 识别错误：收货城市“上海”连续被串成发件城市 ---")
    family = "slot:recipient_city:shanghai"
    for idx, source in enumerate(
        ["order-104", "order-118", "order-125"], start=1
    ):
        outcome = _feed(
            engine,
            ErrorCategory.RECOGNITION,
            family,
            f"task:{source}",
            f"{source} 的地址栏写的是“发往上海”，系统却把上海填进了发件城市。",
            {
                "text": "把货发往上海",
                "alias": "上海",
                "canonical": "shanghai",
                "correct_target": "recipient_city",
                "wrong_target": "sender_city",
            },
            expected="shanghai",
            actual="beijing",
        )
        _print_outcome(f"识别 #{idx}", outcome)
    rules = store.view()["alias_rules"]
    for canonical, rule in rules.items():
        print(f"    -> 别名库已生效: {canonical} <= {rule['aliases']} (patch={rule['patch_id']})")
    print()

    # ---------- 2. 图探针：BFS 圈定范围，DFS 追踪根因 ----------
    print("--- 2) 图探针：多个症状共享一个上游根因 ---")
    root_key = "root:fetch_orders:shared_lock"
    graph_cases = [
        ("session-1", "symptom-a"),
        ("session-1", "symptom-b"),
        ("session-1", "symptom-c"),
        ("session-2", "symptom-d"),
        ("session-3", "symptom-e"),
    ]
    for idx, (session_id, symptom_id) in enumerate(graph_cases, start=1):
        graph = {
            "session_id": session_id,
            "cluster_key": root_key,
            "start_node": symptom_id,
            "nodes": [
                {"id": symptom_id, "kind": "error"},
                {
                    "id": root_key,
                    "kind": "root_cause",
                    "ref_id": root_key,
                },
                {
                    "id": "tool:fetch_orders",
                    "kind": "tool",
                    "ref_id": "fetch_orders",
                },
            ],
            "edges": [
                {
                    "source": symptom_id,
                    "target": root_key,
                    "relation": "caused_by",
                    "confidence": 0.96,
                },
                {
                    "source": root_key,
                    "target": "tool:fetch_orders",
                    "relation": "runs_on",
                    "confidence": 0.9,
                },
            ],
        }
        outcome = _feed(
            engine,
            ErrorCategory.PROCESS,
            f"process:graph:{symptom_id}",
            f"task:{session_id}:{symptom_id}",
            f"{symptom_id} 在 {session_id} 中出现无限重试。",
            {
                "tool": "fetch_orders",
                "fix": {"max_retries": 2, "deadlock_detection_s": 3.0},
                "graph": graph,
            },
        )
        _print_outcome(f"图探针 #{idx}", outcome)
    graph_cluster = store.get_cluster(root_key)
    print(
        "    -> 门控按图簇去重："
        f"独立证据={graph_cluster['occurrences']} "
        f"症状={graph_cluster['symptom_count']} "
        f"状态={graph_cluster['status']}"
    )
    print(f"    -> 结构性修复已生效(不入库): {store.view()['process_settings']}")
    print()
    # ---------- 3. 规则缺失 → 药方 + 人批准 ----------
    print("--- 3) 规则缺失：收件人字段缺失时系统仍自动猜测 ---")
    family = "rule:missing_recipient_confirmation"
    outcomes: list[Dict[str, Any]] = []
    for idx, source in enumerate(["customer-7", "customer-8", "customer-9"], start=1):
        outcome = _feed(
            engine,
            ErrorCategory.RULE_GAP,
            family,
            f"task:{source}",
            f"{source} 未填写收件人，系统直接猜了一个收件人并继续发货。",
            {
                "rule_name": "收件人缺失时不得自动猜测",
                "rule_reason": "自动猜测收件人会产生不可逆的配送副作用",
                "rule_condition": "order.recipient is empty",
                "rule_action": "暂停执行并询问用户收件人",
            },
        )
        _print_outcome(f"规则 #{idx}", outcome)
        outcomes.append(outcome)
    proposal_id = outcomes[-1]["proposal_id"]
    print(f"    -> 决策器药方: {proposal_id}")
    print("    -> 未批准前规则库为空：")
    print(f"       operational_rules = {store.view()['operational_rules']}")
    patch = engine.approve(proposal_id, approver="fde:lin", attribution=None)
    print(f"    -> 人工批准后规则已入库: {store.view()['operational_rules'][list(store.view()['operational_rules'])[0]]['name']} (patch={patch['id']})")
    print()

    # ---------- 4. 知识缺口 → 上报 + 人工归因 ----------
    print("--- 4) 知识缺口：新品 SKU“ZL-9”多次未识别 ---")
    family = "term:sku:ZL-9"
    outcomes = []
    for idx, source in enumerate(["warehouse-1", "warehouse-2", "warehouse-3"], start=1):
        outcome = _feed(
            engine,
            ErrorCategory.KNOWLEDGE_GAP,
            family,
            f"task:{source}",
            f"{source} 单据出现未收录品名 ZL-9，系统无法归类。",
            {"term": "ZL-9", "source_doc": f"{source}-picklist.pdf"},
        )
        _print_outcome(f"知识 #{idx}", outcome)
        outcomes.append(outcome)
    proposal_id = outcomes[-1]["proposal_id"]
    print(f"    -> 待归因药方: {proposal_id}")
    patch = engine.approve(
        proposal_id,
        approver="warehouse:chen",
        attribution={
            "canonical": "ZL-9-BLK",
            "definition": "黑色 9 号重型支架（仓库分区 B3）",
            "kind": "product_sku",
        },
    )
    print(f"    -> 人工归因后词条已入库: {store.view()['terms']['ZL-9']['canonical']} (patch={patch['id']})")
    print()

    # ---------- 护栏与补丁 ----------
    print("--- 5) 护栏域：运行期禁止把规则补丁挂到护栏区 ---")
    try:
        store.commit(
            actions=[{
                "op": "upsert_rule",
                "rule": {"id": "evil", "name": "篡改护栏", "enabled": True},
                "target": "guardrails",
            }],
            summary="非法尝试",
        )
    except GuardrailViolation as exc:
        print(f"    -> 已拦截: {exc}")
    print(f"    -> 护栏硬约束: {store.guardrails['hard_constraints']}")
    print()

    print("--- 6) 放射性补丁：最新补丁可回滚并复扫 ---")
    rescan_before = engine.rescan()
    print(
        f"    回滚前复扫: total={rescan_before['total']} "
        f"resolved={rescan_before['resolved']} failed={rescan_before['failed']}"
    )
    rolled = engine.rollback(patch["id"])
    print(f"    -> 已回滚知识词条补丁 {rolled['id']} (status={rolled['status']})")
    rescan_after = engine.rescan()
    print(
        f"    回滚后复扫: total={rescan_after['total']} "
        f"resolved={rescan_after['resolved']} failed={rescan_after['failed']}"
    )
    print()

    # ---------- 7. 自适应门控试点 ----------
    print("--- 7) 自适应门控：快速复发降到 2 次，但只作为显式试点 ---")
    adaptive_store = RhemStore(root / "adaptive_gate")
    adaptive_engine = LearningEngine(
        adaptive_store,
        gate=AdaptiveGatePolicy(),
    )
    for idx, (source, occurred_at) in enumerate([
        ("adaptive-a", "2026-09-10T00:00:00Z"),
        ("adaptive-b", "2026-09-10T01:00:00Z"),
    ], start=1):
        outcome = _feed(
            adaptive_engine,
            ErrorCategory.RECOGNITION,
            "slot:adaptive:city",
            f"task:{source}",
            "城市槽位在短时间内快速复发。",
            {
                "alias": "X",
                "canonical": "x",
            },
            expected="x",
            actual="X",
            occurred_at=occurred_at,
        )
        decision = outcome["gate_decision"]
        print(
            f"    -> 自适应 #{idx}: event={outcome['event']} "
            f"threshold={decision['effective_min_occurrences']} "
            f"reason={decision['reason']} "
            f"sensitivity={decision['sensitivity']}"
        )
    adaptive_patch = adaptive_store.latest_active_patch()
    if adaptive_patch:
        meta = adaptive_store.view()["patch_meta"][adaptive_patch]
        print(
            "    -> 补丁已保留门控依据: "
            f"patch={adaptive_patch} "
            f"gate_reason={meta['gate_decision']['reason']}"
        )
    print("    -> 默认门控未改变；此策略需要显式传入 AdaptiveGatePolicy。")
    print()

    # ---------- 8. 难例蒸馏试点 ----------
    print("--- 8) 难例蒸馏：先分馏价值，再按反馈账本回炼 ---")
    distill_store = RhemStore(root / "distillation")
    distill_engine = LearningEngine(
        distill_store,
        distiller=HardExampleDistiller(),
    )
    for idx, occurred_at in enumerate([
        "2026-09-10T00:00:00Z",
        "2026-09-10T01:00:00Z",
        "2026-09-10T02:00:00Z",
    ], start=1):
        outcome = _feed(
            distill_engine,
            ErrorCategory.RECOGNITION,
            "slot:distill:single",
            "same-observer",
            "单来源重复反馈。",
            {"alias": "S", "canonical": "s"},
            expected="s",
            actual="S",
            occurred_at=occurred_at,
        )
    print(
        "    -> 单来源重复: "
        f"event={outcome['event']} "
        f"tier={outcome['distillation_decision']['tier']} "
        f"reason={outcome['distillation_decision']['reason']}"
    )

    for idx, source in enumerate(("task-a", "task-b", "task-c"), start=1):
        outcome = _feed(
            distill_engine,
            ErrorCategory.RECOGNITION,
            "slot:distill:cross",
            source,
            "跨来源重复反馈。",
            {"alias": "X", "canonical": "x"},
            expected="x",
            actual="X",
            occurred_at=f"2026-09-10T0{idx}:00:00Z",
        )
    print(
        "    -> 跨来源复发: "
        f"event={outcome['event']} "
        f"tier={outcome['distillation_decision']['tier']} "
        f"patch={outcome['patch_id']}"
    )

    for scenario in ("checkout", "support", "checkout"):
        distill_store.record_feedback(
            "alias",
            "x",
            scenario,
            resolved=True,
        )
    plan = distill_engine.redistill(now="2026-09-12T00:00:00Z")
    applied = distill_engine.redistill(
        apply=True,
        actor="human:demo",
        now="2026-09-12T00:00:00Z",
    )
    quality = distill_store.view()["alias_rules"]["x"]["distillation"]
    print(
        "    -> 回炼计划: "
        f"core={plan['summary']['core']} "
        f"downweight={plan['summary']['downweight']} "
        f"cull={plan['summary']['cull']}"
    )
    print(
        "    -> 多场景证据已升为核心: "
        f"quality={quality['quality_tier']} "
        f"hits={quality['hits']} "
        f"scenarios={len(quality['scenario_sources'])} "
        f"patch={applied['patch_id']}"
    )
    print("    -> 默认引擎未启用蒸馏；显式传入 HardExampleDistiller 才生效。")
    print()

    print("--- 结果总览 ---")
    view = store.view()
    for group in view["hard_examples"].values():
        label = CATEGORY_LABELS[ErrorCategory(group["category"])]
        print(
            f"  {label} | family={group['family']} | "
            f"count={group['occurrences']} | status={group['status']}"
        )
    print(f"\n事件日志: {store.event_file}")
    print(f"补丁快照: {store.patch_dir}")


if __name__ == "__main__":
    main()
