# -*- coding: utf-8 -*-
"""RHEM 参考实现单元测试：门控、四类出口、护栏、补丁与复扫。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.engine import GatePolicy, LearningEngine
from rhem.models import (
    ErrorCategory,
    GuardrailViolation,
    Incident,
    NotFound,
    RollbackOrderError,
)
from rhem.store import RhemStore


def make_incident(
    category: ErrorCategory,
    family: str,
    source: str,
    evidence: dict,
    seq: int = 0,
) -> Incident:
    return Incident(
        category=category,
        family=family,
        message=f"incident {seq} from {source}",
        source=source,
        evidence=evidence,
        expected=evidence.get("canonical"),
        actual=evidence.get("alias") or evidence.get("actual"),
    )


class RhemEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)
        self.engine = LearningEngine(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _alias_incident(self, source: str) -> Incident:
        return make_incident(
            ErrorCategory.RECOGNITION,
            "slot:city:shanghai",
            source,
            {"text": "发往上海", "alias": "上海", "canonical": "shanghai"},
        )

    def test_gate_requires_three_occurrences(self) -> None:
        first = self.engine.ingest(self._alias_incident("a"))
        second = self.engine.ingest(self._alias_incident("b"))
        self.assertEqual(first["event"], "waiting")
        self.assertEqual(second["event"], "waiting")
        self.assertNotIn("shanghai", self.store.view()["alias_rules"])
        third = self.engine.ingest(self._alias_incident("c"))
        self.assertEqual(third["event"], "auto_applied")
        rule = self.store.view()["alias_rules"]["shanghai"]
        self.assertIn("上海", rule["aliases"])
        self.assertEqual(third["occurrences"], 3)
        rescan = self.engine.rescan()
        self.assertEqual(rescan["resolved"], 3)
        self.assertEqual(rescan["failed"], 0)

    def test_min_sources_can_stop_single_observer_pollution(self) -> None:
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=3, min_sources=2),
        )
        for source in ("same-robot", "same-robot", "same-robot"):
            engine.ingest(self._alias_incident(source))
        self.assertNotIn("shanghai", self.store.view()["alias_rules"])
        engine.ingest(self._alias_incident("different-robot"))
        self.assertIn("shanghai", self.store.view()["alias_rules"])

    def test_process_error_becomes_structural_patch_not_knowledge(self) -> None:
        incident = make_incident(
            ErrorCategory.PROCESS,
            "process:retry:fetch",
            "worker-a",
            {"fix": {"max_retries": 2, "deadlock_detection_s": 3.0}},
        )
        for source in ("worker-a", "worker-b", "worker-c"):
            incident.source = source
            self.engine.ingest(incident)
        self.assertEqual(
            self.store.view()["process_settings"]["max_retries"], 2
        )
        self.assertFalse(self.store.view()["operational_rules"])
        rescan = self.engine.rescan()
        self.assertEqual(rescan["resolved"], 3)

    def test_rule_gap_needs_human_approval(self) -> None:
        for source in ("u-1", "u-2", "u-3"):
            self.engine.ingest(make_incident(
                ErrorCategory.RULE_GAP,
                "rule:recipient_required",
                source,
                {
                    "rule_name": "收件人缺失时暂停",
                    "rule_reason": "防错误发货",
                    "rule_condition": "recipient empty",
                    "rule_action": "ask user",
                },
            ))
        proposals = self.store.list_prescriptions(status="proposed")
        self.assertEqual(len(proposals), 1)
        self.assertFalse(self.store.view()["operational_rules"])
        patch = self.engine.approve(proposals[0]["id"], approver="human:owner")
        self.assertEqual(len(self.store.view()["operational_rules"]), 1)
        self.assertEqual(patch["status"], "active")
        rescan = self.engine.rescan()
        self.assertEqual(rescan["resolved"], 3)

    def test_knowledge_gap_requires_human_attribution(self) -> None:
        for source in ("w-1", "w-2", "w-3"):
            self.engine.ingest(make_incident(
                ErrorCategory.KNOWLEDGE_GAP,
                "term:sku:ZL-9",
                source,
                {"term": "ZL-9", "source_doc": f"{source}.pdf"},
            ))
        proposals = self.store.list_prescriptions(status="proposed")
        self.assertEqual(len(proposals), 1)
        with self.assertRaises(NotFound):
            self.engine.approve(
                proposals[0]["id"],
                approver="human:warehouse",
                attribution={"canonical": "ZL-9-BLK"},
            )
        self.engine.approve(
            proposals[0]["id"],
            approver="human:warehouse",
            attribution={
                "canonical": "ZL-9-BLK",
                "definition": "black heavy bracket",
                "kind": "product_sku",
            },
        )
        self.assertEqual(
            self.store.view()["terms"]["ZL-9"]["canonical"],
            "ZL-9-BLK",
        )
        rescan = self.engine.rescan()
        self.assertEqual(rescan["resolved"], 3)

    def test_rollback_restores_memory_and_rescan_flips(self) -> None:
        for source in ("w-1", "w-2", "w-3"):
            self.engine.ingest(make_incident(
                ErrorCategory.KNOWLEDGE_GAP,
                "term:sku:ZX",
                source,
                {"term": "ZX", "source_doc": f"{source}.pdf"},
            ))
        proposal = self.store.list_prescriptions(status="proposed")[0]
        patch = self.engine.approve(
            proposal["id"],
            approver="human:warehouse",
            attribution={"canonical": "ZX-9", "definition": "demo", "kind": "sku"},
        )
        self.assertIn("ZX", self.store.view()["terms"])
        self.assertEqual(self.engine.rescan()["resolved"], 3)
        self.engine.rollback(patch["id"])
        self.assertNotIn("ZX", self.store.view()["terms"])
        group = self.store.get_group("term:sku:ZX")
        self.assertEqual(group["status"], "rolled_back")
        self.assertEqual(self.engine.rescan()["resolved"], 0)

    def test_rollback_must_follow_patch_order(self) -> None:
        for source in ("a", "b", "c"):
            self.engine.ingest(self._alias_incident(source))
        alias_patch = self.store.latest_active_patch()
        for source in ("r-1", "r-2", "r-3"):
            self.engine.ingest(make_incident(
                ErrorCategory.RULE_GAP,
                "rule:later",
                source,
                {"rule_name": "后补规则", "rule_action": "ask"},
            ))
        proposal = self.store.list_prescriptions(status="proposed")[0]
        rule_patch = self.engine.approve(proposal["id"], approver="human:o")
        with self.assertRaises(RollbackOrderError):
            self.engine.rollback(alias_patch)
        self.engine.rollback(rule_patch["id"])
        self.assertFalse(self.store.view()["operational_rules"])

    def test_guardrail_domain_rejects_runtime_patch(self) -> None:
        with self.assertRaises(GuardrailViolation):
            self.store.commit(
                actions=[{
                    "op": "upsert_rule",
                    "rule": {"id": "evil", "name": "bad", "enabled": True},
                    "target": "guardrails",
                }],
                summary="illegal",
            )

    def test_patch_has_before_after_snapshot_and_survives_reload(self) -> None:
        for source in ("a", "b", "c"):
            self.engine.ingest(self._alias_incident(source))
        patch_id = self.store.latest_active_patch()
        assert patch_id
        patch_path = self.store.patch_dir / f"{patch_id}.json"
        self.assertTrue(patch_path.exists())
        patch = self.store.rollback(patch_id)
        self.assertEqual(patch["status"], "rolled_back")
        reloaded = RhemStore(self.root)
        self.assertNotIn("shanghai", reloaded.view()["alias_rules"])


if __name__ == "__main__":
    unittest.main()