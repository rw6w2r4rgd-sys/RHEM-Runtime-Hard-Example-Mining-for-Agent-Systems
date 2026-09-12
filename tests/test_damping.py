# -*- coding: utf-8 -*-
"""Tests for opt-in damping suppression and its patch-governed controls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.damping import DampingSuppressor
from rhem.engine import GatePolicy, LearningEngine
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def alias_incident(
    family: str,
    source: str,
    occurred_at: str,
    *,
    canonical: str = "x",
) -> Incident:
    return Incident(
        category=ErrorCategory.RECOGNITION,
        family=family,
        message=f"alias failure from {source}",
        source=source,
        evidence={"alias": canonical.upper(), "canonical": canonical},
        expected=canonical,
        actual=canonical.upper(),
        occurred_at=occurred_at,
    )


def rule_incident(family: str, source: str, occurred_at: str) -> Incident:
    return Incident(
        category=ErrorCategory.RULE_GAP,
        family=family,
        message=f"missing rule from {source}",
        source=source,
        evidence={
            "rule_name": "Damped rule",
            "rule_reason": "Stability test",
            "rule_condition": "enabled == true",
            "rule_action": "hold",
        },
        occurred_at=occurred_at,
    )


class DampingSuppressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_alias(self, canonical: str) -> None:
        self.store.commit(
            actions=[{
                "op": "add_alias",
                "canonical": canonical,
                "aliases": [canonical.upper()],
                "sources": ["seed"],
            }],
            summary=f"seed {canonical}",
        )

    def test_default_engine_keeps_previous_behavior(self) -> None:
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
        )
        outcome = engine.ingest(alias_incident(
            "slot:damping:default",
            "a",
            "2026-09-12T00:00:00Z",
        ))

        self.assertEqual(outcome["event"], "auto_applied")
        self.assertIsNone(outcome["damping_decision"])
        self.assertIn("x", self.store.view()["alias_rules"])

    def test_dead_zone_holds_until_minimum_count(self) -> None:
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            damping=DampingSuppressor(dead_zone_occurrences=2),
        )
        first = engine.ingest(alias_incident(
            "slot:damping:dead-zone",
            "a",
            "2026-09-12T00:00:00Z",
        ))
        self.assertEqual(first["event"], "damping_dead_zone")
        self.assertEqual(first["damping_decision"]["action"], "dead_zone")
        self.assertNotIn("x", self.store.view()["alias_rules"])
        second = engine.ingest(alias_incident(
            "slot:damping:dead-zone",
            "b",
            "2026-09-12T00:01:00Z",
        ))

        self.assertEqual(second["event"], "auto_applied")
        self.assertEqual(second["damping_decision"]["action"], "allow")

    def test_aba_oscillation_freezes_and_rolls_back(self) -> None:
        for canonical in ("x", "y", "x"):
            self._seed_alias(canonical)
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            damping=DampingSuppressor(
                dead_zone_occurrences=1,
                oscillation_cycles=1,
            ),
        )

        outcome = engine.ingest(alias_incident(
            "slot:damping:oscillation",
            "a",
            "2026-09-12T00:00:00Z",
        ))

        self.assertEqual(outcome["event"], "damping_frozen")
        self.assertEqual(
            outcome["damping_decision"]["action"],
            "freeze_oscillation",
        )
        self.assertEqual(outcome["damping_decision"]["oscillations"], 1)
        self.assertGreater(outcome["damping_decision"]["coefficient"], 0.2)
        control = self.store.view()["damping_state"]["controls"]["alias:x"]
        self.assertEqual(control["status"], "frozen")
        patch_id = outcome["patch_id"]
        assert patch_id is not None
        self.assertEqual(
            self.store.view()["patch_meta"][patch_id]["damping_plan"]["action"],
            "freeze_oscillation",
        )

        engine.rollback(patch_id)
        self.assertFalse(self.store.view()["damping_state"]["controls"])

    def test_cooldown_blocks_then_thaws_and_continues(self) -> None:
        for canonical in ("x", "y", "x"):
            self._seed_alias(canonical)
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            damping=DampingSuppressor(
                dead_zone_occurrences=1,
                oscillation_cycles=1,
            ),
        )
        frozen = engine.ingest(alias_incident(
            "slot:damping:cooldown",
            "a",
            "2026-09-12T00:00:00Z",
        ))
        blocked = engine.ingest(alias_incident(
            "slot:damping:cooldown",
            "b",
            "2026-09-12T01:00:00Z",
        ))
        thawed = engine.ingest(alias_incident(
            "slot:damping:cooldown",
            "c",
            "2026-09-16T00:00:00Z",
        ))

        self.assertEqual(frozen["event"], "damping_frozen")
        self.assertEqual(blocked["event"], "damping_cooldown")
        self.assertEqual(
            blocked["damping_decision"]["reason"],
            "damping_cooldown_active",
        )
        self.assertEqual(thawed["event"], "auto_applied")
        self.assertEqual(thawed["damping_decision"]["action"], "thaw")
        self.assertFalse(self.store.view()["damping_state"]["controls"])
        self.assertIn("x", self.store.view()["alias_rules"])

    def test_regret_rate_downweights_rule_and_rolls_back(self) -> None:
        first_rule = {
            "id": "rule:first",
            "name": "First rule",
            "family": "rule:damping:regret",
            "enabled": True,
        }
        first_patch = self.store.commit(
            actions=[{"op": "upsert_rule", "rule": first_rule}],
            summary="seed first rule",
        )
        self.store.rollback(first_patch["id"])
        second_rule = {
            "id": "rule:second",
            "name": "Second rule",
            "family": "rule:damping:regret",
            "enabled": True,
        }
        self.store.commit(
            actions=[{"op": "upsert_rule", "rule": second_rule}],
            summary="seed second rule",
        )
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            damping=DampingSuppressor(
                dead_zone_occurrences=1,
                regret_min_events=1,
                oscillation_cycles=99,
            ),
        )

        outcome = engine.ingest(rule_incident(
            "rule:damping:regret",
            "a",
            "2026-09-12T00:00:00Z",
        ))

        self.assertEqual(outcome["event"], "damping_frozen")
        self.assertEqual(
            outcome["damping_decision"]["action"],
            "freeze_regret",
        )
        self.assertEqual(outcome["damping_decision"]["regret_events"], 1)
        rule = self.store.view()["operational_rules"]["rule:second"]
        self.assertFalse(rule["enabled"])
        self.assertEqual(rule["distillation"]["status"], "downweighted")

        patch_id = outcome["patch_id"]
        assert patch_id is not None
        engine.rollback(patch_id)
        restored = self.store.view()["operational_rules"]["rule:second"]
        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["distillation"]["status"], "active")
        self.assertFalse(self.store.view()["damping_state"]["controls"])


if __name__ == "__main__":
    unittest.main()
