# -*- coding: utf-8 -*-
"""RHEM 难例蒸馏：并库前分级，库内按账本回炼。"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


@dataclass(frozen=True)
class DistillationDecision:
    """一次并库前分馏的可审计结果。"""

    tier: str
    allowed: bool
    reason: str
    priority: int
    signals: Dict[str, Any]
    evaluated_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "allowed": self.allowed,
            "reason": self.reason,
            "priority": self.priority,
            "signals": copy.deepcopy(self.signals),
            "evaluated_at": self.evaluated_at,
        }


@dataclass
class HardExampleDistiller:
    """只复用账本信号的难例蒸馏器。

    并库前按复发压力、来源覆盖和风险标记分馏；库内按命中数、
    场景覆盖、失败反馈和闲置时间回炼。这里不做语义判断，也不改护栏。
    """

    high_min_sources: int = 2
    high_min_occurrences: int = 2
    core_min_hits: int = 3
    core_min_scenarios: int = 2
    stale_after_days: float = 30.0
    cull_after_days: float = 90.0
    conflict_misses: int = 1

    def __post_init__(self) -> None:
        if self.high_min_sources < 1:
            raise ValueError("high_min_sources must be >= 1")
        if self.high_min_occurrences < 1:
            raise ValueError("high_min_occurrences must be >= 1")
        if self.core_min_hits < 1:
            raise ValueError("core_min_hits must be >= 1")
        if self.core_min_scenarios < 1:
            raise ValueError("core_min_scenarios must be >= 1")
        if self.stale_after_days < 0:
            raise ValueError("stale_after_days must be >= 0")
        if self.cull_after_days < self.stale_after_days:
            raise ValueError(
                "cull_after_days must be >= stale_after_days"
            )
        if self.conflict_misses < 1:
            raise ValueError("conflict_misses must be >= 1")

    def classify(
        self,
        group: Dict[str, Any],
        gate_decision: Dict[str, Any],
        graph_finding: Optional[Dict[str, Any]] = None,
    ) -> DistillationDecision:
        """给并库前样本分级；高风险样本也不会绕过门控。"""

        occurrences = int(group.get("occurrences", 0))
        sources = set(group.get("sources") or [])
        flags = group.get("gate_flags") or {}
        gate_allowed = bool(gate_decision.get("allowed"))
        recent_occurrences = int(
            gate_decision.get("recent_occurrences", 0)
        )
        sensitivity = float(gate_decision.get("sensitivity", 0.0))
        root_candidate = (
            graph_finding.get("root_candidate") if graph_finding else None
        )
        signals = {
            "occurrences": occurrences,
            "sources": len(sources),
            "gate_allowed": gate_allowed,
            "recent_occurrences": recent_occurrences,
            "sensitivity": round(sensitivity, 6),
            "root_candidate": root_candidate,
            "high_risk": bool(flags.get("high_risk")),
            "manual_hold": bool(flags.get("manual_hold")),
            "recheck_failed": bool(flags.get("recheck_failed")),
        }

        risk_locked = any(
            signals[key]
            for key in ("high_risk", "manual_hold", "recheck_failed")
        )
        if risk_locked:
            tier = "high"
            reason = "risk_locked"
        elif (
            gate_allowed
            and occurrences >= self.high_min_occurrences
            and len(sources) >= self.high_min_sources
        ):
            tier = "high"
            reason = "cross_source_recurrence"
        elif (
            occurrences >= self.high_min_occurrences
            or len(sources) >= self.high_min_sources
            or recent_occurrences >= self.high_min_occurrences
        ):
            tier = "medium"
            reason = "awaiting_cross_source_evidence"
        else:
            tier = "low"
            reason = "isolated_or_single_source"

        priority = {"high": 2, "medium": 1, "low": 0}[tier]
        return DistillationDecision(
            tier=tier,
            allowed=tier == "high",
            reason=reason,
            priority=priority,
            signals=signals,
            evaluated_at=gate_decision.get("evaluated_at"),
        )

    def plan_library(
        self,
        records: List[Dict[str, Any]],
        *,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对库内记录做一次只读回炼计划。"""

        evaluated_at = _parse_utc(now) or _utc_now()
        evaluated_text = evaluated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        items: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        summary = {
            "core": 0,
            "downweight": 0,
            "cull": 0,
            "retain": 0,
        }

        for row in records:
            item = self._plan_record(
                domain=row["domain"],
                key=row["key"],
                record=row["record"],
                evaluated_at=evaluated_text,
                now=evaluated_at,
            )
            items.append(item)
            action = item.get("action") or "retain"
            summary[action] = summary.get(action, 0) + 1
            if item.get("patch_action"):
                actions.append(item["patch_action"])

        return {
            "evaluated_at": evaluated_text,
            "summary": summary,
            "items": items,
            "actions": actions,
        }

    def _plan_record(
        self,
        *,
        domain: str,
        key: str,
        record: Dict[str, Any],
        evaluated_at: str,
        now: datetime,
    ) -> Dict[str, Any]:
        metadata = record.get("distillation") or {}
        hits = int(metadata.get("hits", 0))
        misses = int(metadata.get("misses", 0))
        consecutive_misses = int(metadata.get("consecutive_misses", 0))
        superseded_by = metadata.get("superseded_by")
        scenarios = set(metadata.get("scenario_sources") or [])
        quality_tier = metadata.get("quality_tier", "standard")
        status = metadata.get("status", "active")
        created_at = _parse_utc(metadata.get("created_at")) or _parse_utc(
            record.get("created_at")
        )
        last_hit_at = _parse_utc(metadata.get("last_hit_at"))
        activity_at = last_hit_at or created_at or now
        idle_days = max((now - activity_at).total_seconds() / 86400.0, 0.0)

        action = "retain"
        reason = "sufficient_current_value"
        if status == "culled":
            reason = "already_culled"
        elif consecutive_misses >= self.conflict_misses:
            if quality_tier != "downweighted" or status != "downweighted":
                action = "downweight"
                reason = "feedback_conflict"
            else:
                reason = "already_downweighted"
        elif superseded_by:
            if quality_tier != "downweighted" or status != "downweighted":
                action = "downweight"
                reason = "superseded"
            else:
                reason = "already_downweighted"
        elif (
            hits >= self.core_min_hits
            and len(scenarios) >= self.core_min_scenarios
        ):
            if quality_tier != "core" or status != "active":
                action = "core"
                reason = "multi_scenario_confirmed"
            else:
                reason = "already_core"
        elif hits == 0 and idle_days >= self.cull_after_days:
            action = "cull"
            reason = "never_hit_and_stale"
        elif idle_days >= self.stale_after_days:
            if quality_tier != "downweighted" or status != "downweighted":
                action = "downweight"
                reason = "long_idle"
            else:
                reason = "already_downweighted"
        elif hits > 0:
            reason = "recent_hit"
        else:
            reason = "waiting_for_feedback"

        item = {
            "domain": domain,
            "key": key,
            "quality_tier": quality_tier,
            "status": status,
            "hits": hits,
            "misses": misses,
            "consecutive_misses": consecutive_misses,
            "superseded_by": superseded_by,
            "scenarios": len(scenarios),
            "idle_days": round(idle_days, 6),
            "action": action,
            "reason": reason,
        }
        patch_action = self._patch_action_for(
            domain=domain,
            key=key,
            action=action,
            reason=reason,
            evaluated_at=evaluated_at,
        )
        if patch_action:
            item["patch_action"] = patch_action
        return item

    @staticmethod
    def _patch_action_for(
        *,
        domain: str,
        key: str,
        action: str,
        reason: str,
        evaluated_at: str,
    ) -> Optional[Dict[str, Any]]:
        if action == "retain":
            return None
        if action == "cull":
            if domain == "alias":
                return {
                    "op": "delete_alias",
                    "canonical": key,
                    "reason": reason,
                }
            if domain == "rule":
                return {
                    "op": "delete_rule",
                    "rule_id": key,
                    "reason": reason,
                }
            if domain == "term":
                return {
                    "op": "remove_term",
                    "term": key,
                    "reason": reason,
                }
            raise ValueError(f"unsupported distillation domain: {domain}")

        op = {
            "alias": "distill_alias",
            "rule": "distill_rule",
            "term": "distill_term",
        }.get(domain)
        if not op:
            raise ValueError(f"unsupported distillation domain: {domain}")
        key_name = {
            "alias": "canonical",
            "rule": "rule_id",
            "term": "term",
        }[domain]
        patch_action: Dict[str, Any] = {
            "op": op,
            key_name: key,
            "quality_tier": "core" if action == "core" else "downweighted",
            "status": "active" if action == "core" else "downweighted",
            "last_evaluated_at": evaluated_at,
            "last_evaluation_reason": reason,
        }
        if domain == "rule":
            patch_action["enabled"] = action == "core"
        return patch_action
