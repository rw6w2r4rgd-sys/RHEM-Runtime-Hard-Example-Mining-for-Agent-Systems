# -*- coding: utf-8 -*-
"""Tests for the BFS/DFS graph probe and cluster gate integration."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rhem.engine import LearningEngine
from rhem.graph import (
    GraphAnalyzer,
    GraphEdge,
    GraphNode,
    IncidentGraph,
    TraversalPolicy,
)
from rhem.models import ErrorCategory, Incident
from rhem.store import RhemStore


def process_graph(
    session_id: str,
    symptom_id: str,
    *,
    root_id: str = "root:shared_lock",
    confidence: float = 0.95,
) -> dict:
    return {
        "session_id": session_id,
        "cluster_key": root_id,
        "start_node": symptom_id,
        "nodes": [
            {"id": symptom_id, "kind": "error"},
            {"id": root_id, "kind": "root_cause", "ref_id": root_id},
            {"id": "tool:fetch_orders", "kind": "tool", "ref_id": "fetch_orders"},
        ],
        "edges": [
            {
                "source": symptom_id,
                "target": root_id,
                "relation": "caused_by",
                "confidence": confidence,
            },
            {
                "source": root_id,
                "target": "tool:fetch_orders",
                "relation": "runs_on",
                "confidence": 0.9,
            },
        ],
    }


def process_incident(
    family: str,
    source: str,
    graph: dict,
    *,
    fix: dict | None = None,
) -> Incident:
    return Incident(
        category=ErrorCategory.PROCESS,
        family=family,
        message=f"process failure from {source}",
        source=source,
        evidence={
            "fix": fix or {"max_retries": 2, "deadlock_detection_s": 3.0},
            "graph": graph,
        },
    )


class GraphTraversalTest(unittest.TestCase):
    def test_bfs_limits_scope_and_dfs_finds_upstream_root(self) -> None:
        graph = IncidentGraph(
            nodes=[
                GraphNode("symptom", "error"),
                GraphNode("root", "root_cause", ref_id="root:shared"),
                GraphNode("worker", "component", ref_id="worker-a"),
                GraphNode("far", "error"),
            ],
            edges=[
                GraphEdge("symptom", "root", "caused_by", 0.9),
                GraphEdge("symptom", "worker", "related_to", 0.9),
                GraphEdge("worker", "far", "related_to", 0.9),
            ],
        )
        policy = TraversalPolicy(max_bfs_depth=1, max_dfs_depth=4)

        scope = graph.bfs("symptom", policy)
        trace = graph.dfs_upstream("symptom", policy)

        self.assertEqual(scope.visited, ["symptom", "root", "worker"])
        self.assertNotIn("far", scope.visited)
        self.assertEqual(trace.path, ["symptom", "root"])
        self.assertEqual(trace.root_candidate, "root")

    def test_cycle_and_depth_limit_require_human_review(self) -> None:
        cycle_graph = IncidentGraph(
            nodes=[GraphNode("a", "error"), GraphNode("b", "root_cause")],
            edges=[
                GraphEdge("a", "b", "caused_by"),
                GraphEdge("b", "a", "caused_by"),
            ],
        )
        cycle = cycle_graph.dfs_upstream(
            "a",
            TraversalPolicy(max_dfs_depth=4),
        )
        self.assertTrue(cycle.cycle_detected)

        chain_graph = IncidentGraph(
            nodes=[
                GraphNode("a", "error"),
                GraphNode("b", "root_cause"),
                GraphNode("c", "root_cause"),
            ],
            edges=[
                GraphEdge("a", "b", "caused_by"),
                GraphEdge("b", "c", "caused_by"),
            ],
        )
        truncated = chain_graph.dfs_upstream(
            "a",
            TraversalPolicy(max_dfs_depth=2),
        )
        self.assertTrue(truncated.truncated)
        self.assertEqual(truncated.path, ["a", "b"])

    def test_analyzer_uses_root_metadata_as_cluster_key(self) -> None:
        incident = process_incident(
            "process:graph",
            "task:one",
            {
                "session_id": "session-1",
                "start_node": "symptom",
                "nodes": [
                    {"id": "symptom", "kind": "error"},
                    {
                        "id": "root",
                        "kind": "root_cause",
                        "metadata": {"cluster_key": "cluster:shared_lock"},
                    },
                ],
                "edges": [
                    {
                        "source": "symptom",
                        "target": "root",
                        "relation": "caused_by",
                        "confidence": 1.0,
                    }
                ],
            },
        )

        finding = GraphAnalyzer().analyze(incident)

        self.assertIsNotNone(finding)
        assert finding is not None
        self.assertEqual(finding.cluster_key, "cluster:shared_lock")
        self.assertEqual(finding.root_candidate, "root")
        self.assertEqual(finding.cause_path, ["symptom", "root"])
        self.assertFalse(finding.needs_human)


class GraphGateIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RhemStore(Path(self.tmp.name) / "store")
        self.engine = LearningEngine(self.store)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_same_root_in_one_session_counts_as_one_evidence(self) -> None:
        for index in range(3):
            outcome = self.engine.ingest(process_incident(
                f"process:symptom:{index}",
                f"task:{index}",
                process_graph("session-1", f"symptom-{index}"),
            ))
            self.assertEqual(outcome["event"], "waiting")
            self.assertEqual(outcome["occurrences"], 1)

        cluster = self.store.get_cluster("root:shared_lock")
        self.assertIsNotNone(cluster)
        assert cluster is not None
        self.assertEqual(cluster["occurrences"], 1)
        self.assertEqual(cluster["symptom_count"], 3)
        self.assertIsNone(self.store.latest_active_patch())

    def test_cross_session_evidence_applies_one_cluster_patch(self) -> None:
        cases = [
            ("process:symptom:a", "task:a", process_graph("session-1", "a")),
            ("process:symptom:b", "task:b", process_graph("session-1", "b")),
            ("process:symptom:c", "task:c", process_graph("session-1", "c")),
            ("process:symptom:d", "task:d", process_graph("session-2", "d")),
            ("process:symptom:e", "task:e", process_graph("session-3", "e")),
        ]
        outcomes = [
            self.engine.ingest(process_incident(family, source, graph))
            for family, source, graph in cases
        ]

        self.assertEqual(outcomes[-1]["event"], "structural_fix_applied")
        self.assertEqual(outcomes[-1]["occurrences"], 3)
        self.assertEqual(self.store.view()["process_settings"]["max_retries"], 2)
        cluster = self.store.get_cluster("root:shared_lock")
        assert cluster is not None
        self.assertEqual(cluster["status"], "applied")
        self.assertEqual(cluster["occurrences"], 3)
        self.assertEqual(cluster["symptom_count"], 5)
        self.assertTrue(all(
            self.store.get_group(family)["status"] == "applied"
            for family, _, _ in cases
        ))
        self.assertEqual(self.engine.rescan()["resolved"], 5)

        reloaded = RhemStore(self.store.root)
        reloaded_cluster = reloaded.get_cluster("root:shared_lock")
        assert reloaded_cluster is not None
        self.assertEqual(reloaded_cluster["status"], "applied")
        self.assertEqual(reloaded_cluster["evidence_keys"], [
            "root:shared_lock@session-1",
            "root:shared_lock@session-2",
            "root:shared_lock@session-3",
        ])

    def test_rule_proposal_and_rollback_sync_cluster_state(self) -> None:
        for index in range(3):
            incident = Incident(
                category=ErrorCategory.RULE_GAP,
                family=f"rule:graph:{index}",
                message=f"missing rule {index}",
                source=f"user:{index}",
                evidence={
                    "rule_name": "Require confirmation",
                    "rule_reason": "Avoid irreversible action",
                    "rule_condition": "action.irreversible == true",
                    "rule_action": "ask for confirmation",
                    "graph": process_graph(
                        f"rule-session-{index}",
                        f"rule-symptom-{index}",
                    ),
                },
            )
            outcome = self.engine.ingest(incident)

        self.assertEqual(outcome["event"], "proposal_created")
        proposal = self.store.list_prescriptions(status="proposed")[0]
        self.assertEqual(proposal["cluster_key"], "root:shared_lock")
        self.assertEqual(
            proposal["suggested"]["rule"]["graph_evidence"]["root_candidate"],
            "root:shared_lock",
        )

        patch = self.engine.approve(proposal["id"], approver="human:owner")
        cluster = self.store.get_cluster("root:shared_lock")
        assert cluster is not None
        self.assertEqual(cluster["status"], "applied")
        self.assertEqual(cluster["patch_id"], patch["id"])

        self.engine.rollback(patch["id"])
        cluster = self.store.get_cluster("root:shared_lock")
        assert cluster is not None
        self.assertEqual(cluster["status"], "rolled_back")
        self.assertEqual(cluster["patch_id"], patch["id"])

    def test_ambiguous_same_session_symptoms_share_one_evidence(self) -> None:
        outcomes = []
        for index in range(3):
            outcomes.append(self.engine.ingest(process_incident(
                f"process:low-confidence:{index}",
                f"task:low-confidence:{index}",
                process_graph(
                    "ambiguous-session",
                    f"ambiguous-symptom-{index}",
                    confidence=0.3,
                ),
            )))

        self.assertTrue(all(item["event"] == "waiting" for item in outcomes))
        self.assertTrue(all(item["occurrences"] == 1 for item in outcomes))
        self.assertTrue(all(
            item["graph_finding"]["root_candidate"] is None
            for item in outcomes
        ))
        cluster = self.store.get_cluster("root:shared_lock")
        assert cluster is not None
        self.assertEqual(cluster["occurrences"], 1)
        self.assertEqual(cluster["symptom_count"], 3)

    def test_ambiguous_graph_never_auto_applies(self) -> None:
        for index in range(3):
            outcome = self.engine.ingest(process_incident(
                f"process:cycle:{index}",
                f"task:cycle:{index}",
                process_graph(
                    f"cycle-session-{index}",
                    f"cycle-symptom-{index}",
                    confidence=0.3,
                ),
            ))

        self.assertEqual(outcome["event"], "needs_human_graph")
        self.assertIsNone(self.store.latest_active_patch())
        cluster = self.store.get_cluster("root:shared_lock")
        assert cluster is not None
        self.assertEqual(cluster["status"], "needs_human")
        self.assertTrue(outcome["graph_finding"]["low_confidence_edge"])
        self.assertIsNone(outcome["graph_finding"]["root_candidate"])


if __name__ == "__main__":
    unittest.main()