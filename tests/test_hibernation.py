# -*- coding: utf-8 -*-
"""Tests for opt-in hibernation, wake, and human-approved recycling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.engine import GatePolicy, LearningEngine
from rhem.hibernation import HibernationManager
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def alias_incident(
    source: str,
    occurred_at: str,
    *,
    canonical: str = "x",
) -> Incident:
    return Incident(
        category=ErrorCategory.RECOGNITION,
        family="slot:hibernation:alias",
        message=f"alias failure from {source}",
        source=source,
        evidence={"alias": canonical.upper(), "canonical": canonical},
        expected=canonical,
        actual=canonical.upper(),
        occurred_at=occurred_at,
    )


def rule_incident(source: str, occurred_at: str) -> Incident:
    return Incident(
        category=ErrorCategory.RULE_GAP,
        family="rule:hibernation:test",
        message=f"missing rule from {source}",
        source=source,
        evidence={
            "rule_name": "Hibernation rule",
            "rule_reason": "Test lifecycle",
            "rule_condition": "enabled == true",
            "rule_action": "hold",
        },
        occurred_at=occurred_at,
    )


class HibernationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_alias(self, canonical: str = "x") -> None:
        self.store.commit(
            actions=[{
                "op": "add_alias",
                "canonical": canonical,
                "aliases": [canonical.upper()],
                "sources": ["seed"],
            }],
            summary=f"seed alias {canonical}",
        )

    def _seed_rule(self, enabled: bool = True) -> str:
        rule_id = "rule:hibernation:test"
        self.store.commit(
            actions=[{
                "op": "upsert_rule",
                "rule": {
                    "id": rule_id,
                    "name": "Hibernation rule",
                    "family": "rule:hibernation:test",
                    "enabled": enabled,
                },
            }],
            summary="seed rule",
        )
        return rule_id

    def test_default_engine_does_not_wake_or_hibernate(self) -> None:
        self._seed_alias()
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
        )

        outcome = engine.ingest(alias_incident(
            "default-source",
            "2026-09-12T00:00:00Z",
        ))

        self.assertIsNone(outcome["hibernation_wake"])
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "active",
        )

    def test_observe_and_hibernate_are_patch_governed_and_rollbackable(self) -> None:
        self._seed_alias()
        engine = LearningEngine(
            self.store,
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
            ),
        )

        observed = engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        self.assertEqual(observed["summary"]["observe"], 1)
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "observing",
        )
        observe_patch = observed["patch_id"]
        assert observe_patch is not None
        self.assertEqual(
            self.store.view()["patch_meta"][observe_patch]
            ["hibernation_plan"]["summary"]["observe"],
            1,
        )

        hibernated = engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        self.assertEqual(hibernated["summary"]["hibernate"], 1)
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "hibernating",
        )
        hibernate_patch = hibernated["patch_id"]
        assert hibernate_patch is not None

        engine.rollback(hibernate_patch)
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "observing",
        )
        engine.rollback(observe_patch)
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "active",
        )

    def test_hibernating_rule_is_disabled_and_recurrence_wakes_it(self) -> None:
        rule_id = self._seed_rule()
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=3),
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
            ),
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        self.assertFalse(self.store.view()["operational_rules"][rule_id]["enabled"])

        outcome = engine.ingest(rule_incident(
            "recurrence-source",
            "2026-09-12T01:00:00Z",
        ))

        self.assertIsNotNone(outcome["hibernation_wake"])
        self.assertEqual(
            self.store.view()["operational_rules"][rule_id]["hibernation"]
            ["status"],
            "active",
        )
        self.assertTrue(self.store.view()["operational_rules"][rule_id]["enabled"])
        wake_patch = outcome["hibernation_wake"]["patch_id"]
        assert wake_patch is not None
        engine.rollback(wake_patch)
        self.assertFalse(self.store.view()["operational_rules"][rule_id]["enabled"])
        self.assertEqual(
            self.store.view()["operational_rules"][rule_id]["hibernation"]
            ["status"],
            "hibernating",
        )

    def test_wake_preserves_manually_disabled_rule(self) -> None:
        rule_id = self._seed_rule(enabled=False)
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=3),
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
            ),
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        self.assertFalse(
            self.store.view()["operational_rules"][rule_id]["enabled"]
        )

        outcome = engine.ingest(rule_incident(
            "manual-disabled-recurrence",
            "2026-09-12T01:00:00Z",
        ))

        self.assertIsNotNone(outcome["hibernation_wake"])
        rule = self.store.view()["operational_rules"][rule_id]
        self.assertEqual(rule["hibernation"]["status"], "active")
        self.assertFalse(rule["enabled"])
        self.assertFalse(rule["hibernation"]["disabled_by_hibernation"])

    def test_hibernating_alias_is_not_resolved(self) -> None:
        engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
        )
        engine.ingest(alias_incident(
            "seed-source",
            "2026-09-12T00:00:00Z",
        ))
        self.assertEqual(engine.rescan()["resolved"], 1)

        hibernating_engine = LearningEngine(
            self.store,
            gate=GatePolicy(min_occurrences=1),
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
            ),
        )
        hibernating_engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        hibernating_engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )

        self.assertEqual(hibernating_engine.rescan()["resolved"], 0)

    def test_recycle_requires_human_approval_and_restores_on_rollback(self) -> None:
        self._seed_alias()
        engine = LearningEngine(
            self.store,
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
                recycle_after_days=0,
            ),
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        plan = engine.hibernate(now="2026-09-12T00:00:00Z")
        self.assertEqual(plan["summary"]["recycle_candidate"], 1)
        self.assertEqual(len(plan["actions"]), 0)

        recycled = engine.recycle_hibernated(
            "alias",
            "x",
            approver="human:owner",
            now="2026-09-12T00:00:00Z",
        )
        self.assertNotIn("x", self.store.view()["alias_rules"])
        self.assertEqual(
            self.store.view()["patch_meta"][recycled["id"]]
            ["hibernation_plan"]["action"]["op"],
            "delete_alias",
        )

        engine.rollback(recycled["id"])
        self.assertIn("x", self.store.view()["alias_rules"])
        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "hibernating",
        )

    def test_manual_wake_is_auditable(self) -> None:
        self._seed_alias()
        engine = LearningEngine(
            self.store,
            hibernation=HibernationManager(
                observe_after_days=0,
                hibernate_after_days=0,
            ),
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        engine.hibernate(
            apply=True,
            actor="rhem:hibernation",
            now="2026-09-12T00:00:00Z",
        )
        patch = engine.wake_hibernated(
            "alias",
            "x",
            reason="人工确认环境回流",
            approver="owner",
            now="2026-09-12T02:00:00Z",
        )

        self.assertEqual(
            self.store.view()["alias_rules"]["x"]["hibernation"]["status"],
            "active",
        )
        self.assertEqual(patch["actor"], "human:owner")
        self.assertEqual(
            self.store.view()["patch_meta"][patch["id"]]
            ["hibernation_plan"]["decision"]["reason"],
            "人工确认环境回流",
        )


if __name__ == "__main__":
    unittest.main()
