# -*- coding: utf-8 -*-
"""RHEM 图探针：BFS 圈定范围，DFS 追踪上游根因。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .models import Incident


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str
    ref_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GraphNode":
        return cls(
            id=str(data["id"]),
            kind=str(data.get("kind") or "unknown"),
            ref_id=data.get("ref_id"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    confidence: float = 1.0
    session_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GraphEdge":
        confidence = float(data.get("confidence", 1.0))
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError(f"edge confidence must be in [0, 1]: {confidence}")
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            relation=str(data.get("relation") or "related_to"),
            confidence=confidence,
            session_id=data.get("session_id"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class GraphFinding:
    cluster_key: str
    start_node: str
    session_id: Optional[str]
    scope_node_ids: List[str]
    related_incident_ids: List[str]
    affected_domains: List[str]
    root_candidate: Optional[str]
    cause_path: List[str]
    cycle_detected: bool = False
    truncated: bool = False
    low_confidence_edge: bool = False
    needs_human: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cluster_key": self.cluster_key,
            "start_node": self.start_node,
            "session_id": self.session_id,
            "scope_node_ids": list(self.scope_node_ids),
            "related_incident_ids": list(self.related_incident_ids),
            "affected_domains": list(self.affected_domains),
            "root_candidate": self.root_candidate,
            "cause_path": list(self.cause_path),
            "cycle_detected": self.cycle_detected,
            "truncated": self.truncated,
            "low_confidence_edge": self.low_confidence_edge,
            "needs_human": self.needs_human,
        }


@dataclass(frozen=True)
class TraversalPolicy:
    max_bfs_depth: int = 2
    max_dfs_depth: int = 8
    min_edge_confidence: float = 0.6
    upstream_relations: Tuple[str, ...] = ("caused_by", "depends_on")


@dataclass
class _ScopeScan:
    visited: List[str]
    levels: Dict[str, int]
    related_incident_ids: List[str]
    affected_domains: List[str]
    low_confidence_edge: bool = False


@dataclass
class _TraceScan:
    root_candidate: Optional[str]
    path: List[str]
    cycle_detected: bool
    truncated: bool
    low_confidence_edge: bool = False


class IncidentGraph:
    """从 Incident.evidence["graph"] 构建的轻量有向图。"""

    def __init__(
        self,
        nodes: Iterable[GraphNode] = (),
        edges: Iterable[GraphEdge] = (),
    ) -> None:
        self.nodes: Dict[str, GraphNode] = {node.id: node for node in nodes}
        self.edges: List[GraphEdge] = list(edges)
        self._out: Dict[str, List[GraphEdge]] = defaultdict(list)
        self._undirected: Dict[str, List[GraphEdge]] = defaultdict(list)
        for edge in self.edges:
            if edge.source not in self.nodes or edge.target not in self.nodes:
                raise ValueError(
                    f"edge endpoints must exist as nodes: {edge.source} -> {edge.target}"
                )
            self._out[edge.source].append(edge)
            self._undirected[edge.source].append(edge)
            self._undirected[edge.target].append(edge)

    @classmethod
    def from_evidence(cls, incident: Incident) -> Optional["IncidentGraph"]:
        data = incident.evidence.get("graph")
        if not isinstance(data, dict):
            return None
        node_rows = data.get("nodes") or []
        edge_rows = data.get("edges") or []
        if not node_rows:
            return None
        return cls(
            nodes=[GraphNode.from_dict(row) for row in node_rows],
            edges=[GraphEdge.from_dict(row) for row in edge_rows],
        )

    def start_node(self, incident: Incident) -> Optional[str]:
        graph_data = incident.evidence.get("graph") or {}
        start = graph_data.get("start_node")
        if start and start in self.nodes:
            return str(start)
        for node in self.nodes.values():
            if node.kind in {"incident", "error", "failure"}:
                return node.id
        return next(iter(self.nodes), None)

    def session_id(self, incident: Incident) -> Optional[str]:
        graph_data = incident.evidence.get("graph") or {}
        session = graph_data.get("session_id") or incident.evidence.get("session_id")
        return str(session) if session else None

    def cluster_key(self, incident: Incident) -> Optional[str]:
        graph_data = incident.evidence.get("graph") or {}
        cluster_key = graph_data.get("cluster_key")
        return str(cluster_key) if cluster_key else None

    def bfs(self, start: str, policy: TraversalPolicy) -> _ScopeScan:
        if start not in self.nodes:
            raise KeyError(f"start node not found: {start}")
        queue: deque[Tuple[str, int]] = deque([(start, 0)])
        levels: Dict[str, int] = {start: 0}
        visited: List[str] = []
        related_incidents: List[str] = []
        affected_domains: List[str] = []
        low_confidence_edge = False
        while queue:
            node_id, depth = queue.popleft()
            visited.append(node_id)
            node = self.nodes[node_id]
            if node.kind in {"incident", "error", "failure"}:
                ref = str(node.ref_id or node.id)
                if ref not in related_incidents:
                    related_incidents.append(ref)
            if node.kind in {"component", "domain", "service", "tool", "step"}:
                ref = str(node.ref_id or node.id)
                if ref not in affected_domains:
                    affected_domains.append(ref)
            if depth >= policy.max_bfs_depth:
                continue
            for edge in self._undirected.get(node_id, []):
                if edge.confidence < policy.min_edge_confidence:
                    low_confidence_edge = True
                    continue
                neighbor = edge.target if edge.source == node_id else edge.source
                if neighbor not in levels:
                    levels[neighbor] = depth + 1
                    queue.append((neighbor, depth + 1))
        return _ScopeScan(
            visited=visited,
            levels=levels,
            related_incident_ids=related_incidents,
            affected_domains=affected_domains,
            low_confidence_edge=low_confidence_edge,
        )

    def dfs_upstream(self, start: str, policy: TraversalPolicy) -> _TraceScan:
        if start not in self.nodes:
            raise KeyError(f"start node not found: {start}")
        best_path: List[str] = [start]
        cycle_detected = False
        truncated = False
        low_confidence_edge = False
        stack: List[Tuple[str, List[str], Set[str]]] = [(start, [start], {start})]
        while stack:
            node_id, path, seen = stack.pop()
            candidates = []
            for edge in self._out.get(node_id, []):
                if edge.relation not in policy.upstream_relations:
                    continue
                if edge.confidence < policy.min_edge_confidence:
                    low_confidence_edge = True
                    continue
                candidates.append(edge)
            if len(path) > len(best_path):
                best_path = path
            if not candidates:
                continue
            if len(path) >= policy.max_dfs_depth:
                truncated = True
                continue
            for edge in sorted(candidates, key=lambda item: item.confidence):
                if edge.target in seen:
                    cycle_detected = True
                    continue
                stack.append((edge.target, path + [edge.target], seen | {edge.target}))
        return _TraceScan(
            root_candidate=best_path[-1] if best_path else None,
            path=best_path,
            cycle_detected=cycle_detected,
            truncated=truncated,
            low_confidence_edge=low_confidence_edge,
        )


class GraphAnalyzer:
    """将一张现场图压缩成门控与补丁可用的 GraphFinding。"""

    def __init__(self, policy: Optional[TraversalPolicy] = None) -> None:
        self.policy = policy or TraversalPolicy()

    def analyze(self, incident: Incident) -> Optional[GraphFinding]:
        graph = IncidentGraph.from_evidence(incident)
        if graph is None:
            return None
        start = graph.start_node(incident)
        if not start:
            return None
        scope = graph.bfs(start, self.policy)
        trace = graph.dfs_upstream(start, self.policy)
        root_candidate = trace.root_candidate
        if trace.low_confidence_edge and len(trace.path) == 1:
            root_candidate = None
        root_node = graph.nodes.get(root_candidate) if root_candidate else None
        cluster_key = graph.cluster_key(incident)
        if not cluster_key and root_node:
            cluster_key = (
                root_node.metadata.get("cluster_key")
                or root_node.ref_id
                or root_node.id
            )
        cluster_key = str(cluster_key or incident.family)
        return GraphFinding(
            cluster_key=cluster_key,
            start_node=start,
            session_id=graph.session_id(incident),
            scope_node_ids=scope.visited,
            related_incident_ids=scope.related_incident_ids,
            affected_domains=scope.affected_domains,
            root_candidate=root_candidate,
            cause_path=trace.path,
            cycle_detected=trace.cycle_detected,
            truncated=trace.truncated,
            low_confidence_edge=(
                scope.low_confidence_edge or trace.low_confidence_edge
            ),
            needs_human=(
                scope.low_confidence_edge
                or trace.cycle_detected
                or trace.truncated
                or trace.low_confidence_edge
            ),
        )
