# -*- coding: utf-8 -*-
"""Tests for opt-in adaptive gating and its patch audit trail."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from rhem.engine import AdaptiveGatePolicy, LearningEngine
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def alias_incident(
    source: str,
    occurred_at: str,
    *,
    family: str = "slot:city:x",
    gate: dict | None = None,
) -> Incident:
    evidence = {
        "alias": "X",
        "canonical": "x",
    }
    if gate:
        evidence["gate"] = gate
    return Incident(
        category=ErrorCategory.RECOGNITION,
        family=family,
        message=f"alias failure from {source}",
        source=source,
        evidence=evidence,
        expected="x",
        actual="X",
        occurred_at=occurred_at,
    )


def rule_incident(source: str, occurred_at: str) -> Incident:
    return Incident(
        category=ErrorCategory.RULE_GAP,
        family="rule:adaptive:confirm",
        message=f"missing rule from {source}",
        source=source,
        evidence={
            "rule_name": "Adaptive confirmation",
            "rule_reason": "Prevent irreversible action",
            "rule_condition": "action.irreversible == true",
            "rule_action": "ask for confirmation",
        },
        occurred_at=occurred_at,
    )


class AdaptiveGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        self.store = RhemStore(self.root)
        self.engine = LearningEngine(
            self.store,
            gate=AdaptiveGatePolicy(),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fast_recurrence_lowers_threshold_to_two(self) -> None:
        first = self.engine.ingest(alias_incident(
            "source-a",
            "2026-09-10T00:00:00Z",
        ))
        second = self.engine.ingest(alias_incident(
            "source-b",
            "2026-09-10T01:00:00Z",
        ))

        self.assertEqual(first["event"], "waiting")
        self.assertEqual(
            first["gate_decision"]["effective_min_occurrences"],
            3,
        )
        self.assertEqual(second["event"], "auto_applied")
        self.assertEqual(
            second["gate_decision"]["effective_min_occurrences"],
            2,
        )
        self.assertEqual(second["gate_decision"]["reason"], "fast_recurrence")
        self.assertIn("x", self.store.view()["alias_rules"])

        patch_id = self.store.latest_active_patch()
        assert patch_id is not None
        patch_meta = self.store.view()["patch_meta"][patch_id]
        self.assertEqual(
            patch_meta["gate_decision"]["effective_min_occurrences"],
            2,
        )
        self.assertEqual(
            patch_meta["gate_decision"]["mode"],
            "adaptive",
        )

    def test_quiet_recurrence_keeps_base_threshold(self) -> None:
        self.engine.ingest(alias_incident(
            "source-a",
            "2026-08-01T00:00:00Z",
        ))
        second = self.engine.ingest(alias_incident(
            "source-b",
            "2026-09-10T00:00:00Z",
        ))

        self.assertEqual(second["event"], "waiting")
        self.assertEqual(
            second["gate_decision"]["effective_min_occurrences"],
            3,
        )
        self.assertEqual(
            second["gate_decision"]["recent_occurrences"],
            1,
        )
        self.assertNotIn("x", self.store.view()["alias_rules"])

    def test_legacy_records_do_not_infer_occurrence_times(self) -> None:
        decision = AdaptiveGatePolicy().decide({
            "occurrences": 3,
            "sources": ["a", "b"],
            "first_seen": "2026-01-01T00:00:00Z",
            "last_seen": "2026-01-02T00:00:00Z",
        }, now="2026-01-02T00:00:00Z")

        self.assertEqual(decision.recent_occurrences, 0)
        self.assertEqual(decision.sensitivity, 0.0)
        self.assertEqual(decision.effective_min_occurrences, 3)

    def test_risk_lock_does_not_overwrite_recurrence_sensitivity(self) -> None:
        decision = AdaptiveGatePolicy().decide({
            "occurrences": 5,
            "sources": ["a", "b", "c", "d", "e"],
            "occurrence_times": [
                "2026-01-01T00:00:00Z",
                "2026-01-10T00:00:00Z",
                "2026-01-20T00:00:00Z",
                "2026-01-30T00:00:00Z",
                "2026-02-09T00:00:00Z",
            ],
            "gate_flags": {"high_risk": True},
        }, now="2026-02-09T00:00:00Z")

        self.assertEqual(decision.lock_reason, "high_risk")
        self.assertEqual(decision.recent_occurrences, 1)
        self.assertAlmostEqual(decision.sensitivity, 0.5)
        self.assertEqual(decision.effective_min_occurrences, 5)

    def test_high_risk_locks_high_and_requires_human(self) -> None:
        for index in range(5):
            outcome = self.engine.ingest(alias_incident(
                f"source-{index}",
                f"2026-09-10T0{index}:00:00Z",
                family="slot:risk",
                gate={"high_risk": True},
            ))

        self.assertEqual(outcome["event"], "needs_human_gate")
        decision = outcome["gate_decision"]
        self.assertEqual(decision["effective_min_occurrences"], 5)
        self.assertEqual(decision["reason"], "high_risk")
        self.assertEqual(decision["lock_reason"], "high_risk")
        self.assertTrue(decision["requires_human"])
        self.assertIsNone(self.store.latest_active_patch())
        self.assertEqual(
            self.store.get_group("slot:risk")["status"],
            "needs_human",
        )

    def test_recheck_failed_forces_human_attribution(self) -> None:
        for index in range(5):
            outcome = self.engine.ingest(alias_incident(
                f"recheck-{index}",
                f"2026-09-10T0{index}:00:00Z",
                family="slot:recheck",
                gate={"recheck_failed": True},
            ))

        self.assertEqual(outcome["event"], "needs_human_gate")
        self.assertEqual(
            outcome["gate_decision"]["reason"],
            "recheck_failed",
        )
        self.assertTrue(outcome["gate_decision"]["requires_human"])
        self.assertIsNone(self.store.latest_active_patch())

    def test_legacy_gate_without_decide_remains_supported(self) -> None:
        class LegacyGate:
            min_occurrences = 2
            min_sources = 1

            def reached(self, group: dict) -> bool:
                return int(group.get("occurrences", 0)) >= 2

        engine = LearningEngine(self.store, gate=LegacyGate())
        engine.ingest(alias_incident(
            "legacy-a",
            "2026-09-10T00:00:00Z",
            family="slot:legacy",
        ))
        outcome = engine.ingest(alias_incident(
            "legacy-b",
            "2026-09-10T01:00:00Z",
            family="slot:legacy",
        ))

        self.assertEqual(outcome["event"], "auto_applied")
        self.assertEqual(outcome["gate_decision"]["mode"], "legacy")
        self.assertTrue(outcome["gate_decision"]["allowed"])

    def test_rule_proposal_and_patch_keep_gate_decision(self) -> None:
        self.engine.ingest(rule_incident(
            "rule-source-a",
            "2026-09-10T00:00:00Z",
        ))
        outcome = self.engine.ingest(rule_incident(
            "rule-source-b",
            "2026-09-10T01:00:00Z",
        ))

        self.assertEqual(outcome["event"], "proposal_created")
        proposal = self.store.list_prescriptions(status="proposed")[0]
        self.assertEqual(
            proposal["gate_decision"]["effective_min_occurrences"],
            2,
        )
        patch = self.engine.approve(
            proposal["id"],
            approver="human:owner",
        )
        patch_meta = self.store.view()["patch_meta"][patch["id"]]
        self.assertEqual(
            patch_meta["gate_decision"]["reason"],
            "fast_recurrence",
        )

    def test_gate_decisions_are_written_to_audit_log(self) -> None:
        self.engine.ingest(alias_incident(
            "log-source-a",
            "2026-09-10T00:00:00Z",
        ))
        self.engine.ingest(alias_incident(
            "log-source-b",
            "2026-09-10T01:00:00Z",
        ))

        rows = [
            json.loads(line)
            for line in self.store.event_file.read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        gate_rows = [
            row for row in rows
            if row.get("kind") == "gate_evaluated"
        ]
        self.assertEqual(len(gate_rows), 2)
        self.assertEqual(
            gate_rows[-1]["decision"]["reason"],
            "fast_recurrence",
        )

    def test_incident_round_trip_preserves_id_and_timestamp(self) -> None:
        original = alias_incident(
            "round-trip",
            "2026-09-10T01:00:00Z",
        )
        restored = Incident.from_dict(original.to_dict())

        self.assertEqual(restored.id, original.id)
        self.assertEqual(restored.occurred_at, original.occurred_at)
        self.assertEqual(restored.cluster_key, original.cluster_key)


if __name__ == "__main__":
    unittest.main()