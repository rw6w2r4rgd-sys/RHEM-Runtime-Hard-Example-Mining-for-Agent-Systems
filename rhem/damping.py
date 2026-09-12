# -*- coding: utf-8 -*-
"""RHEM 参考实现：可选的阻尼抑制与稳定性治理。"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from .models import ErrorCategory, Incident


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


def _unique(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(item for item in items if item))


def _fingerprint_for_action(action: Dict[str, Any]) -> Optional[str]:
    op = action.get("op")
    if op in {"add_alias", "delete_alias", "distill_alias"}:
        canonical = action.get("canonical")
        return f"alias:{canonical}" if canonical else None
    if op in {"upsert_rule", "delete_rule", "distill_rule"}:
        rule = action.get("rule") or {}
        if op == "upsert_rule":
            key = rule.get("family") or rule.get("id")
        else:
            key = action.get("rule_id")
        return f"rule:{key}" if key else None
    if op in {"add_term", "remove_term", "distill_term"}:
        if op == "add_term":
            key = (action.get("term") or {}).get("term")
        else:
            key = action.get("term")
        return f"term:{key}" if key else None
    if op == "set_process_settings":
        keys = sorted((action.get("settings") or {}).keys())
        return f"process:{','.join(keys)}" if keys else None
    return None


def _fingerprint_for_incident(incident: Incident) -> str:
    evidence = incident.evidence or {}
    if incident.category == ErrorCategory.RECOGNITION:
        key = evidence.get("canonical") or incident.expected or incident.family
        return f"alias:{key}"
    if incident.category == ErrorCategory.PROCESS:
        fix = evidence.get("fix") or {}
        key = evidence.get("damping_key")
        if not key and isinstance(fix, dict):
            key = ",".join(sorted(fix.keys()))
        return f"process:{key or incident.family}"
    if incident.category == ErrorCategory.RULE_GAP:
        key = evidence.get("rule_key") or incident.family
        return f"rule:{key}"
    if incident.category == ErrorCategory.KNOWLEDGE_GAP:
        key = evidence.get("term") or incident.family
        return f"term:{key}"
    return incident.family


@dataclass(frozen=True)
class DampingDecision:
    """一次阻尼判断，供调用方审计、落补丁或继续执行。"""

    allowed: bool
    action: str
    reason: str
    fingerprint: str
    domain: str
    coefficient: float
    oscillations: int = 0
    regret_events: int = 0
    regret_rate: float = 0.0
    cooldown_until: Optional[str] = None
    requires_human: bool = False
    evidence: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "action": self.action,
            "reason": self.reason,
            "fingerprint": self.fingerprint,
            "domain": self.domain,
            "coefficient": self.coefficient,
            "oscillations": self.oscillations,
            "regret_events": self.regret_events,
            "regret_rate": self.regret_rate,
            "cooldown_until": self.cooldown_until,
            "requires_human": self.requires_human,
            "evidence": copy.deepcopy(self.evidence),
        }


@dataclass
class DampingSuppressor:
    """只调整自动进化的稳定性边界，不判断业务对错。"""

    dead_zone_occurrences: int = 2
    oscillation_window: int = 8
    oscillation_cycles: int = 1
    regret_min_events: int = 2
    regret_rate_threshold: float = 0.5
    base_cooldown_hours: float = 24.0
    min_coefficient: float = 0.2
    max_coefficient: float = 0.9

    def __post_init__(self) -> None:
        if self.dead_zone_occurrences < 1:
            raise ValueError("dead_zone_occurrences must be >= 1")
        if self.oscillation_window < 3:
            raise ValueError("oscillation_window must be >= 3")
        if self.oscillation_cycles < 1:
            raise ValueError("oscillation_cycles must be >= 1")
        if self.regret_min_events < 1:
            raise ValueError("regret_min_events must be >= 1")
        if not 0 <= self.regret_rate_threshold <= 1:
            raise ValueError("regret_rate_threshold must be between 0 and 1")
        if self.base_cooldown_hours <= 0:
            raise ValueError("base_cooldown_hours must be > 0")
        if not 0 <= self.min_coefficient <= self.max_coefficient <= 1:
            raise ValueError("coefficient bounds must satisfy 0 <= min <= max <= 1")

    def fingerprint_for_incident(self, incident: Incident) -> str:
        return _fingerprint_for_incident(incident)

    def evaluate(
        self,
        store: Any,
        incident: Incident,
        occurrences: int,
        *,
        now: Optional[str] = None,
    ) -> DampingDecision:
        evaluated_at = (
            _parse_utc(now or incident.occurred_at)
            or datetime.now(timezone.utc)
        )
        fingerprint = self.fingerprint_for_incident(incident)
        domain = incident.category.value
        state = store.view().get("damping_state") or {}
        control = (state.get("controls") or {}).get(fingerprint)
        if control:
            frozen_until = _parse_utc(control.get("frozen_until"))
            if frozen_until and evaluated_at < frozen_until:
                return DampingDecision(
                    allowed=False,
                    action="cooldown",
                    reason="damping_cooldown_active",
                    fingerprint=fingerprint,
                    domain=domain,
                    coefficient=float(control.get("coefficient", self.min_coefficient)),
                    cooldown_until=control.get("frozen_until"),
                    requires_human=True,
                    evidence={"control": copy.deepcopy(control)},
                )
            return DampingDecision(
                allowed=True,
                action="thaw",
                reason="damping_cooldown_expired",
                fingerprint=fingerprint,
                domain=domain,
                coefficient=self.min_coefficient,
                cooldown_until=control.get("frozen_until"),
                requires_human=False,
                evidence={"control": copy.deepcopy(control)},
            )

        history = self._patch_history(store.list_patches())
        stats = self._analyze(history, fingerprint)
        coefficient = min(
            self.max_coefficient,
            self.min_coefficient
            + 0.15 * stats["oscillations"]
            + 0.15 * stats["regret_events"],
        )
        if occurrences < self.dead_zone_occurrences:
            return DampingDecision(
                allowed=False,
                action="dead_zone",
                reason="damping_dead_zone",
                fingerprint=fingerprint,
                domain=domain,
                coefficient=coefficient,
                oscillations=stats["oscillations"],
                regret_events=stats["regret_events"],
                regret_rate=stats["regret_rate"],
                evidence=stats["evidence"],
            )
        if stats["oscillations"] >= self.oscillation_cycles:
            return self._freeze_decision(
                fingerprint=fingerprint,
                domain=domain,
                action="freeze_oscillation",
                reason="oscillation_detected",
                coefficients=coefficient,
                evaluated_at=evaluated_at,
                stats=stats,
            )
        if (
            stats["regret_events"] >= self.regret_min_events
            and stats["regret_rate"] >= self.regret_rate_threshold
        ):
            return self._freeze_decision(
                fingerprint=fingerprint,
                domain=domain,
                action="freeze_regret",
                reason="regret_rate_exceeded",
                coefficients=coefficient,
                evaluated_at=evaluated_at,
                stats=stats,
            )
        return DampingDecision(
            allowed=True,
            action="allow",
            reason="damping_stable",
            fingerprint=fingerprint,
            domain=domain,
            coefficient=coefficient,
            oscillations=stats["oscillations"],
            regret_events=stats["regret_events"],
            regret_rate=stats["regret_rate"],
            evidence=stats["evidence"],
        )

    def control_action(self, decision: DampingDecision) -> Dict[str, Any]:
        return {
            "op": "set_damping_control",
            "key": decision.fingerprint,
            "domain": decision.domain,
            "status": "frozen",
            "reason": decision.reason,
            "coefficient": decision.coefficient,
            "frozen_until": decision.cooldown_until,
            "requires_human": decision.requires_human,
            "evidence": copy.deepcopy(decision.evidence),
        }

    def clear_action(self, fingerprint: str) -> Dict[str, Any]:
        return {
            "op": "clear_damping_control",
            "key": fingerprint,
        }

    def downweight_actions(
        self,
        store: Any,
        fingerprint: str,
    ) -> List[Dict[str, Any]]:
        if not fingerprint.startswith("rule:"):
            return []
        target = fingerprint.split(":", 1)[1]
        actions: List[Dict[str, Any]] = []
        for rule_id, rule in store.view()["operational_rules"].items():
            family = rule.get("family") or rule_id
            metadata = rule.get("distillation") or {}
            already_downweighted = (
                metadata.get("status") == "downweighted"
                or metadata.get("quality_tier") == "downweighted"
            )
            if target not in {rule_id, family} or already_downweighted:
                continue
            actions.append({
                "op": "distill_rule",
                "rule_id": rule_id,
                "quality_tier": "downweighted",
                "status": "downweighted",
                "enabled": False,
                "last_evaluation_reason": "damping_regret",
            })
        return actions

    def _freeze_decision(
        self,
        *,
        fingerprint: str,
        domain: str,
        action: str,
        reason: str,
        coefficients: float,
        evaluated_at: datetime,
        stats: Dict[str, Any],
    ) -> DampingDecision:
        hours = self.base_cooldown_hours * (1.0 + coefficients)
        until = evaluated_at + timedelta(hours=hours)
        return DampingDecision(
            allowed=False,
            action=action,
            reason=reason,
            fingerprint=fingerprint,
            domain=domain,
            coefficient=coefficients,
            oscillations=stats["oscillations"],
            regret_events=stats["regret_events"],
            regret_rate=stats["regret_rate"],
            cooldown_until=_format_utc(until),
            requires_human=True,
            evidence=stats["evidence"],
        )

    def _patch_history(self, patches: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        history: List[Dict[str, Any]] = []
        for patch in patches:
            fingerprints = _unique([
                fingerprint
                for action in patch.get("actions") or []
                for fingerprint in [_fingerprint_for_action(action)]
                if fingerprint
            ])
            for fingerprint in fingerprints:
                history.append({
                    "seq": int(patch.get("seq", 0)),
                    "patch_id": patch.get("id"),
                    "status": patch.get("status"),
                    "fingerprint": fingerprint,
                })
        return history

    def _analyze(
        self,
        history: Sequence[Dict[str, Any]],
        fingerprint: str,
    ) -> Dict[str, Any]:
        window = list(history)[-self.oscillation_window:]
        sequence = [row["fingerprint"] for row in window]
        oscillations = 0
        for index in range(2, len(sequence)):
            left = sequence[index - 2]
            middle = sequence[index - 1]
            right = sequence[index]
            if left == right and left != middle and fingerprint in {left, middle}:
                oscillations += 1

        target_statuses = [
            row["status"]
            for row in history
            if row["fingerprint"] == fingerprint
        ]
        applies = target_statuses.count("active")
        rollbacks = target_statuses.count("rolled_back")
        reactivations = sum(
            1
            for previous, current in zip(target_statuses, target_statuses[1:])
            if previous == "rolled_back" and current == "active"
        )
        regret_rate = reactivations / applies if applies else 0.0
        return {
            "oscillations": oscillations,
            "applies": applies,
            "rollbacks": rollbacks,
            "regret_events": reactivations,
            "regret_rate": regret_rate,
            "evidence": {
                "window": copy.deepcopy(window),
                "target_statuses": target_statuses[-self.oscillation_window:],
            },
        }
