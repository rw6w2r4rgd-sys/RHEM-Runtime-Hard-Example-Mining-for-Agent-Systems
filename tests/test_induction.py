# -*- coding: utf-8 -*-
"""Tests for distillate induction and human-governed feedback."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.engine import LearningEngine
from rhem.induction import DistillateInducer
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def core_rule(
    rule_id: str,
    condition: str,
    action: str,
    *,
    family: str,
    structure_key: str,
) -> dict:
    return {
        "id": rule_id,
        "name": rule_id,
        "description": f"rule {rule_id}",
        "condition": condition,
        "action": action,
        "family": family,
        "structure_key": structure_key,
        "sources": ["seed"],
        "enabled": True,
        "created_at": "2026-09-12T00:00:00Z",
    }


class InductionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)
        self.engine = LearningEngine(
            self.store,
            inducer=DistillateInducer(),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_core_rule(self, record: dict) -> None:
        self.store.commit(
            actions=[
                {"op": "upsert_rule", "rule": record},
                {
                    "op": "distill_rule",
                    "rule_id": record["id"],
                    "quality_tier": "core",
                    "status": "active",
                    "last_evaluated_at": "2026-09-12T00:00:00Z",
                    "last_evaluation_reason": "test_core",
                },
            ],
            summary=f"seed core rule {record['id']}",
        )

    def test_no_core_records_produce_no_findings(self) -> None:
        plan = self.engine.induct(
            now="2026-09-12T00:00:00Z",
            persist=False,
        )

        self.assertEqual(plan["summary"]["rule_template"], 0)
        self.assertEqual(plan["summary"]["structural_weakness"], 0)
        self.assertEqual(plan["summary"]["hazard_prediction"], 0)
        self.assertFalse(plan["automatic_changes"])

    def test_rule_template_requires_human_approval_and_rolls_back(self) -> None:
        self._seed_core_rule(core_rule(
            "rule_date_a",
            "slot.date is null",
            "ask_for_confirmation",
            family="rule:date:checkout",
            structure_key="slot:date",
        ))
        self._seed_core_rule(core_rule(
            "rule_date_b",
            "slot.date has no anchor",
            "ask_for_confirmation",
            family="rule:date:support",
            structure_key="slot:date",
        ))

        plan = self.engine.induct(now="2026-09-12T00:00:00Z")
        findings = [
            item for item in plan["findings"]
            if item["kind"] == "rule_template"
        ]
        self.assertEqual(len(findings), 1)
        finding_id = findings[0]["finding_id"]

        stored = self.store.get_induction_finding(finding_id)
        suggested_rule_id = stored["suggested"]["rule"]["id"]
        self.assertEqual(stored["status"], "proposed")
        self.assertNotIn(
            suggested_rule_id,
            self.store.view()["operational_rules"],
        )

        patch = self.engine.approve_induction(
            finding_id,
            approver="human:owner",
        )
        self.assertIn(
            suggested_rule_id,
            self.store.view()["operational_rules"],
        )
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "applied",
        )

        self.engine.rollback(patch["id"])
        self.assertNotIn(
            suggested_rule_id,
            self.store.view()["operational_rules"],
        )
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "proposed",
        )

    def test_structural_weakness_is_advisory_and_rolls_back(self) -> None:
        alias = {
            "canonical": "date",
            "aliases": ["date-raw"],
            "family": "slot:date:checkout",
            "structure_key": "slot:date",
            "sources": ["checkout"],
            "created_at": "2026-09-12T00:00:00Z",
        }
        rule = core_rule(
            "rule_date_structural",
            "slot.date is null",
            "ask_for_confirmation",
            family="rule:date:checkout",
            structure_key="slot:date",
        )
        term = {
            "term": "DATE-9",
            "canonical": "date-v9",
            "definition": "date structure marker",
            "kind": "structure_marker",
            "family": "term:date:marker",
            "structure_key": "slot:date",
            "created_at": "2026-09-12T00:00:00Z",
        }
        self.store.commit(
            actions=[
                {"op": "add_alias", **alias},
                {
                    "op": "distill_alias",
                    "canonical": "date",
                    "quality_tier": "core",
                    "status": "active",
                },
                {"op": "upsert_rule", "rule": rule},
                {
                    "op": "distill_rule",
                    "rule_id": rule["id"],
                    "quality_tier": "core",
                    "status": "active",
                },
                {"op": "add_term", "term": term},
                {
                    "op": "distill_term",
                    "term": "DATE-9",
                    "quality_tier": "core",
                    "status": "active",
                },
            ],
            summary="seed structural evidence",
        )

        plan = self.engine.induct(now="2026-09-12T00:00:00Z")
        findings = [
            item for item in plan["findings"]
            if item["kind"] == "structural_weakness"
        ]
        self.assertEqual(len(findings), 1)
        finding_id = findings[0]["finding_id"]
        rule_count = len(self.store.view()["operational_rules"])

        patch = self.engine.approve_induction(
            finding_id,
            approver="human:owner",
        )
        self.assertEqual(
            len(self.store.view()["operational_rules"]),
            rule_count,
        )
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "accepted",
        )

        self.engine.rollback(patch["id"])
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "proposed",
        )

    def test_reject_induction_is_patch_governed_and_rolls_back(self) -> None:
        self._seed_core_rule(core_rule(
            "rule_one",
            "condition one",
            "same_action",
            family="rule:one",
            structure_key="slot:shared",
        ))
        self._seed_core_rule(core_rule(
            "rule_two",
            "condition two",
            "same_action",
            family="rule:two",
            structure_key="slot:shared",
        ))
        plan = self.engine.induct(now="2026-09-12T00:00:00Z")
        finding_id = plan["findings"][0]["finding_id"]

        patch = self.engine.reject_induction(
            finding_id,
            reason="condition boundary is unclear",
            approver="human:owner",
        )
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "rejected",
        )

        self.engine.rollback(patch["id"])
        self.assertEqual(
            self.store.get_induction_finding(finding_id)["status"],
            "proposed",
        )

    def test_pending_finding_is_reused(self) -> None:
        self._seed_core_rule(core_rule(
            "rule_a",
            "condition a",
            "same_action",
            family="rule:a",
            structure_key="slot:shared",
        ))
        self._seed_core_rule(core_rule(
            "rule_b",
            "condition b",
            "same_action",
            family="rule:b",
            structure_key="slot:shared",
        ))

        first = self.engine.induct(now="2026-09-12T00:00:00Z")
        second = self.engine.induct(now="2026-09-12T01:00:00Z")

        first_id = first["findings"][0]["finding_id"]
        second_id = second["findings"][0]["finding_id"]
        self.assertEqual(first_id, second_id)
        self.assertTrue(second["findings"][0]["reused"])
        self.assertEqual(len(self.store.list_induction_findings()), 1)

    def test_hazard_prediction_uses_high_tier_pending_groups(self) -> None:
        rows = [
            Incident(
                category=ErrorCategory.PROCESS,
                family="process:graph:a",
                message="hazard a1",
                source="worker-a",
                evidence={},
            ),
            Incident(
                category=ErrorCategory.PROCESS,
                family="process:graph:a",
                message="hazard a2",
                source="worker-b",
                evidence={},
            ),
            Incident(
                category=ErrorCategory.PROCESS,
                family="process:graph:b",
                message="hazard b1",
                source="worker-c",
                evidence={},
            ),
        ]
        for row in rows:
            self.store.record_incident(row)
        for family in ("process:graph:a", "process:graph:b"):
            self.store.mark_distillation_decision(
                family,
                {
                    "tier": "high",
                    "reason": "test_high",
                    "signals": {},
                    "evaluated_at": "2026-09-12T00:00:00Z",
                },
            )
            self.store.mark_group(family, "needs_human")

        plan = self.engine.induct(
            now="2026-09-12T00:00:00Z",
            persist=False,
        )
        findings = [
            item for item in plan["findings"]
            if item["kind"] == "hazard_prediction"
        ]
        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0]["evidence"]["structure_key"],
            "process:graph",
        )
        self.assertFalse(plan["automatic_changes"])


if __name__ == "__main__":
    unittest.main()
