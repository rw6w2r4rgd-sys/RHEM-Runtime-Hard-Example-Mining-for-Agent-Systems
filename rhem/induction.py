# -*- coding: utf-8 -*-
"""RHEM 蒸馏反哺：从高纯难例账本归纳候选，不替代人工判断。"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .models import new_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _stable_key(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _family_root(family: Optional[str]) -> Optional[str]:
    value = _clean_text(family)
    if not value:
        return None
    parts = value.split(":")
    if len(parts) >= 2:
        return ":".join(parts[:2])
    return value


def _structure_key(record: Dict[str, Any]) -> Optional[str]:
    explicit = _clean_text(record.get("structure_key"))
    if explicit:
        return explicit
    return _family_root(record.get("family"))


def _is_core(record: Dict[str, Any]) -> bool:
    metadata = record.get("distillation") or {}
    return (
        metadata.get("status", "active") == "active"
        and metadata.get("quality_tier") == "core"
    )


def _record_ref(row: Dict[str, Any]) -> Dict[str, str]:
    return {"domain": row["domain"], "key": row["key"]}


def _merge_sources(rows: Iterable[Dict[str, Any]]) -> List[str]:
    sources: List[str] = []
    for row in rows:
        record = row.get("record") or {}
        for source in record.get("sources") or []:
            if source not in sources:
                sources.append(source)
    return sources


@dataclass(frozen=True)
class InductionFinding:
    """一条只基于账本证据的归纳候选。"""

    kind: str
    key: str
    title: str
    detail: str
    priority_score: int
    evidence: Dict[str, Any]
    suggested: Dict[str, Any]
    requires_human: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "detail": self.detail,
            "priority_score": self.priority_score,
            "evidence": copy.deepcopy(self.evidence),
            "suggested": copy.deepcopy(self.suggested),
            "requires_human": self.requires_human,
            "not_verified": True,
        }


@dataclass
class DistillateInducer:
    """把已蒸馏的高纯账本归纳为候选规则、结构弱点和隐患预测。

    归纳只使用记录质量、反馈命中/失败、来源覆盖、family 和 structure_key。
    它不调用 LLM，不推断语义覆盖，也不会自动把候选写入运行库。
    """

    min_rule_support: int = 2
    min_structural_support: int = 3
    min_hazard_occurrences: int = 3
    min_hazard_sources: int = 2

    def __post_init__(self) -> None:
        if self.min_rule_support < 2:
            raise ValueError("min_rule_support must be >= 2")
        if self.min_structural_support < 2:
            raise ValueError("min_structural_support must be >= 2")
        if self.min_hazard_occurrences < 1:
            raise ValueError("min_hazard_occurrences must be >= 1")
        if self.min_hazard_sources < 1:
            raise ValueError("min_hazard_sources must be >= 1")

    def induce(
        self,
        records: Sequence[Dict[str, Any]],
        groups: Sequence[Dict[str, Any]],
        *,
        now: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成只读候选清单；持久化和批准由 LearningEngine 负责。"""

        evaluated_at = now or _utc_now()
        findings: List[Dict[str, Any]] = []
        findings.extend(self._rule_templates(records, evaluated_at))
        findings.extend(self._structural_weaknesses(records, evaluated_at))
        findings.extend(self._hazard_predictions(groups, evaluated_at))
        findings.sort(
            key=lambda item: (
                item["kind"],
                -int(item["priority_score"]),
                item["key"],
            )
        )
        summary = {
            "rule_template": sum(
                item["kind"] == "rule_template" for item in findings
            ),
            "structural_weakness": sum(
                item["kind"] == "structural_weakness" for item in findings
            ),
            "hazard_prediction": sum(
                item["kind"] == "hazard_prediction" for item in findings
            ),
        }
        return {
            "schema": "rhem.induction.1",
            "evaluated_at": evaluated_at,
            "summary": summary,
            "findings": findings,
            "requires_human_approval": bool(findings),
            "automatic_changes": False,
        }

    def _rule_templates(
        self,
        records: Sequence[Dict[str, Any]],
        evaluated_at: str,
    ) -> List[Dict[str, Any]]:
        buckets: Dict[tuple, List[Dict[str, Any]]] = {}
        for row in records:
            if row.get("domain") != "rule":
                continue
            record = row.get("record") or {}
            if not _is_core(record):
                continue
            action = _clean_text(record.get("action"))
            condition = _clean_text(record.get("condition"))
            structure = _structure_key(record) or "unscoped"
            if not action or not condition:
                continue
            buckets.setdefault((action, structure), []).append(row)

        findings: List[Dict[str, Any]] = []
        for (action, structure), rows in sorted(buckets.items()):
            if len(rows) < self.min_rule_support:
                continue
            conditions = []
            for row in rows:
                condition = _clean_text((row.get("record") or {}).get("condition"))
                if condition and condition not in conditions:
                    conditions.append(condition)
            if len(conditions) < self.min_rule_support:
                continue
            sources = _merge_sources(rows)
            refs = [_record_ref(row) for row in rows]
            structure_keys = [structure]

            template_key = _stable_key(
                action + "\n" + structure + "\n" + "\n".join(conditions)
            )
            rule = {
                "id": new_id("rule"),
                "name": f"通用规则模板：{action[:36]}",
                "description": (
                    f"由 {len(rows)} 条 core 规则账本归纳；"
                    "条件边界必须人工审核，不能视为已验证规则。"
                ),
                "condition": " OR ".join(
                    f"({condition})" for condition in conditions
                ),
                "action": (rows[0].get("record") or {}).get("action"),
                "family": f"induction:rule_template:{structure}:{template_key}",
                "sources": sources,
                "enabled": True,
                "created_at": evaluated_at,
                "induced": {
                    "kind": "rule_template",
                    "evidence": refs,
                    "not_verified": True,
                },
            }
            if structure_keys:
                rule["structure_key"] = structure_keys[0]
            findings.append(InductionFinding(
                kind="rule_template",
                key=f"rule_template:{template_key}",
                title=rule["name"],
                detail=(
                    "同一结构根因下，同一动作在多个 core 规则条件下"
                    "重复出现；批准后仍通过规则补丁写入并可回滚。"
                ),
                priority_score=len(rows) * 2 + len(sources),
                evidence={
                    "structure_key": structure,
                    "record_refs": refs,
                    "conditions": conditions,
                    "sources": sources,
                },
                suggested={"rule": rule},
            ).to_dict())
        return findings

    def _structural_weaknesses(
        self,
        records: Sequence[Dict[str, Any]],
        evaluated_at: str,
    ) -> List[Dict[str, Any]]:
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for row in records:
            record = row.get("record") or {}
            if not _is_core(record):
                continue
            structure = _structure_key(record)
            if not structure:
                continue
            buckets.setdefault(structure, []).append(row)

        findings: List[Dict[str, Any]] = []
        for structure, rows in sorted(buckets.items()):
            if len(rows) < self.min_structural_support:
                continue
            domains = sorted({row["domain"] for row in rows})
            sources = _merge_sources(rows)
            misses = sum(
                int(((row.get("record") or {}).get("distillation") or {})
                    .get("misses", 0))
                for row in rows
            )
            refs = [_record_ref(row) for row in rows]
            findings.append(InductionFinding(
                kind="structural_weakness",
                key=f"structural_weakness:{structure}",
                title=f"结构弱点候选：{structure}",
                detail=(
                    f"{len(rows)} 条核心记录集中在该结构键，覆盖 "
                    f"{len(domains)} 个域；这是人工设计复核候选，"
                    "不是已证明的缺陷。"
                ),
                priority_score=(
                    len(rows) * 2 + len(sources) + len(domains) + misses
                ),
                evidence={
                    "structure_key": structure,
                    "domains": domains,
                    "sources": sources,
                    "record_refs": refs,
                    "misses": misses,
                },
                suggested={
                    "kind": "structural_weakness",
                    "structure_key": structure,
                    "recommended_action": "human_design_review",
                    "not_verified": True,
                },
            ).to_dict())
        return findings

    def _hazard_predictions(
        self,
        groups: Sequence[Dict[str, Any]],
        evaluated_at: str,
    ) -> List[Dict[str, Any]]:
        buckets: Dict[tuple, List[Dict[str, Any]]] = {}
        for group in groups:
            if group.get("distillation_tier") != "high":
                continue
            if group.get("status") not in {"pending", "needs_human"}:
                continue
            family = _clean_text(group.get("family"))
            category = _clean_text(group.get("category"))
            root = _family_root(family) or family
            if not family or not root:
                continue
            buckets.setdefault((category, root), []).append(group)

        findings: List[Dict[str, Any]] = []
        for (category, root), rows in sorted(buckets.items()):
            occurrences = sum(int(row.get("occurrences", 0)) for row in rows)
            sources = sorted({
                source
                for row in rows
                for source in (row.get("sources") or [])
            })
            if (
                occurrences < self.min_hazard_occurrences
                and len(sources) < self.min_hazard_sources
            ):
                continue
            family_refs = sorted(str(row.get("family") or "") for row in rows)
            findings.append(InductionFinding(
                kind="hazard_prediction",
                key=f"hazard_prediction:{category}:{root}",
                title=f"隐患预测候选：{root}",
                detail=(
                    "高价值待处理难例在该结构附近聚集；"
                    "建议人工决定是否新增探针或扩大复查范围。"
                ),
                priority_score=occurrences * 2 + len(sources) * 3,
                evidence={
                    "category": category,
                    "structure_key": root,
                    "families": family_refs,
                    "occurrences": occurrences,
                    "sources": sources,
                },
                suggested={
                    "kind": "hazard_prediction",
                    "structure_key": root,
                    "recommended_action": "human_probe_review",
                    "not_verified": True,
                },
            ).to_dict())
        return findings
