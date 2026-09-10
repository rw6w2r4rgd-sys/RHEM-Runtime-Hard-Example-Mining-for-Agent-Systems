# -*- coding: utf-8 -*-
"""RHEM 参考实现：门控、自动并库、决策器与人工批准。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .graph import GraphAnalyzer, GraphFinding
from .models import (
    ErrorCategory,
    HardExampleGroup,
    Incident,
    NotFound,
    new_id,
    utc_now,
)
from .store import RhemStore


@dataclass
class GatePolicy:
    """同一图簇或难例族需跨多次累计，达到阈值后才允许并库。"""

    min_occurrences: int = 3
    min_sources: int = 1

    def reached(self, group: Dict[str, Any]) -> bool:
        occurrences = int(group.get("occurrences", 0))
        sources = set(group.get("sources") or [])
        return (
            occurrences >= self.min_occurrences
            and len(sources) >= self.min_sources
        )


class Decisioner:
    """把规则缺失/知识缺口写成药方；知识缺口不代答，只要求人归因。"""

    def draft(
        self,
        incident: Incident,
        graph_finding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        evidence = incident.evidence or {}
        graph_context = copy.deepcopy(graph_finding) if graph_finding else None
        if incident.category == ErrorCategory.RULE_GAP:
            proposed = evidence.get("proposed_rule")
            if isinstance(proposed, dict):
                rule = copy.deepcopy(proposed)
                if graph_context:
                    rule["graph_evidence"] = graph_context
                return {"rule": rule}
            rule = {
                "id": new_id("rule"),
                "name": evidence.get("rule_name") or "规则缺失补丁",
                "description": evidence.get("rule_reason")
                or f"为 {incident.family} 补一条可执行规则。",
                "condition": evidence.get("rule_condition")
                or f"family={incident.family}",
                "action": evidence.get("rule_action")
                or "命中时先请求人工确认，再继续执行",
                "family": incident.family,
                "sources": [incident.source],
                "enabled": True,
                "patch_id": None,
                "created_at": utc_now(),
            }
            if graph_context:
                rule["graph_evidence"] = graph_context
            return {"rule": rule}
        if incident.category == ErrorCategory.KNOWLEDGE_GAP:
            suggestion = {
                "term": evidence.get("term") or incident.family,
                "required_attribution": ["canonical", "definition", "kind"],
            }
            if graph_context:
                suggestion["graph_evidence"] = graph_context
            return suggestion
        return {}


class LearningEngine:
    """四类难例的统一学习回路。"""

    def __init__(
        self,
        store: RhemStore,
        gate: Optional[GatePolicy] = None,
        decisioner: Optional[Decisioner] = None,
        graph_analyzer: Optional[GraphAnalyzer] = None,
    ) -> None:
        self.store = store
        self.gate = gate or GatePolicy()
        self.decisioner = decisioner or Decisioner()
        self.graph_analyzer = graph_analyzer or GraphAnalyzer()

    # ----------------------------------------------------------
    # 主入口：现场错误/用户纠错 → 待学习区 → 门控
    # ----------------------------------------------------------
    def ingest(self, incident: Incident) -> Dict[str, Any]:
        finding = self.graph_analyzer.analyze(incident)
        graph_finding = finding.to_dict() if finding else None
        cluster_key = None
        evidence_key = None
        if finding:
            incident.cluster_key = finding.cluster_key
            cluster_key = finding.cluster_key
            evidence_key = self._graph_evidence_key(incident, finding)

        group = self.store.record_incident(
            incident,
            evidence_key=evidence_key,
            deduplicate_evidence=finding is not None,
        )
        cluster = self.store.get_cluster(cluster_key) if cluster_key else None
        gate_record = cluster or group
        occurrences = int(gate_record.get("occurrences", 0))
        outcome = {
            "incident_id": incident.id,
            "family": incident.family,
            "cluster_key": cluster_key,
            "category": incident.category.value,
            "occurrences": occurrences,
            "family_occurrences": group["occurrences"],
            "symptom_count": (
                cluster["symptom_count"] if cluster else group["occurrences"]
            ),
            "graph_finding": graph_finding,
            "event": "waiting",
            "message": (
                f"已进入待学习区；同族累计 {group['occurrences']} 次，"
                "尚未达到门控。"
            ),
            "patch_id": None,
            "proposal_id": None,
        }
        if finding:
            root = finding.root_candidate or finding.cluster_key
            outcome["message"] = (
                f"已进入待学习图簇；根因候选 {root} 累计 {occurrences} "
                f"个独立证据、{outcome['symptom_count']} 个症状，"
                "尚未达到门控。"
            )

        if not self.gate.reached(gate_record):
            return outcome
        if gate_record["status"] != "pending":
            outcome["event"] = "already_handled"
            scope = "簇" if finding else "族"
            outcome["message"] = (
                f"该{scope}已处于 {gate_record['status']} 状态，不再重复并库。"
            )
            return outcome
        if finding and finding.needs_human:
            self.store.mark_cluster(finding.cluster_key, "needs_human")
            reasons = []
            if finding.cycle_detected:
                reasons.append("上游路径存在循环")
            if finding.truncated:
                reasons.append("根因追踪达到深度上限")
            if finding.low_confidence_edge:
                reasons.append("观察到低置信度边")
            outcome["event"] = "needs_human_graph"
            outcome["message"] = (
                "图探针达到门控，但无法给出单一路径："
                + "；".join(reasons or ["图结构不明确"])
                + "。已转人工复核。"
            )
            return outcome

        if incident.category == ErrorCategory.RECOGNITION:
            return self._apply_recognition(incident, group, cluster, outcome)
        if incident.category == ErrorCategory.PROCESS:
            return self._apply_process_fix(incident, group, cluster, outcome)
        if incident.category == ErrorCategory.RULE_GAP:
            return self._propose_rule(incident, group, cluster, outcome)
        if incident.category == ErrorCategory.KNOWLEDGE_GAP:
            return self._report_knowledge(incident, group, cluster, outcome)
        raise ValueError(f"unknown category: {incident.category}")

    @staticmethod
    def _graph_evidence_key(
        incident: Incident,
        finding: GraphFinding,
    ) -> str:
        if finding.needs_human or not finding.root_candidate:
            root = finding.cluster_key or incident.family
        else:
            root = finding.root_candidate
        session = finding.session_id or incident.source
        return f"{root}@{session}"

    # ----------------------------------------------------------
    # 四类出口
    # ----------------------------------------------------------
    def _apply_recognition(
        self,
        incident: Incident,
        group: Dict[str, Any],
        cluster: Optional[Dict[str, Any]],
        outcome: Dict[str, Any],
    ) -> Dict[str, Any]:
        evidence = incident.evidence or {}
        alias = evidence.get("alias") or incident.actual
        canonical = evidence.get("canonical") or incident.expected
        if not alias or not canonical:
            outcome["event"] = "needs_correction"
            outcome["message"] = (
                "已达门控，但证据缺少 alias/canonical，不能自动猜测修正值。"
            )
            return outcome
        gate_record = cluster or group
        patch = self.store.commit(
            actions=[{
                "op": "add_alias",
                "canonical": canonical,
                "aliases": [alias],
                "sources": list(gate_record["sources"]),
            }],
            summary=f"识别难例并库：{alias} -> {canonical}",
            actor="rhem:recognition",
            incident_ids=list(gate_record["incident_ids"]),
        )
        self.store.mark_group(
            incident.family,
            "applied",
            patch_id=patch["id"],
            proposal_id=group.get("proposal_id"),
        )
        if cluster:
            self.store.mark_cluster(
                cluster["cluster_key"],
                "applied",
                patch_id=patch["id"],
                proposal_id=cluster.get("proposal_id"),
            )
        outcome.update({
            "event": "auto_applied",
            "message": (
                f"门控通过；别名 {alias} -> {canonical} "
                "已自动并库并即时生效。"
            ),
            "patch_id": patch["id"],
        })
        return outcome

    def _apply_process_fix(
        self,
        incident: Incident,
        group: Dict[str, Any],
        cluster: Optional[Dict[str, Any]],
        outcome: Dict[str, Any],
    ) -> Dict[str, Any]:
        evidence = incident.evidence or {}
        fix = evidence.get("fix")
        if not isinstance(fix, dict) or not fix:
            outcome["event"] = "needs_structural_fix"
            outcome["message"] = "已达门控，但证据缺少可执行的 fix 配置。"
            return outcome
        gate_record = cluster or group
        patch = self.store.commit(
            actions=[{
                "op": "set_process_settings",
                "settings": copy.deepcopy(fix),
            }],
            summary=f"流程难例结构性修复：{incident.family}",
            actor="rhem:process",
            incident_ids=list(gate_record["incident_ids"]),
        )
        self.store.mark_group(
            incident.family,
            "applied",
            patch_id=patch["id"],
            proposal_id=group.get("proposal_id"),
        )
        if cluster:
            self.store.mark_cluster(
                cluster["cluster_key"],
                "applied",
                patch_id=patch["id"],
                proposal_id=cluster.get("proposal_id"),
            )
        outcome.update({
            "event": "structural_fix_applied",
            "message": (
                "门控通过；流程配置已结构性修复，不入知识库。"
                f"补丁：{patch['id']}"
            ),
            "patch_id": patch["id"],
        })
        return outcome

    def _propose_rule(
        self,
        incident: Incident,
        group: Dict[str, Any],
        cluster: Optional[Dict[str, Any]],
        outcome: Dict[str, Any],
    ) -> Dict[str, Any]:
        suggestion = self.decisioner.draft(
            incident,
            outcome.get("graph_finding"),
        )
        rule = suggestion.get("rule") or {}
        rule.setdefault("id", new_id("rule"))
        rule.setdefault("name", "规则缺失补丁")
        rule.setdefault("family", incident.family)
        gate_record = cluster or group
        rule["family"] = incident.family
        rule["enabled"] = True
        rule["sources"] = list(dict.fromkeys(gate_record["sources"]))
        rule["created_at"] = utc_now()
        proposal = self.store.create_proposal(
            kind="rule_gap",
            family=incident.family,
            cluster_key=cluster["cluster_key"] if cluster else None,
            title=rule.get("name", "规则缺失药方"),
            detail=rule.get("description", ""),
            incident_ids=list(gate_record["incident_ids"]),
            suggested={"rule": rule},
        )
        outcome.update({
            "event": "proposal_created",
            "message": "门控通过；决策器已开药方，需人工批准后才写入规则库。",
            "proposal_id": proposal["id"],
        })
        return outcome

    def _report_knowledge(
        self,
        incident: Incident,
        group: Dict[str, Any],
        cluster: Optional[Dict[str, Any]],
        outcome: Dict[str, Any],
    ) -> Dict[str, Any]:
        suggestion = self.decisioner.draft(
            incident,
            outcome.get("graph_finding"),
        )
        term = suggestion.get("term") or incident.family
        gate_record = cluster or group
        proposal = self.store.create_proposal(
            kind="knowledge_gap",
            family=incident.family,
            cluster_key=cluster["cluster_key"] if cluster else None,
            title=f"知识缺口：{term}",
            detail="该词/品名多次未识别；需要人工归因后才能写入词条库。",
            incident_ids=list(gate_record["incident_ids"]),
            suggested=suggestion,
            required_attribution=["canonical", "definition", "kind"],
        )
        outcome.update({
            "event": "knowledge_reported",
            "message": "门控通过；已上报待归因，等待人工给出 canonical/definition。",
            "proposal_id": proposal["id"],
        })
        return outcome

    # ----------------------------------------------------------
    # 人工批准
    # ----------------------------------------------------------
    def approve(
        self,
        proposal_id: str,
        approver: str,
        attribution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        proposal = self.store.get_prescription(proposal_id)
        kind = proposal["kind"]
        if kind == "rule_gap":
            rule = copy.deepcopy(proposal["suggested"]["rule"])
            actions = [{
                "op": "upsert_rule",
                "rule": rule,
            }]
        elif kind == "knowledge_gap":
            if not attribution:
                raise NotFound(
                    "knowledge attribution requires canonical/definition/kind"
                )
            required = set(proposal.get("required_attribution") or [])
            missing = required - set(attribution.keys())
            if missing:
                raise NotFound(f"missing attribution fields: {sorted(missing)}")
            term = proposal["suggested"]["term"]
            term_record = {
                "term": term,
                "canonical": attribution["canonical"],
                "definition": attribution["definition"],
                "kind": attribution["kind"],
                "attributed_by": approver,
                "created_at": utc_now(),
                "status": "active",
                "patch_id": None,
                "family": proposal["family"],
            }
            actions = [{
                "op": "add_term",
                "term": term_record,
            }]
        else:
            raise NotFound(f"unsupported proposal kind: {kind}")
        return self.store.apply_proposal(
            proposal_id=proposal_id,
            approver=approver,
            actions=actions,
            summary=f"人工批准：{proposal['title']}",
            incident_ids=proposal["incident_ids"],
        )

    def reject(self, proposal_id: str, reason: str, approver: str) -> None:
        self.store.reject_proposal(proposal_id, reason=reason, approver=approver)

    def rollback(self, patch_id: str) -> Dict[str, Any]:
        patch = self.store.rollback(patch_id)
        view = self.store.view()
        for cluster_key, cluster in view["clusters"].items():
            if cluster.get("patch_id") == patch_id:
                self.store.mark_cluster(
                    cluster_key,
                    "rolled_back",
                    patch_id=patch_id,
                    proposal_id=cluster.get("proposal_id"),
                )
        for family, group in view["hard_examples"].items():
            if group.get("patch_id") == patch_id:
                self.store.mark_group(
                    family,
                    "rolled_back",
                    patch_id=patch_id,
                    proposal_id=group.get("proposal_id"),
                )
        for proposal_id, proposal in view["prescriptions"].items():
            if proposal.get("patch_id") == patch_id:
                self.store.rollback_proposal_link(proposal_id, patch_id)
        return patch

    # ----------------------------------------------------------
    # 复扫：用修复后的运行态回放历史难例
    # ----------------------------------------------------------
    def rescan(
        self,
        resolver: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> Dict[str, Any]:
        rows = self.store.list_incidents(active_only=True)
        resolved = 0
        failed = 0
        detail: List[Dict[str, Any]] = []
        for row in rows:
            ok = resolver(row) if resolver else self._default_resolver(row)
            detail.append({
                "incident_id": row["id"],
                "family": row["family"],
                "category": row["category"],
                "resolved": bool(ok),
            })
            resolved += bool(ok)
            failed += not bool(ok)
        return {
            "total": len(rows),
            "resolved": resolved,
            "failed": failed,
            "detail": detail,
        }

    def _default_resolver(self, row: Dict[str, Any]) -> bool:
        view = self.store.view()
        category = row["category"]
        evidence = row.get("evidence") or {}
        if category == ErrorCategory.RECOGNITION.value:
            canonical = evidence.get("canonical") or row.get("expected")
            alias = evidence.get("alias") or row.get("actual")
            rule = view["alias_rules"].get(canonical)
            return bool(rule and alias and alias in rule["aliases"])
        if category == ErrorCategory.PROCESS.value:
            fix = evidence.get("fix") or {}
            settings = view["process_settings"]
            return bool(fix) and all(
                settings.get(key) == value for key, value in fix.items()
            )
        if category == ErrorCategory.RULE_GAP.value:
            family = row["family"]
            return any(
                rule.get("family") == family and bool(rule.get("enabled"))
                for rule in view["operational_rules"].values()
            )
        if category == ErrorCategory.KNOWLEDGE_GAP.value:
            term = evidence.get("term") or row["family"]
            record = view["terms"].get(term)
            return bool(record and record.get("canonical"))
        return False
