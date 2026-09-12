# -*- coding: utf-8 -*-
"""Tests for pre-admission fractionation and in-library re-distillation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.distillation import HardExampleDistiller
from rhem.engine import GatePolicy, LearningEngine
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def alias_incident(source: str, occurred_at: str) -> Incident:
    return Incident(
        category=ErrorCategory.RECOGNITION,
        family="slot:distill:city",
        message=f"alias failure from {source}",
        source=source,
        evidence={"alias": "X", "canonical": "x"},
        expected="x",
        actual="X",
        occurred_at=occurred_at,
    )


def rule_incident(source: str) -> Incident:
    return Incident(
        category=ErrorCategory.RULE_GAP,
        family="rule:distill:confirm",
        message=f"missing rule from {source}",
        source=source,
        evidence={
            "rule_name": "Confirm distillation action",
            "rule_reason": "Avoid unsafe action",
            "rule_condition": "action.unsafe == true",
            "rule_action": "ask for confirmation",
        },
    )


class DistillationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_engine_keeps_previous_behavior(self) -> None:
        engine = LearningEngine(self.store)
        for source in ("same", "same", "same"):
            outcome = engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{len(self.store.list_incidents())}:00:00Z",
            ))

        self.assertEqual(outcome["event"], "auto_applied")
        self.assertIn("x", self.store.view()["alias_rules"])
        self.assertIsNone(outcome["distillation_decision"])

    def test_single_source_is_held_by_pre_admission(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        for index in range(3):
            outcome = engine.ingest(alias_incident(
                "single-observer",
                f"2026-09-10T0{index}:00:00Z",
            ))

        self.assertEqual(outcome["event"], "distillation_hold")
        self.assertEqual(outcome["distillation_decision"]["tier"], "medium")
        self.assertNotIn("x", self.store.view()["alias_rules"])
        self.assertEqual(
            self.store.get_group("slot:distill:city")["status"],
            "pending",
        )

    def test_cross_source_recurrence_is_high_value(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        outcomes = [
            engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{index}:00:00Z",
            ))
            for index, source in enumerate(("a", "b", "c"))
        ]

        self.assertEqual(outcomes[-1]["event"], "auto_applied")
        self.assertEqual(
            outcomes[-1]["distillation_decision"]["tier"],
            "high",
        )
        self.assertIn("x", self.store.view()["alias_rules"])

    def test_medium_value_waits_then_promotes(self) -> None:
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            distiller=HardExampleDistiller(),
        )
        first = engine.ingest(alias_incident(
            "a",
            "2026-09-10T00:00:00Z",
        ))
        second = engine.ingest(alias_incident(
            "a",
            "2026-09-10T01:00:00Z",
        ))
        third = engine.ingest(alias_incident(
            "b",
            "2026-09-10T02:00:00Z",
        ))

        self.assertEqual(first["event"], "distillation_hold")
        self.assertEqual(first["distillation_decision"]["tier"], "low")
        self.assertEqual(second["event"], "distillation_hold")
        self.assertEqual(second["distillation_decision"]["tier"], "medium")
        self.assertEqual(third["event"], "auto_applied")
        self.assertEqual(third["distillation_decision"]["tier"], "high")

    def test_multi_scenario_feedback_promotes_core_and_rolls_back(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        for index, source in enumerate(("a", "b", "c")):
            engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{index}:00:00Z",
            ))

        for scenario in ("checkout", "support", "checkout"):
            self.store.record_feedback(
                "alias",
                "x",
                scenario,
                resolved=True,
            )

        plan = engine.redistill(now="2026-09-12T00:00:00Z")
        self.assertEqual(plan["summary"]["core"], 1)
        self.assertFalse(plan["applied"])

        applied = engine.redistill(
            apply=True,
            actor="human:owner",
            now="2026-09-12T00:00:00Z",
        )
        rule = self.store.view()["alias_rules"]["x"]
        self.assertEqual(rule["distillation"]["quality_tier"], "core")
        patch_id = applied["patch_id"]
        assert patch_id is not None

        engine.rollback(patch_id)
        restored = self.store.view()["alias_rules"]["x"]
        self.assertEqual(
            restored["distillation"]["quality_tier"],
            "standard",
        )

    def test_failed_feedback_downweights_rule_and_rolls_back(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        for source in ("a", "b", "c"):
            engine.ingest(rule_incident(source))
        proposal = self.store.list_prescriptions(status="proposed")[0]
        engine.approve(proposal["id"], approver="human:owner")
        rule_id = next(iter(self.store.view()["operational_rules"]))

        self.store.record_feedback(
            "rule",
            rule_id,
            "production",
            resolved=False,
        )
        plan = engine.redistill(now="2026-09-12T00:00:00Z")
        self.assertEqual(plan["summary"]["downweight"], 1)
        applied = engine.redistill(
            apply=True,
            actor="human:owner",
            now="2026-09-12T00:00:00Z",
        )

        rule = self.store.view()["operational_rules"][rule_id]
        self.assertFalse(rule["enabled"])
        self.assertEqual(
            rule["distillation"]["quality_tier"],
            "downweighted",
        )
        engine.rollback(applied["patch_id"])
        restored = self.store.view()["operational_rules"][rule_id]
        self.assertTrue(restored["enabled"])

        self.store.record_feedback(
            "rule",
            rule_id,
            "production",
            resolved=True,
        )
        recovered = engine.redistill(now="2026-09-12T00:00:00Z")
        self.assertEqual(recovered["summary"]["downweight"], 0)
        self.assertEqual(
            self.store.view()["operational_rules"][rule_id]
            ["distillation"]["consecutive_misses"],
            0,
        )

    def test_explicit_superseded_record_is_downweighted(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        for index, source in enumerate(("a", "b", "c")):
            engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{index}:00:00Z",
            ))
        self.store.mark_superseded("alias", "x", "x-v2")

        plan = engine.redistill(now="2026-09-12T00:00:00Z")
        self.assertEqual(plan["summary"]["downweight"], 1)
        self.assertEqual(plan["items"][0]["reason"], "superseded")

    def test_stale_record_can_be_culled_and_restored(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(
                stale_after_days=0.0,
                cull_after_days=0.0,
            ),
        )
        for index, source in enumerate(("a", "b", "c")):
            engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{index}:00:00Z",
            ))

        plan = engine.redistill(now="2026-09-12T00:00:00Z")
        self.assertEqual(plan["summary"]["cull"], 1)
        applied = engine.redistill(
            apply=True,
            actor="human:owner",
            now="2026-09-12T00:00:00Z",
        )
        self.assertNotIn("x", self.store.view()["alias_rules"])

        engine.rollback(applied["patch_id"])
        self.assertIn("x", self.store.view()["alias_rules"])

    def test_rescan_can_fill_the_feedback_ledger(self) -> None:
        engine = LearningEngine(
            self.store,
            distiller=HardExampleDistiller(),
        )
        for index, source in enumerate(("a", "b", "c")):
            engine.ingest(alias_incident(
                source,
                f"2026-09-10T0{index}:00:00Z",
            ))

        result = engine.rescan(record_feedback=True)
        self.assertEqual(result["resolved"], 3)
        metadata = self.store.view()["alias_rules"]["x"]["distillation"]
        self.assertEqual(metadata["hits"], 3)
        self.assertEqual(len(metadata["scenario_sources"]), 3)


if __name__ == "__main__":
    unittest.main()
