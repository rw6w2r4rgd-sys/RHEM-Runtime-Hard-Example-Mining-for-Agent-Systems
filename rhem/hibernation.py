# -*- coding: utf-8 -*-
"""RHEM 参考实现：可选的记录生命周期与冬眠治理。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .models import ErrorCategory, Incident, NotFound


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


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_between(start: Optional[str], end: datetime) -> float:
    parsed = _parse_utc(start)
    if parsed is None:
        return 0.0
    return max((end - parsed).total_seconds() / 86400.0, 0.0)


@dataclass(frozen=True)
class HibernationDecision:
    """一次生命周期判断，供计划、补丁和审计复用。"""

    action: str
    reason: str
    domain: str
    key: str
    status: str
    idle_days: float
    phase_days: float
    hits: int
    misses: int
    consecutive_misses: int
    last_hit_at: Optional[str] = None
    hibernated_at: Optional[str] = None
    recycle_eligible_at: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "domain": self.domain,
            "key": self.key,
            "status": self.status,
            "idle_days": round(self.idle_days, 6),
            "phase_days": round(self.phase_days, 6),
            "hits": self.hits,
            "misses": self.misses,
            "consecutive_misses": self.consecutive_misses,
            "last_hit_at": self.last_hit_at,
            "hibernated_at": self.hibernated_at,
            "recycle_eligible_at": self.recycle_eligible_at,
            "evidence": copy.deepcopy(self.evidence),
        }


@dataclass
class HibernationManager:
    """只根据账本维护记录生命周期，不判断业务语义。

    状态机为：``active -> observing -> hibernating -> recycle_candidate``。
    唤醒会把 ``observing`` 或 ``hibernating`` 直接恢复为 ``active``。
    回收只生成候选，真正的删除必须由调用方显式提供人工批准。
    """

    observe_after_days: float = 30.0
    hibernate_after_days: float = 30.0
    recycle_after_days: float = 90.0

    def __post_init__(self) -> None:
        if self.observe_after_days < 0:
            raise ValueError("observe_after_days must be >= 0")
        if self.hibernate_after_days < 0:
            raise ValueError("hibernate_after_days must be >= 0")
        if self.recycle_after_days < 0:
            raise ValueError("recycle_after_days must be >= 0")

    def plan_library(
        self,
        records: Sequence[Dict[str, Any]],
        *,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成只读生命周期计划；自动动作只包含观察和冬眠。"""

        evaluated_at = _parse_utc(now) or datetime.now(timezone.utc)
        evaluated_text = _format_utc(evaluated_at)
        items: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        recycle_candidates: List[Dict[str, Any]] = []
        summary = {
            "observe": 0,
            "hibernate": 0,
            "recycle_candidate": 0,
            "retain": 0,
        }

        for row in records:
            decision = self._plan_record(row, now=evaluated_at)
            item = decision.to_dict()
            items.append(item)
            summary[decision.action] = summary.get(decision.action, 0) + 1
            if decision.action in {"observe", "hibernate"}:
                actions.append(self.state_action(decision, evaluated_at))
            elif decision.action == "recycle_candidate":
                recycle_candidates.append(item)

        return {
            "evaluated_at": evaluated_text,
            "summary": summary,
            "items": items,
            "actions": actions,
            "recycle_candidates": recycle_candidates,
        }

    def plan_wake(
        self,
        records: Sequence[Dict[str, Any]],
        incident: Incident,
        *,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发现同类错误复发时，生成恢复活跃状态的动作。"""

        evaluated_at = _parse_utc(now or incident.occurred_at) or datetime.now(
            timezone.utc
        )
        evaluated_text = _format_utc(evaluated_at)
        items: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        for row in records:
            hibernation = row["record"].get("hibernation") or {}
            current_status = hibernation.get("status") or "active"
            if current_status not in {"observing", "hibernating"}:
                continue
            if not self._matches_incident(row, incident):
                continue
            decision = HibernationDecision(
                action="wake",
                reason="recurrence_wake",
                domain=row["domain"],
                key=row["key"],
                status="active",
                idle_days=0.0,
                phase_days=0.0,
                hits=int((row["record"].get("distillation") or {}).get("hits", 0)),
                misses=int(
                    (row["record"].get("distillation") or {}).get("misses", 0)
                ),
                consecutive_misses=int(
                    (row["record"].get("distillation") or {}).get(
                        "consecutive_misses", 0
                    )
                ),
                evidence={
                    "previous_status": current_status,
                    "incident_id": incident.id,
                    "incident_source": incident.source,
                    "incident_family": incident.family,
                },
            )
            items.append(decision.to_dict())
            actions.append(self.state_action(decision, evaluated_at))

        return {
            "evaluated_at": evaluated_text,
            "summary": {"wake": len(actions)},
            "items": items,
            "actions": actions,
        }

    def plan_recycle(
        self,
        records: Sequence[Dict[str, Any]],
        *,
        domain: str,
        key: str,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """校验一条冬眠记录是否达到人工回收候选条件。"""

        evaluated_at = _parse_utc(now) or datetime.now(timezone.utc)
        evaluated_text = _format_utc(evaluated_at)
        row = next(
            (
                item
                for item in records
                if item["domain"] == domain and item["key"] == key
            ),
            None,
        )
        if row is None:
            raise NotFound(f"hibernation record not found: {domain}:{key}")
        decision = self._plan_record(row, now=evaluated_at)
        if decision.action != "recycle_candidate":
            raise NotFound(
                "record is not eligible for hibernation recycling: "
                f"{domain}:{key} ({decision.reason})"
            )
        return {
            "evaluated_at": evaluated_text,
            "decision": decision.to_dict(),
            "action": self.recycle_action(domain, key),
            "drift_sample": copy.deepcopy(decision.evidence),
        }

    def plan_manual_wake(
        self,
        records: Sequence[Dict[str, Any]],
        *,
        domain: str,
        key: str,
        reason: str,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成人工唤醒计划；只允许恢复观察期或冬眠期记录。"""

        evaluated_at = _parse_utc(now) or datetime.now(timezone.utc)
        evaluated_text = _format_utc(evaluated_at)
        row = next(
            (
                item
                for item in records
                if item["domain"] == domain and item["key"] == key
            ),
            None,
        )
        if row is None:
            raise NotFound(f"hibernation record not found: {domain}:{key}")
        record = row["record"]
        hibernation = record.get("hibernation") or {}
        current_status = hibernation.get("status") or "active"
        if current_status == "active":
            raise NotFound(f"record is already active: {domain}:{key}")
        distillation = record.get("distillation") or {}
        decision = HibernationDecision(
            action="wake",
            reason=reason,
            domain=domain,
            key=key,
            status="active",
            idle_days=0.0,
            phase_days=0.0,
            hits=int(distillation.get("hits", 0)),
            misses=int(distillation.get("misses", 0)),
            consecutive_misses=int(
                distillation.get("consecutive_misses", 0)
            ),
            evidence={
                "previous_status": current_status,
                "manual_wake": True,
            },
        )
        return {
            "evaluated_at": evaluated_text,
            "decision": decision.to_dict(),
            "action": self.state_action(decision, evaluated_at),
        }

    def state_action(
        self,
        decision: HibernationDecision,
        evaluated_at: datetime,
    ) -> Dict[str, Any]:
        return {
            "op": "set_hibernation_state",
            "domain": decision.domain,
            "key": decision.key,
            "status": decision.status,
            "reason": decision.reason,
            "evaluated_at": _format_utc(evaluated_at),
            "evidence": copy.deepcopy(decision.evidence),
        }

    @staticmethod
    def recycle_action(domain: str, key: str) -> Dict[str, Any]:
        if domain == "alias":
            return {
                "op": "delete_alias",
                "canonical": key,
                "reason": "hibernation_recycle",
            }
        if domain == "rule":
            return {
                "op": "delete_rule",
                "rule_id": key,
                "reason": "hibernation_recycle",
            }
        if domain == "term":
            return {
                "op": "remove_term",
                "term": key,
                "reason": "hibernation_recycle",
            }
        raise ValueError(f"unsupported hibernation domain: {domain}")

    def _plan_record(
        self,
        row: Dict[str, Any],
        *,
        now: datetime,
    ) -> HibernationDecision:
        record = row["record"]
        domain = row["domain"]
        key = row["key"]
        hibernation = record.get("hibernation") or {}
        distillation = record.get("distillation") or {}
        status = hibernation.get("status") or "active"
        hits = int(distillation.get("hits", 0))
        misses = int(distillation.get("misses", 0))
        consecutive_misses = int(distillation.get("consecutive_misses", 0))
        last_hit_at = distillation.get("last_hit_at")
        created_at = record.get("created_at") or distillation.get("created_at")
        activity_at = last_hit_at or created_at
        idle_days = _days_between(activity_at, now)
        evidence = {
            "hits": hits,
            "misses": misses,
            "consecutive_misses": consecutive_misses,
            "last_hit_at": last_hit_at,
            "last_miss_at": distillation.get("last_miss_at"),
            "last_feedback_at": distillation.get("last_feedback_at"),
            "scenario_count": len(distillation.get("scenario_sources") or []),
            "idle_days": round(idle_days, 6),
            "lifecycle_status": status,
        }

        if distillation.get("status") == "culled":
            return self._decision(
                action="retain",
                reason="already_culled",
                domain=domain,
                key=key,
                status=status,
                idle_days=idle_days,
                phase_days=0.0,
                hits=hits,
                misses=misses,
                consecutive_misses=consecutive_misses,
                last_hit_at=last_hit_at,
                hibernated_at=hibernation.get("hibernated_at"),
                recycle_eligible_at=None,
                evidence=evidence,
            )
        if (
            distillation.get("quality_tier") == "downweighted"
            or distillation.get("status") == "downweighted"
            or distillation.get("superseded_by")
        ):
            return self._decision(
                action="retain",
                reason="distillation_already_downweighted",
                domain=domain,
                key=key,
                status=status,
                idle_days=idle_days,
                phase_days=0.0,
                hits=hits,
                misses=misses,
                consecutive_misses=consecutive_misses,
                last_hit_at=last_hit_at,
                hibernated_at=hibernation.get("hibernated_at"),
                recycle_eligible_at=None,
                evidence=evidence,
            )

        if status == "hibernating":
            hibernated_at = hibernation.get("hibernated_at") or activity_at
            phase_days = _days_between(hibernated_at, now)
            eligible_at = self._eligible_at(hibernated_at)
            action = (
                "recycle_candidate"
                if phase_days >= self.recycle_after_days
                else "retain"
            )
            reason = (
                "hibernation_aged_out"
                if action == "recycle_candidate"
                else "hibernating_waiting_for_recycle"
            )
            return self._decision(
                action=action,
                reason=reason,
                domain=domain,
                key=key,
                status=status,
                idle_days=idle_days,
                phase_days=phase_days,
                hits=hits,
                misses=misses,
                consecutive_misses=consecutive_misses,
                last_hit_at=last_hit_at,
                hibernated_at=hibernated_at,
                recycle_eligible_at=eligible_at,
                evidence=evidence,
            )

        if status == "observing":
            observation_at = hibernation.get("observation_started_at") or activity_at
            phase_days = _days_between(observation_at, now)
            action = (
                "hibernate"
                if phase_days >= self.hibernate_after_days
                else "retain"
            )
            reason = (
                "observation_expired_without_hit"
                if action == "hibernate"
                else "observing_waiting_for_recovery"
            )
            return self._decision(
                action=action,
                reason=reason,
                domain=domain,
                key=key,
                status="hibernating" if action == "hibernate" else status,
                idle_days=idle_days,
                phase_days=phase_days,
                hits=hits,
                misses=misses,
                consecutive_misses=consecutive_misses,
                last_hit_at=last_hit_at,
                hibernated_at=hibernation.get("hibernated_at"),
                recycle_eligible_at=None,
                evidence=evidence,
            )

        action = "observe" if idle_days >= self.observe_after_days else "retain"
        reason = (
            "long_zero_hit_window"
            if action == "observe"
            else "recent_activity_or_waiting"
        )
        return self._decision(
            action=action,
            reason=reason,
            domain=domain,
            key=key,
            status="observing" if action == "observe" else status,
            idle_days=idle_days,
            phase_days=0.0,
            hits=hits,
            misses=misses,
            consecutive_misses=consecutive_misses,
            last_hit_at=last_hit_at,
            hibernated_at=hibernation.get("hibernated_at"),
            recycle_eligible_at=None,
            evidence=evidence,
        )

    @staticmethod
    def _decision(
        *,
        action: str,
        reason: str,
        domain: str,
        key: str,
        status: str,
        idle_days: float,
        phase_days: float,
        hits: int,
        misses: int,
        consecutive_misses: int,
        last_hit_at: Optional[str],
        hibernated_at: Optional[str],
        recycle_eligible_at: Optional[str],
        evidence: Dict[str, Any],
    ) -> HibernationDecision:
        return HibernationDecision(
            action=action,
            reason=reason,
            domain=domain,
            key=key,
            status=status,
            idle_days=idle_days,
            phase_days=phase_days,
            hits=hits,
            misses=misses,
            consecutive_misses=consecutive_misses,
            last_hit_at=last_hit_at,
            hibernated_at=hibernated_at,
            recycle_eligible_at=recycle_eligible_at,
            evidence=evidence,
        )

    def _eligible_at(self, hibernated_at: Optional[str]) -> Optional[str]:
        parsed = _parse_utc(hibernated_at)
        if parsed is None:
            return None
        return _format_utc(
            parsed + timedelta(days=self.recycle_after_days)
        )

    @staticmethod
    def _matches_incident(row: Dict[str, Any], incident: Incident) -> bool:
        domain = row["domain"]
        key = row["key"]
        record = row["record"]
        evidence = incident.evidence or {}
        if incident.category == ErrorCategory.RECOGNITION:
            expected = evidence.get("canonical") or incident.expected
            return domain == "alias" and expected == key
        if incident.category == ErrorCategory.RULE_GAP:
            rule_key = evidence.get("rule_key")
            return domain == "rule" and (
                record.get("family") == incident.family or rule_key == key
            )
        if incident.category == ErrorCategory.KNOWLEDGE_GAP:
            term = evidence.get("term") or incident.family
            return domain == "term" and term == key
        return False
