# -*- coding: utf-8 -*-
"""RHEM 参考实现：门控、自动并库、决策器与人工批准。"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from .distillation import HardExampleDistiller
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


@dataclass(frozen=True)
class GateDecision:
    """一次门控判断的完整说明，便于审计和复现实验。"""

    allowed: bool
    mode: str
    reason: str
    effective_min_occurrences: int
    effective_min_sources: int
    occurrences: int
    sources: int
    sensitivity: float = 0.0
    recurrence_per_day: float = 0.0
    recent_occurrences: int = 0
    requires_human: bool = False
    lock_reason: Optional[str] = None
    evaluated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "mode": self.mode,
            "reason": self.reason,
            "effective_min_occurrences": self.effective_min_occurrences,
            "effective_min_sources": self.effective_min_sources,
            "occurrences": self.occurrences,
            "sources": self.sources,
            "sensitivity": self.sensitivity,
            "recurrence_per_day": self.recurrence_per_day,
            "recent_occurrences": self.recent_occurrences,
            "requires_human": self.requires_human,
            "lock_reason": self.lock_reason,
            "evaluated_at": self.evaluated_at,
        }


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)


@dataclass
class GatePolicy:
    """固定门控；保留为向后兼容的默认策略。"""

    min_occurrences: int = 3
    min_sources: int = 1

    def decide(
        self,
        group: Dict[str, Any],
        *,
        now: Optional[str] = None,
    ) -> GateDecision:
        occurrences = int(group.get("occurrences", 0))
        sources = set(group.get("sources") or [])
        allowed = (
            occurrences >= self.min_occurrences
            and len(sources) >= self.min_sources
        )
        return GateDecision(
            allowed=allowed,
            mode="fixed",
            reason="fixed_threshold_reached" if allowed else "fixed_threshold_waiting",
            effective_min_occurrences=self.min_occurrences,
            effective_min_sources=self.min_sources,
            occurrences=occurrences,
            sources=len(sources),
            evaluated_at=now,
        )

    def reached(self, group: Dict[str, Any]) -> bool:
        return self.decide(group).allowed


@dataclass
class AdaptiveGatePolicy(GatePolicy):
    """按近期复发压力调档；只改变收得快慢，不改变对错标准。"""

    fast_min_occurrences: int = 2
    high_risk_min_occurrences: int = 5
    recurrence_window_days: float = 7.0
    fast_recent_occurrences: int = 2
    decay_days: float = 7.0

    def __post_init__(self) -> None:
        if self.min_occurrences < 1:
            raise ValueError("min_occurrences must be >= 1")
        if self.fast_min_occurrences < 1:
            raise ValueError("fast_min_occurrences must be >= 1")
        if self.fast_min_occurrences > self.min_occurrences:
            raise ValueError(
                "fast_min_occurrences cannot exceed min_occurrences"
            )
        if self.high_risk_min_occurrences < self.min_occurrences:
            raise ValueError(
                "high_risk_min_occurrences cannot be below min_occurrences"
            )
        if self.recurrence_window_days <= 0:
            raise ValueError("recurrence_window_days must be > 0")
        if self.fast_recent_occurrences < 1:
            raise ValueError("fast_recent_occurrences must be >= 1")
        if self.decay_days <= 0:
            raise ValueError("decay_days must be > 0")

    def decide(
        self,
        group: Dict[str, Any],
        *,
        now: Optional[str] = None,
    ) -> GateDecision:
        occurrences = int(group.get("occurrences", 0))
        sources = set(group.get("sources") or [])
        times = self._occurrence_times(group)
        evaluated_at = _parse_utc(now) or _parse_utc(group.get("last_seen"))
        if evaluated_at is None:
            evaluated_at = datetime.now(timezone.utc)

        recent_occurrences = 0
        recurrence_per_day = 0.0
        sensitivity = 0.0
        if times:
            window_start = evaluated_at - timedelta(
                days=self.recurrence_window_days
            )
            recent_occurrences = sum(
                1 for item in times if item >= window_start
            )
            if len(times) >= 2:
                elapsed_days = max(
                    (times[-1] - times[0]).total_seconds() / 86400.0,
                    1.0 / 86400.0,
                )
                recurrence_per_day = (len(times) - 1) / elapsed_days
            newest_age_days = max(
                (evaluated_at - times[-1]).total_seconds() / 86400.0,
                0.0,
            )
            recency_factor = math.exp(
                -newest_age_days / self.decay_days
            )
            density_pressure = min(
                1.0,
                recent_occurrences / self.fast_recent_occurrences,
            )
            sensitivity = density_pressure * recency_factor

        threshold = self.min_occurrences
        if sensitivity > 0:
            reduction = self.min_occurrences - self.fast_min_occurrences
            threshold = int(
                math.ceil(
                    self.min_occurrences
                    - reduction * sensitivity
                    - 1e-9
                )
            )
        reason = "base_threshold"
        if sensitivity >= 1.0:
            reason = "fast_recurrence"
        elif recent_occurrences == 0:
            reason = "quiet_period"

        flags = group.get("gate_flags") or {}
        requires_human = False
        lock_reason = None
        if flags.get("manual_hold"):
            threshold = self.high_risk_min_occurrences
            reason = "manual_hold"
            lock_reason = "manual_hold"
            requires_human = True
        elif flags.get("high_risk"):
            threshold = self.high_risk_min_occurrences
            reason = "high_risk"
            lock_reason = "high_risk"
            requires_human = True
        if flags.get("recheck_failed"):
            threshold = self.high_risk_min_occurrences
            reason = "recheck_failed"
            lock_reason = "recheck_failed"
            requires_human = True

        allowed = (
            occurrences >= threshold
            and len(sources) >= self.min_sources
        )
        return GateDecision(
            allowed=allowed,
            mode="adaptive",
            reason=reason if allowed else f"{reason}_waiting",
            effective_min_occurrences=threshold,
            effective_min_sources=self.min_sources,
            occurrences=occurrences,
            sources=len(sources),
            sensitivity=round(sensitivity, 6),
            recurrence_per_day=round(recurrence_per_day, 6),
            recent_occurrences=recent_occurrences,
            requires_human=requires_human,
            lock_reason=lock_reason,
            evaluated_at=evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _occurrence_times(self, group: Dict[str, Any]) -> List[datetime]:
        parsed = [
            item
            for item in (
                _parse_utc(value)
                for value in (group.get("occurrence_times") or [])
            )
            if item is not None
        ]
        if parsed:
            return sorted(parsed)
        # 旧记录没有真实发生时间时，不推测历史分布；从升级后的新事件开始积累。
        return []


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
        distiller: Optional[HardExampleDistiller] = None,
    ) -> None:
        self.store = store
        self.gate = gate or GatePolicy()
        self.decisioner = decisioner or Decisioner()
        self.graph_analyzer = graph_analyzer or GraphAnalyzer()
        self.distiller = distiller

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
        decision = self._decide_gate(gate_record, now=incident.occurred_at)
        gate_decision = decision.to_dict()
        record_id = cluster_key or incident.family
        self.store.log_gate_decision(record_id, gate_decision)
        distillation_decision = None
        if self.distiller is not None:
            distillation_decision = self.distiller.classify(
                gate_record,
                gate_decision,
                graph_finding,
            ).to_dict()
            self.store.mark_distillation_decision(
                incident.family,
                distillation_decision,
                cluster_key=cluster_key,
            )
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
            "gate_decision": gate_decision,
            "distillation_decision": distillation_decision,
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
                f"本轮门控阈值 {decision.effective_min_occurrences}，尚未达到。"
            )
        else:
            outcome["message"] = (
                f"已进入待学习区；同族累计 {group['occurrences']} 次，"
                f"本轮门控阈值 {decision.effective_min_occurrences}，"
                "尚未达到。"
            )

        if not decision.allowed:
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
        if (
            distillation_decision is not None
            and not distillation_decision["allowed"]
        ):
            outcome["event"] = "distillation_hold"
            outcome["message"] = (
                "门控已通过，但难例蒸馏判定当前价值等级为 "
                f"{distillation_decision['tier']}（"
                f"{distillation_decision['reason']}）；"
                "暂不并库，继续积累跨来源证据。"
            )
            return outcome
        if (
            decision.requires_human
            and incident.category in {
                ErrorCategory.RECOGNITION,
                ErrorCategory.PROCESS,
            }
        ):
            self.store.mark_group(incident.family, "needs_human")
            if cluster:
                self.store.mark_cluster(cluster["cluster_key"], "needs_human")
            outcome["event"] = "needs_human_gate"
            outcome["message"] = (
                "自适应门控已锁高档并达到阈值；该类不允许自动落补丁，"
                f"需人工复核。原因：{decision.reason}。"
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

    def _decide_gate(
        self,
        group: Dict[str, Any],
        *,
        now: Optional[str],
    ) -> GateDecision:
        decide = getattr(self.gate, "decide", None)
        if callable(decide):
            return decide(group, now=now)

        # 兼容旧版只实现 reached() 的自定义门控。
        occurrences = int(group.get("occurrences", 0))
        sources = len(set(group.get("sources") or []))
        allowed = bool(self.gate.reached(group))
        return GateDecision(
            allowed=allowed,
            mode="legacy",
            reason=(
                "legacy_threshold_reached"
                if allowed
                else "legacy_threshold_waiting"
            ),
            effective_min_occurrences=int(self.gate.min_occurrences),
            effective_min_sources=int(self.gate.min_sources),
            occurrences=occurrences,
            sources=sources,
            evaluated_at=now,
        )

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
            gate_decision=outcome.get("gate_decision"),
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
            gate_decision=outcome.get("gate_decision"),
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
            gate_decision=outcome.get("gate_decision"),
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
            gate_decision=outcome.get("gate_decision"),
        )
        outcome.update({
            "event": "knowledge_reported",
            "message": "门控通过；已上报待归因，等待人工给出 canonical/definition。",
            "proposal_id": proposal["id"],
        })
        return outcome

    # ----------------------------------------------------------
    # 库内回炼：只通过补丁改质量账本、禁用或删除陈旧记录
    # ----------------------------------------------------------
    def redistill(
        self,
        *,
        apply: bool = False,
        now: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成回炼计划；显式 apply 时才生成可回滚补丁。"""

        distiller = self.distiller or HardExampleDistiller()
        plan = distiller.plan_library(
            self.store.list_distillation_records(),
            now=now,
        )
        self.store.log_distillation_plan(plan)
        plan["applied"] = False
        plan["patch_id"] = None
        if not apply:
            return plan
        if not actor:
            raise ValueError("redistill(apply=True) requires actor")
        if not plan["actions"]:
            plan["applied"] = True
            return plan

        counts = plan.get("summary") or {}
        patch = self.store.commit(
            actions=copy.deepcopy(plan["actions"]),
            summary=(
                "难例蒸馏回炼："
                f"core={counts.get('core', 0)}, "
                f"downweight={counts.get('downweight', 0)}, "
                f"cull={counts.get('cull', 0)}"
            ),
            actor=actor if actor.startswith("human:") else f"human:{actor}",
            distillation_plan=copy.deepcopy(plan),
        )
        plan["applied"] = True
        plan["patch_id"] = patch["id"]
        return plan

    # ----------------------------------------------------------
    # 人工批准
    # ----------------------------------------------------------'
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
        *,
        record_feedback: bool = False,
    ) -> Dict[str, Any]:
        rows = self.store.list_incidents(active_only=True)
        resolved = 0
        failed = 0
        detail: List[Dict[str, Any]] = []
        for row in rows:
            ok = resolver(row) if resolver else self._default_resolver(row)
            if record_feedback:
                self._record_rescan_feedback(row, bool(ok))
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

    def _record_rescan_feedback(
        self,
        row: Dict[str, Any],
        resolved: bool,
    ) -> None:
        view = self.store.view()
        category = row["category"]
        evidence = row.get("evidence") or {}
        scenario = row.get("source") or row["id"]
        if category == ErrorCategory.RECOGNITION.value:
            canonical = evidence.get("canonical") or row.get("expected")
            if canonical and canonical in view["alias_rules"]:
                self.store.record_feedback(
                    "alias",
                    canonical,
                    scenario,
                    resolved=resolved,
                )
            return
        if category == ErrorCategory.RULE_GAP.value:
            family = row["family"]
            for rule_id, rule in view["operational_rules"].items():
                if rule.get("family") == family:
                    self.store.record_feedback(
                        "rule",
                        rule_id,
                        scenario,
                        resolved=resolved,
                    )
            return
        if category == ErrorCategory.KNOWLEDGE_GAP.value:
            term = evidence.get("term") or row["family"]
            if term in view["terms"]:
                self.store.record_feedback(
                    "term",
                    term,
                    scenario,
                    resolved=resolved,
                )

    def _default_resolver(self, row: Dict[str, Any]) -> bool:
        view = self.store.view()
        category = row["category"]
        evidence = row.get("evidence") or {}
        if category == ErrorCategory.RECOGNITION.value:
            canonical = evidence.get("canonical") or row.get("expected")
            alias = evidence.get("alias") or row.get("actual")
            rule = view["alias_rules"].get(canonical)
            status = (rule.get("distillation") or {}).get("status") if rule else None
            return bool(
                rule
                and status == "active"
                and alias
                and alias in rule["aliases"]
            )
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
            status = (record.get("distillation") or {}).get("status") if record else None
            return bool(record and status == "active" and record.get("canonical"))
        return False
