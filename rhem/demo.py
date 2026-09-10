# -*- coding: utf-8 -*-
"""RHEM 可离线演示：把附带的 README 方法跑成四类难例闭环。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict

from .engine import LearningEngine
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
) -> Dict[str, Any]:
    incident = Incident(
        category=category,
        family=family,
        message=message,
        source=source,
        evidence=evidence,
        expected=expected,
        actual=actual,
    )
    return engine.ingest(incident)


def _print_outcome(step: str, outcome: Dict[str, Any]) -> None:
    status = outcome.get("event", "waiting")
    note = outcome.get("message", "")
    print(f"    [{step}] 第 {outcome.get('occurrences')} 次 | {status}")
    print(f"      {note}")
    if outcome.get("patch_id"):
        print(f"      补丁: {outcome['patch_id']}")
    if outcome.get("proposal_id"):
        print(f"      药方: {outcome['proposal_id']}")


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="rhem_demo_"))
    store = RhemStore(root)
    engine = LearningEngine(store)

    print("=" * 74)
    print("RHEM 参考实现演示：错一次，教会一类；免重训，当场生效")
    print("=" * 74)
    print(f"持久化目录: {store.root}")
    print(f"门控: 同族 {engine.gate.min_occurrences} 次后进入并库判断")
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

    # ---------- 2. 流程错误 → 结构性修复 ----------
    print("--- 2) 流程错误：HTTP 拉单反复重试导致死锁式卡死 ---")
    family = "process:retry:fetch_orders"
    for idx, source in enumerate(["queue-a", "queue-b", "queue-c"], start=1):
        outcome = _feed(
            engine,
            ErrorCategory.PROCESS,
            family,
            f"task:{source}",
            f"{source} 在 fetch_orders 上进入无限重试，直到超时。",
            {
                "tool": "fetch_orders",
                "fix": {"max_retries": 2, "deadlock_detection_s": 3.0},
            },
        )
        _print_outcome(f"流程 #{idx}", outcome)
    settings = store.view()["process_settings"]
    print(f"    -> 结构性修复已生效(不入库): {settings}")
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