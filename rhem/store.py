# -*- coding: utf-8 -*-
"""RHEM 参考实现：带护栏域的持久化记忆库与补丁治理。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import (
    GuardrailViolation,
    HardExampleGroup,
    Incident,
    NotFound,
    RollbackOrderError,
    new_id,
    utc_now,
)

MEMORY_SCHEMA = "rhem.memory.1"
GUARDRAIL_SCHEMA = "rhem.guardrails.1"

DEFAULT_PROCESS_SETTINGS: Dict[str, Any] = {
    "max_retries": 3,
    "tool_timeout_s": 60.0,
    "deadlock_detection_s": 10.0,
}

def _default_distillation(created_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "quality_tier": "standard",
        "status": "active",
        "hits": 0,
        "misses": 0,
        "consecutive_misses": 0,
        "scenario_sources": [],
        "last_hit_at": None,
        "last_miss_at": None,
        "last_feedback_at": None,
        "last_evaluated_at": None,
        "last_evaluation_reason": None,
        "superseded_by": None,
        "superseded_at": None,
        "patch_id": None,
        "created_at": created_at or utc_now(),
    }


def _default_hibernation(created_at: Optional[str] = None) -> Dict[str, Any]:
    entered_at = created_at or utc_now()
    return {
        "status": "active",
        "reason": "initial",
        "entered_at": entered_at,
        "last_transition_at": entered_at,
        "observation_started_at": None,
        "hibernated_at": None,
        "last_wake_at": None,
        "patch_id": None,
        "history": [],
    }


DEFAULT_GUARDRAILS: Dict[str, Any] = {
    "schema": GUARDRAIL_SCHEMA,
    "frozen": True,
    "editable_domains": [
        "alias_rules",
        "operational_rules",
        "terms",
        "induction_findings",
        "process_settings",
        "damping_state",
    ],
    "protected_domains": [
        "guardrails",
        "model_weights",
        "system_prompt",
        "rhem_code",
        "source_records",
    ],
    "hard_constraints": [
        "运行期禁止改写模型权重与系统提示词",
        "自动变更必须生成补丁、保留快照并支持回滚",
        "护栏域冻结；护栏变更只能走人工维护通道",
        "难例来源与原始证据只追加、不覆盖",
        "蒸馏反哺候选必须人工批准；结构弱点和隐患不得自动执行",
        "阻尼冷却只冻结自动进化动作，不冻结人工审批",
        "冬眠回收必须人工批准；自动冬眠不得删除记录",
        "冬眠不冻结人工查看与人工唤醒",
    ],
}


class RhemStore:
    """JSON 持久化的 RHEM 记忆库。

    默认存到 ``root`` 下的 ``memory.json``；每次自动变更都会在
    ``patches/`` 保留 before/after 快照，并追加事件日志。
    """

    def __init__(self, root: Any = None) -> None:
        self.root = Path(root or "rhem_store").expanduser().resolve()
        self.patch_dir = self.root / "patches"
        self.memory_file = self.root / "memory.json"
        self.guardrail_file = self.root / "guardrails.json"
        self.event_file = self.root / "events.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)
        self.patch_dir.mkdir(parents=True, exist_ok=True)
        self.guardrails = self._load_guardrails()
        self.state = self._load_memory()

    # ----------------------------------------------------------
    # 持久化
    # ----------------------------------------------------------
    def _load_guardrails(self) -> Dict[str, Any]:
        if self.guardrail_file.exists():
            data = json.loads(self.guardrail_file.read_text(encoding="utf-8"))
            editable = list(data.get("editable_domains") or [])
            if "induction_findings" not in editable:
                editable.append("induction_findings")
                data["editable_domains"] = editable
            constraints = list(data.get("hard_constraints") or [])
            induction_constraint = (
                "蒸馏反哺候选必须人工批准；结构弱点和隐患不得自动执行"
            )
            if induction_constraint not in constraints:
                constraints.append(induction_constraint)
                data["hard_constraints"] = constraints
            if "damping_state" not in editable:
                editable.append("damping_state")
                data["editable_domains"] = editable
            damping_constraint = "阻尼冷却只冻结自动进化动作，不冻结人工审批"
            constraints = list(data.get("hard_constraints") or [])
            if damping_constraint not in constraints:
                constraints.append(damping_constraint)
                data["hard_constraints"] = constraints
            hibernation_constraints = [
                "冬眠回收必须人工批准；自动冬眠不得删除记录",
                "冬眠不冻结人工查看与人工唤醒",
            ]
            constraints = list(data.get("hard_constraints") or [])
            changed = False
            for constraint in hibernation_constraints:
                if constraint not in constraints:
                    constraints.append(constraint)
                    changed = True
            if changed:
                data["hard_constraints"] = constraints
            self._write_json(self.guardrail_file, data)
            return data
        self._write_json(self.guardrail_file, DEFAULT_GUARDRAILS)
        return copy.deepcopy(DEFAULT_GUARDRAILS)

    def _default_memory(self) -> Dict[str, Any]:
        now = utc_now()
        return {
            "schema": MEMORY_SCHEMA,
            "created_at": now,
            "updated_at": now,
            "next_patch_seq": 1,
            "process_settings": copy.deepcopy(DEFAULT_PROCESS_SETTINGS),
            "alias_rules": {},
            "operational_rules": {},
            "terms": {},
            "incidents": [],
            "hard_examples": {},
            "clusters": {},
            "prescriptions": {},
            "induction_findings": {},
            "damping_state": {
                "controls": {},
                "events": [],
            },
            "patch_meta": {},
        }

    def _load_memory(self) -> Dict[str, Any]:
        if self.memory_file.exists():
            data = json.loads(self.memory_file.read_text(encoding="utf-8"))
            data.setdefault("next_patch_seq", max(
                [int(pid[1:]) for pid in data.get("patch_meta", {})] or [0]
            ) + 1)
            data.setdefault("process_settings", copy.deepcopy(DEFAULT_PROCESS_SETTINGS))
            data.setdefault("alias_rules", {})
            data.setdefault("operational_rules", {})
            data.setdefault("terms", {})
            data.setdefault("incidents", [])
            data.setdefault("hard_examples", {})
            data.setdefault("clusters", {})
            data.setdefault("prescriptions", {})
            data.setdefault("induction_findings", {})
            data.setdefault("damping_state", {"controls": {}, "events": []})
            data["damping_state"].setdefault("controls", {})
            data["damping_state"].setdefault("events", [])
            data.setdefault("patch_meta", {})
            for group in data["hard_examples"].values():
                group.setdefault("occurrence_times", [])
                group.setdefault("gate_flags", {})
            for cluster in data["clusters"].values():
                cluster.setdefault("occurrence_times", [])
                cluster.setdefault("gate_flags", {})
            for collection in ("alias_rules", "operational_rules", "terms"):
                for record in data[collection].values():
                    record.setdefault(
                        "distillation",
                        _default_distillation(record.get("created_at")),
                    )
                    record["distillation"].setdefault("consecutive_misses", 0)
                    record["distillation"].setdefault("superseded_by", None)
                    record.setdefault(
                        "hibernation",
                        _default_hibernation(record.get("created_at")),
                    )
            return data
        data = self._default_memory()
        self._write_json(self.memory_file, data)
        return data

    def _write_json(self, path: Path, data: Any) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save(self) -> None:
        self.state["updated_at"] = utc_now()
        self._write_json(self.memory_file, self.state)

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        row = {
            "kind": kind,
            "at": utc_now(),
            **payload,
        }
        with self.event_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _patch_path(self, patch_id: str) -> Path:
        return self.patch_dir / f"{patch_id}.json"

    # ----------------------------------------------------------
    # 只读视图
    # ----------------------------------------------------------
    def view(self) -> Dict[str, Any]:
        return copy.deepcopy(self.state)

    def guardrail_view(self) -> Dict[str, Any]:
        return copy.deepcopy(self.guardrails)

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        for row in self.state["incidents"]:
            if row["id"] == incident_id:
                return copy.deepcopy(row)
        raise NotFound(f"incident not found: {incident_id}")

    def list_incidents(self, active_only: bool = True) -> List[Dict[str, Any]]:
        rows = self.state["incidents"]
        if active_only:
            rows = [r for r in rows if r.get("active", True)]
        return copy.deepcopy(rows)

    def get_group(self, family: str) -> Optional[Dict[str, Any]]:
        data = self.state["hard_examples"].get(family)
        return copy.deepcopy(data) if data else None

    def get_prescription(self, proposal_id: str) -> Dict[str, Any]:
        row = self.state["prescriptions"].get(proposal_id)
        if not row:
            raise NotFound(f"proposal not found: {proposal_id}")
        return copy.deepcopy(row)

    def list_distillation_records(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for domain, collection in (
            ("alias", "alias_rules"),
            ("rule", "operational_rules"),
            ("term", "terms"),
        ):
            for key, record in self.state[collection].items():
                rows.append({
                    "domain": domain,
                    "key": key,
                    "record": copy.deepcopy(record),
                })
        return rows

    def get_induction_finding(self, finding_id: str) -> Dict[str, Any]:
        row = self.state["induction_findings"].get(finding_id)
        if not row:
            raise NotFound(f"induction finding not found: {finding_id}")
        return copy.deepcopy(row)

    def list_induction_findings(
        self,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = list(self.state["induction_findings"].values())
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: (
            -int(row.get("priority_score", 0)),
            row.get("kind", ""),
            row.get("key", ""),
        ))
        return copy.deepcopy(rows)

    def list_patches(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for patch_id in sorted(
            self.state["patch_meta"],
            key=lambda value: int(value[1:]),
        ):
            path = self._patch_path(patch_id)
            if not path.exists():
                continue
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        return rows

    def create_induction_finding(
        self,
        finding: Dict[str, Any],
    ) -> Dict[str, Any]:
        """保存或复用一条待人工处理的归纳候选。"""

        fingerprint = str(finding.get("key") or "").strip()
        if not fingerprint:
            raise ValueError("induction finding key must not be empty")
        for existing in self.state["induction_findings"].values():
            if existing.get("fingerprint") != fingerprint:
                continue
            if existing.get("status") == "proposed":
                existing["updated_at"] = utc_now()
                existing["priority_score"] = int(
                    finding.get("priority_score", existing.get("priority_score", 0))
                )
                existing["detail"] = finding.get("detail", existing.get("detail"))
                existing["evidence"] = copy.deepcopy(
                    finding.get("evidence", existing.get("evidence"))
                )
                self._save()
            return {
                "finding": copy.deepcopy(existing),
                "created": False,
            }

        now = utc_now()
        finding_id = new_id("induction")
        record = copy.deepcopy(finding)
        record.update({
            "id": finding_id,
            "fingerprint": fingerprint,
            "status": "proposed",
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
            "approver": None,
            "patch_id": None,
        })
        self.state["induction_findings"][finding_id] = record
        self._save()
        self._log("induction_finding_created", {
            "finding_id": finding_id,
            "kind": record.get("kind"),
            "key": fingerprint,
            "priority_score": record.get("priority_score", 0),
        })
        return {
            "finding": copy.deepcopy(record),
            "created": True,
        }

    def log_induction_plan(self, plan: Dict[str, Any]) -> None:
        self._log("induction_planned", {
            "evaluated_at": plan.get("evaluated_at"),
            "summary": copy.deepcopy(plan.get("summary")),
            "finding_count": len(plan.get("findings") or []),
        })

    def record_feedback(
        self,
        domain: str,
        key: str,
        scenario: str,
        *,
        resolved: bool,
        at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """记录一次命中或失败反馈，作为库内回炼账本。"""

        collection = {
            "alias": "alias_rules",
            "alias_rule": "alias_rules",
            "rule": "operational_rules",
            "operational_rule": "operational_rules",
            "term": "terms",
            "knowledge_term": "terms",
        }.get(domain)
        if collection is None:
            raise NotFound(f"unsupported distillation domain: {domain}")
        if not scenario or not str(scenario).strip():
            raise ValueError("scenario must not be empty")
        record = self.state[collection].get(key)
        if not record:
            raise NotFound(f"distillation record not found: {domain}:{key}")

        now = at or utc_now()
        metadata = record.setdefault(
            "distillation",
            _default_distillation(record.get("created_at")),
        )
        metadata.setdefault("created_at", record.get("created_at") or now)
        scenarios = metadata.setdefault("scenario_sources", [])
        if scenario not in scenarios:
            scenarios.append(scenario)
            metadata["scenario_sources"] = scenarios[-200:]
        if resolved:
            metadata["hits"] = int(metadata.get("hits", 0)) + 1
            metadata["consecutive_misses"] = 0
            metadata["last_hit_at"] = now
        else:
            metadata["misses"] = int(metadata.get("misses", 0)) + 1
            metadata["consecutive_misses"] = (
                int(metadata.get("consecutive_misses", 0)) + 1
            )
            metadata["last_miss_at"] = now
        metadata["last_feedback_at"] = now
        self._save()
        self._log("distillation_feedback", {
            "domain": domain,
            "key": key,
            "scenario": scenario,
            "resolved": bool(resolved),
            "hits": metadata["hits"],
            "misses": metadata["misses"],
            "scenarios": len(metadata["scenario_sources"]),
        })
        return copy.deepcopy(record)

    def mark_superseded(
        self,
        domain: str,
        key: str,
        superseded_by: str,
        *,
        at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """显式记录一条规则被新规则替代；不自动猜语义覆盖关系。"""

        collection = {
            "alias": "alias_rules",
            "alias_rule": "alias_rules",
            "rule": "operational_rules",
            "operational_rule": "operational_rules",
            "term": "terms",
            "knowledge_term": "terms",
        }.get(domain)
        if collection is None:
            raise NotFound(f"unsupported distillation domain: {domain}")
        if not superseded_by or not str(superseded_by).strip():
            raise ValueError("superseded_by must not be empty")
        record = self.state[collection].get(key)
        if not record:
            raise NotFound(f"distillation record not found: {domain}:{key}")
        now = at or utc_now()
        metadata = record.setdefault(
            "distillation",
            _default_distillation(record.get("created_at")),
        )
        metadata["superseded_by"] = superseded_by
        metadata["superseded_at"] = now
        metadata["last_feedback_at"] = now
        self._save()
        self._log("distillation_superseded", {
            "domain": domain,
            "key": key,
            "superseded_by": superseded_by,
        })
        return copy.deepcopy(record)

    def mark_distillation_decision(
        self,
        family: str,
        decision: Dict[str, Any],
        *,
        cluster_key: Optional[str] = None,
    ) -> None:
        group = self.state["hard_examples"].get(family)
        if not group:
            raise NotFound(f"hard-example family not found: {family}")
        fields = {
            "distillation_tier": decision.get("tier"),
            "distillation_reason": decision.get("reason"),
            "distillation_signals": copy.deepcopy(decision.get("signals")),
            "distillation_evaluated_at": decision.get("evaluated_at"),
        }
        group.update(fields)
        if cluster_key:
            cluster = self.state["clusters"].get(cluster_key)
            if cluster:
                cluster.update(fields)
        self._save()
        self._log("distillation_evaluated", {
            "record_id": cluster_key or family,
            "decision": copy.deepcopy(decision),
        })

    def log_distillation_plan(self, plan: Dict[str, Any]) -> None:
        self._log("distillation_planned", {
            "evaluated_at": plan.get("evaluated_at"),
            "summary": copy.deepcopy(plan.get("summary")),
            "action_count": len(plan.get("actions") or []),
        })

    def log_hibernation_plan(self, plan: Dict[str, Any]) -> None:
        self._log("hibernation_planned", {
            "evaluated_at": plan.get("evaluated_at"),
            "summary": copy.deepcopy(plan.get("summary")),
            "action_count": len(plan.get("actions") or []),
            "recycle_candidate_count": len(
                plan.get("recycle_candidates") or []
            ),
        })

    def list_prescriptions(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.state["prescriptions"].values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return copy.deepcopy(rows)

    # ----------------------------------------------------------
    # 难例记录与门控
    # ----------------------------------------------------------
    def record_incident(
        self,
        incident: Incident,
        *,
        evidence_key: Optional[str] = None,
        deduplicate_evidence: bool = False,
    ) -> Dict[str, Any]:
        cluster_key = incident.cluster_key or incident.family
        gate_context = incident.evidence.get("gate") or {}
        if not isinstance(gate_context, dict):
            gate_context = {}
        gate_flags = {
            key: bool(gate_context[key])
            for key in ("high_risk", "manual_hold", "recheck_failed")
            if key in gate_context
        }
        existing_group = self.state["hard_examples"].get(incident.family)
        if (
            existing_group
            and existing_group.get("category") != incident.category.value
        ):
            raise ValueError(
                "family already belongs to another category: "
                f"{existing_group['category']}"
            )
        existing_cluster = self.state["clusters"].get(cluster_key)
        if (
            existing_cluster
            and existing_cluster.get("category") != incident.category.value
        ):
            raise ValueError(
                "cluster already belongs to another category: "
                f"{existing_cluster['category']}"
            )

        self.state["incidents"].append(incident.to_dict())
        group = existing_group
        if group is None:
            group = {
                "family": incident.family,
                "category": incident.category.value,
                "occurrences": 0,
                "sources": [],
                "incident_ids": [],
                "cluster_keys": [],
                "occurrence_times": [],
                "gate_flags": {},
                "first_seen": incident.occurred_at,
                "last_seen": incident.occurred_at,
                "status": "pending",
                "patch_id": None,
                "proposal_id": None,
            }
            self.state["hard_examples"][incident.family] = group
        group["occurrences"] += 1
        group["last_seen"] = incident.occurred_at
        group.setdefault("cluster_keys", [])
        if cluster_key not in group["cluster_keys"]:
            group["cluster_keys"].append(cluster_key)
        if incident.source not in group["sources"]:
            group["sources"].append(incident.source)
        if incident.id not in group["incident_ids"]:
            group["incident_ids"].append(incident.id)
        group.setdefault("occurrence_times", []).append(incident.occurred_at)
        group["occurrence_times"] = group["occurrence_times"][-200:]
        group.setdefault("gate_flags", {}).update(gate_flags)

        cluster = existing_cluster
        if cluster is None:
            cluster = {
                "cluster_key": cluster_key,
                "category": incident.category.value,
                "occurrences": 0,
                "symptom_count": 0,
                "sources": [],
                "families": [],
                "incident_ids": [],
                "evidence_keys": [],
                "occurrence_times": [],
                "gate_flags": {},
                "first_seen": incident.occurred_at,
                "last_seen": incident.occurred_at,
                "status": "pending",
                "patch_id": None,
                "proposal_id": None,
            }
            self.state["clusters"][cluster_key] = cluster
        cluster["symptom_count"] += 1
        key = evidence_key or incident.id
        should_count = (
            not deduplicate_evidence or key not in cluster["evidence_keys"]
        )
        if should_count:
            cluster["occurrences"] += 1
            if key not in cluster["evidence_keys"]:
                cluster["evidence_keys"].append(key)
            cluster.setdefault("occurrence_times", []).append(
                incident.occurred_at
            )
            cluster["occurrence_times"] = cluster["occurrence_times"][-200:]
        cluster["last_seen"] = incident.occurred_at
        if incident.source not in cluster["sources"]:
            cluster["sources"].append(incident.source)
        if incident.family not in cluster["families"]:
            cluster["families"].append(incident.family)
        if incident.id not in cluster["incident_ids"]:
            cluster["incident_ids"].append(incident.id)
        cluster.setdefault("gate_flags", {}).update(gate_flags)

        self._save()
        self._log("incident_recorded", {
            "incident_id": incident.id,
            "family": incident.family,
            "cluster_key": cluster_key,
            "category": incident.category.value,
            "source": incident.source,
            "occurrences": group["occurrences"],
            "cluster_occurrences": cluster["occurrences"],
            "cluster_symptom_count": cluster["symptom_count"],
        })
        return self.get_group(incident.family)  # type: ignore[return-value]

    def get_cluster(self, cluster_key: str) -> Optional[Dict[str, Any]]:
        data = self.state["clusters"].get(cluster_key)
        return copy.deepcopy(data) if data else None

    def mark_cluster(
        self,
        cluster_key: str,
        status: str,
        patch_id: Optional[str] = None,
        proposal_id: Optional[str] = None,
    ) -> None:
        cluster = self.state["clusters"].get(cluster_key)
        if not cluster:
            raise NotFound(f"hard-example cluster not found: {cluster_key}")
        cluster["status"] = status
        if patch_id:
            cluster["patch_id"] = patch_id
        if proposal_id:
            cluster["proposal_id"] = proposal_id
        for family in cluster.get("families", []):
            group = self.state["hard_examples"].get(family)
            if not group:
                continue
            group["status"] = status
            if patch_id:
                group["patch_id"] = patch_id
            if proposal_id:
                group["proposal_id"] = proposal_id
        self._save()

    def mark_group(
        self,
        family: str,
        status: str,
        patch_id: Optional[str] = None,
        proposal_id: Optional[str] = None,
    ) -> None:
        group = self.state["hard_examples"].get(family)
        if not group:
            raise NotFound(f"hard-example family not found: {family}")
        group["status"] = status
        if patch_id:
            group["patch_id"] = patch_id
        if proposal_id:
            group["proposal_id"] = proposal_id
        self._save()

    def log_gate_decision(
        self,
        record_id: str,
        decision: Dict[str, Any],
    ) -> None:
        self._log("gate_evaluated", {
            "record_id": record_id,
            "decision": copy.deepcopy(decision),
        })

    # ----------------------------------------------------------
    # 决策器药方（尚未生效，等待人工）
    # ----------------------------------------------------------
    def create_proposal(
        self,
        *,
        kind: str,
        family: str,
        cluster_key: Optional[str] = None,
        title: str,
        detail: str,
        incident_ids: Iterable[str],
        suggested: Optional[Dict[str, Any]] = None,
        required_attribution: Optional[List[str]] = None,
        gate_decision: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        proposal_id = new_id("proposal")
        proposal = {
            "id": proposal_id,
            "kind": kind,  # rule_gap | knowledge_gap
            "family": family,
            "cluster_key": cluster_key,
            "title": title,
            "detail": detail,
            "status": "proposed",
            "incident_ids": list(incident_ids),
            "suggested": suggested,
            "required_attribution": required_attribution or [],
            "gate_decision": copy.deepcopy(gate_decision),
            "created_at": now,
            "resolved_at": None,
            "approver": None,
            "reason": None,
            "patch_id": None,
        }
        self.state["prescriptions"][proposal_id] = proposal
        self.mark_group(family, "proposed", proposal_id=proposal_id)
        if cluster_key:
            self.mark_cluster(cluster_key, "proposed", proposal_id=proposal_id)
        self._save()
        self._log("proposal_created", {
            "proposal_id": proposal_id,
            "kind": kind,
            "family": family,
        })
        return copy.deepcopy(proposal)

    def reject_proposal(
        self,
        proposal_id: str,
        reason: str,
        approver: str,
    ) -> None:
        proposal = self.state["prescriptions"].get(proposal_id)
        if not proposal:
            raise NotFound(f"proposal not found: {proposal_id}")
        if proposal["status"] != "proposed":
            raise NotFound(f"proposal is not proposed: {proposal['status']}")
        proposal["status"] = "rejected"
        proposal["reason"] = reason
        proposal["approver"] = approver
        proposal["resolved_at"] = utc_now()
        family = proposal["family"]
        self.mark_group(family, "rejected", proposal_id=proposal_id)
        if proposal.get("cluster_key"):
            self.mark_cluster(proposal["cluster_key"], "rejected", proposal_id=proposal_id)
        self._save()
        self._log("proposal_rejected", {
            "proposal_id": proposal_id,
            "reason": reason,
            "approver": approver,
        })

    def apply_proposal(
        self,
        proposal_id: str,
        approver: str,
        actions: List[Dict[str, Any]],
        summary: str,
        incident_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        proposal = self.state["prescriptions"].get(proposal_id)
        if not proposal:
            raise NotFound(f"proposal not found: {proposal_id}")
        if proposal["status"] != "proposed":
            raise NotFound(f"proposal is not proposed: {proposal['status']}")
        patch = self.commit(
            actions=actions,
            summary=summary,
            actor=f"human:{approver}",
            incident_ids=incident_ids or proposal["incident_ids"],
            gate_decision=proposal.get("gate_decision"),
        )
        proposal["status"] = "applied"
        proposal["approver"] = approver
        proposal["resolved_at"] = patch["created_at"]
        proposal["patch_id"] = patch["id"]
        family = proposal["family"]
        self.mark_group(
            family,
            "applied",
            patch_id=patch["id"],
            proposal_id=proposal_id,
        )
        if proposal.get("cluster_key"):
            self.mark_cluster(
                proposal["cluster_key"],
                "applied",
                patch_id=patch["id"],
                proposal_id=proposal_id,
            )
        self._save()
        self._log("proposal_applied", {
            "proposal_id": proposal_id,
            "patch_id": patch["id"],
            "approver": approver,
        })
        return patch

    # ----------------------------------------------------------
    # 补丁：快照 + 自动变更 + 回滚
    # ----------------------------------------------------------
    _ACTION_DOMAINS = {
        "add_alias": "alias_rules",
        "upsert_rule": "operational_rules",
        "delete_rule": "operational_rules",
        "add_term": "terms",
        "remove_term": "terms",
        "set_process_settings": "process_settings",
        "distill_alias": "alias_rules",
        "distill_rule": "operational_rules",
        "distill_term": "terms",
        "delete_alias": "alias_rules",
        "resolve_induction": "induction_findings",
        "set_damping_control": "damping_state",
        "clear_damping_control": "damping_state",
        "set_hibernation_state": None,
    }
    _HIBERNATION_DOMAINS = {
        "alias": "alias_rules",
        "rule": "operational_rules",
        "term": "terms",
    }

    def commit(
        self,
        *,
        actions: List[Dict[str, Any]],
        summary: str,
        actor: str = "system",
        incident_ids: Optional[List[str]] = None,
        gate_decision: Optional[Dict[str, Any]] = None,
        distillation_plan: Optional[Dict[str, Any]] = None,
        induction_plan: Optional[Dict[str, Any]] = None,
        damping_plan: Optional[Dict[str, Any]] = None,
        hibernation_plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_actions(actions)
        patch_id = f"p{self.state['next_patch_seq']:04d}"
        seq = self.state["next_patch_seq"]
        self.state["next_patch_seq"] = seq + 1
        before = copy.deepcopy(self.state)
        for action in actions:
            self._apply(action, patch_id)
        after = copy.deepcopy(self.state)
        now = utc_now()
        patch = {
            "id": patch_id,
            "seq": seq,
            "summary": summary,
            "actor": actor,
            "created_at": now,
            "status": "active",
            "incident_ids": list(incident_ids or []),
            "gate_decision": copy.deepcopy(gate_decision),
            "distillation_plan": copy.deepcopy(distillation_plan),
            "induction_plan": copy.deepcopy(induction_plan),
            "damping_plan": copy.deepcopy(damping_plan),
            "hibernation_plan": copy.deepcopy(hibernation_plan),
            "actions": copy.deepcopy(actions),
            "before": before,
            "after": after,
        }
        self._write_json(self._patch_path(patch_id), patch)
        self.state["patch_meta"][patch_id] = {
            "id": patch_id,
            "seq": seq,
            "summary": summary,
            "status": "active",
            "created_at": now,
            "gate_decision": copy.deepcopy(gate_decision),
            "distillation_plan": copy.deepcopy(distillation_plan),
            "induction_plan": copy.deepcopy(induction_plan),
            "damping_plan": copy.deepcopy(damping_plan),
            "hibernation_plan": copy.deepcopy(hibernation_plan),
        }
        self._save()
        self._log("patch_applied", {
            "patch_id": patch_id,
            "summary": summary,
            "actor": actor,
            "incident_ids": patch["incident_ids"],
        })
        return copy.deepcopy(patch)

    def _validate_actions(self, actions: List[Dict[str, Any]]) -> None:
        editable = set(self.guardrails.get("editable_domains", []))
        protected = set(self.guardrails.get("protected_domains", []))
        for action in actions:
            op = action.get("op")
            if op == "set_hibernation_state":
                domain = self._HIBERNATION_DOMAINS.get(action.get("domain"))
            else:
                domain = self._ACTION_DOMAINS.get(op)
            if domain is None:
                raise GuardrailViolation(f"unknown patch action: {op}")
            if domain in protected or domain not in editable:
                raise GuardrailViolation(f"guardrail domain cannot be patched: {domain}")
            if "guardrails" in str(action.get("target", "")).lower():
                raise GuardrailViolation("target is inside a protected guardrail domain")

    def _apply(self, action: Dict[str, Any], patch_id: str) -> None:
        op = action["op"]
        if op == "add_alias":
            self._apply_add_alias(action, patch_id)
        elif op == "upsert_rule":
            self._apply_upsert_rule(action)
        elif op == "delete_rule":
            self._apply_delete_rule(action)
        elif op == "add_term":
            self._apply_add_term(action, patch_id)
        elif op == "remove_term":
            self._apply_remove_term(action)
        elif op == "set_process_settings":
            self._apply_process_settings(action)
        elif op == "distill_alias":
            self._apply_distillation(
                action, patch_id, "alias_rules", "canonical"
            )
        elif op == "distill_rule":
            self._apply_distillation(
                action, patch_id, "operational_rules", "rule_id"
            )
        elif op == "distill_term":
            self._apply_distillation(
                action, patch_id, "terms", "term"
            )
        elif op == "delete_alias":
            self._apply_delete_alias(action)
        elif op == "resolve_induction":
            self._apply_resolve_induction(action, patch_id)
        elif op == "set_damping_control":
            self._apply_set_damping_control(action, patch_id)
        elif op == "clear_damping_control":
            self._apply_clear_damping_control(action)
        elif op == "set_hibernation_state":
            self._apply_set_hibernation_state(action, patch_id)
        else:
            raise GuardrailViolation(f"unsupported op: {op}")

    def _apply_add_alias(self, action: Dict[str, Any], patch_id: str) -> None:
        canonical = action["canonical"]
        rule = self.state["alias_rules"].get(canonical)
        if rule is None:
            created_at = utc_now()
            rule = {
                "canonical": canonical,
                "aliases": [],
                "sources": [],
                "created_at": created_at,
                "patch_id": None,
                "distillation": _default_distillation(created_at),
                "hibernation": _default_hibernation(created_at),
            }
            self.state["alias_rules"][canonical] = rule
            action["created"] = True
        else:
            action["created"] = False
        rule.setdefault(
            "distillation",
            _default_distillation(rule.get("created_at")),
        )
        rule.setdefault(
            "hibernation",
            _default_hibernation(rule.get("created_at")),
        )
        for field in ("family", "category", "structure_key"):
            if action.get(field) and not rule.get(field):
                rule[field] = action[field]
        added = [
            alias for alias in action["aliases"]
            if alias not in rule["aliases"]
        ]
        action["added"] = added
        rule["aliases"].extend(added)
        for source in action.get("sources", []):
            if source not in rule["sources"]:
                rule["sources"].append(source)
        rule["patch_id"] = patch_id

    def _apply_upsert_rule(self, action: Dict[str, Any]) -> None:
        rule = copy.deepcopy(action["rule"])
        rule_id = rule["id"]
        before = self.state["operational_rules"].get(rule_id)
        if before and "distillation" in before and "distillation" not in rule:
            rule["distillation"] = copy.deepcopy(before["distillation"])
        else:
            rule.setdefault(
                "distillation",
                _default_distillation(rule.get("created_at")),
            )
        if before and "hibernation" in before and "hibernation" not in rule:
            rule["hibernation"] = copy.deepcopy(before["hibernation"])
        else:
            rule.setdefault(
                "hibernation",
                _default_hibernation(rule.get("created_at")),
            )
        action["before"] = copy.deepcopy(before)
        action["after"] = rule
        self.state["operational_rules"][rule_id] = rule

    def _apply_delete_rule(self, action: Dict[str, Any]) -> None:
        rule_id = action["rule_id"]
        before = self.state["operational_rules"].get(rule_id)
        action["before"] = copy.deepcopy(before)
        if before:
            self.state["operational_rules"].pop(rule_id, None)

    def _apply_add_term(self, action: Dict[str, Any], patch_id: str) -> None:
        record = copy.deepcopy(action["term"])
        before = self.state["terms"].get(record["term"])
        if before and "distillation" in before and "distillation" not in record:
            record["distillation"] = copy.deepcopy(before["distillation"])
        else:
            record.setdefault(
                "distillation",
                _default_distillation(record.get("created_at")),
            )
        if before and "hibernation" in before and "hibernation" not in record:
            record["hibernation"] = copy.deepcopy(before["hibernation"])
        else:
            record.setdefault(
                "hibernation",
                _default_hibernation(record.get("created_at")),
            )
        action["before"] = copy.deepcopy(before)
        record["patch_id"] = patch_id
        action["after"] = copy.deepcopy(record)
        self.state["terms"][record["term"]] = record

    def _apply_remove_term(self, action: Dict[str, Any]) -> None:
        term = action["term"]
        action["before"] = copy.deepcopy(self.state["terms"].get(term))
        self.state["terms"].pop(term, None)

    def _apply_distillation(
        self,
        action: Dict[str, Any],
        patch_id: str,
        collection: str,
        key_field: str,
    ) -> None:
        key = action[key_field]
        record = self.state[collection].get(key)
        if record is None:
            raise NotFound(f"distillation record not found: {collection}:{key}")
        action["before"] = copy.deepcopy(record)
        metadata = record.setdefault(
            "distillation",
            _default_distillation(record.get("created_at")),
        )
        for field in (
            "quality_tier",
            "status",
            "last_evaluated_at",
            "last_evaluation_reason",
        ):
            if field in action:
                metadata[field] = copy.deepcopy(action[field])
        metadata["patch_id"] = patch_id
        if collection == "operational_rules" and "enabled" in action:
            record["enabled"] = bool(action["enabled"])
        action["after"] = copy.deepcopy(record)

    def _apply_delete_alias(self, action: Dict[str, Any]) -> None:
        canonical = action["canonical"]
        action["before"] = copy.deepcopy(
            self.state["alias_rules"].get(canonical)
        )
        self.state["alias_rules"].pop(canonical, None)

    def _apply_resolve_induction(
        self,
        action: Dict[str, Any],
        patch_id: str,
    ) -> None:
        finding_id = action["finding_id"]
        finding = self.state["induction_findings"].get(finding_id)
        if finding is None:
            raise NotFound(f"induction finding not found: {finding_id}")
        action["before"] = copy.deepcopy(finding)
        finding["status"] = action["status"]
        finding["approver"] = action.get("approver")
        finding["resolved_at"] = utc_now()
        finding["updated_at"] = finding["resolved_at"]
        finding["patch_id"] = patch_id
        action["after"] = copy.deepcopy(finding)

    def _apply_process_settings(self, action: Dict[str, Any]) -> None:
        settings = action["settings"]
        before: Dict[str, Any] = {}
        after = copy.deepcopy(self.state["process_settings"])
        for key, value in settings.items():
            before[key] = copy.deepcopy(after.get(key))
            after[key] = value
        action["before"] = before
        action["after"] = after
        self.state["process_settings"] = after

    def _apply_set_damping_control(
        self,
        action: Dict[str, Any],
        patch_id: str,
    ) -> None:
        key = action["key"]
        state = self.state.setdefault(
            "damping_state",
            {"controls": {}, "events": []},
        )
        controls = state.setdefault("controls", {})
        before = copy.deepcopy(controls.get(key))
        control = {
            "key": key,
            "domain": action.get("domain"),
            "status": action.get("status", "frozen"),
            "reason": action.get("reason"),
            "coefficient": action.get("coefficient"),
            "frozen_until": action.get("frozen_until"),
            "requires_human": bool(action.get("requires_human", False)),
            "evidence": copy.deepcopy(action.get("evidence") or {}),
            "frozen_at": utc_now(),
            "patch_id": patch_id,
        }
        controls[key] = control
        state.setdefault("events", []).append({
            "action": "set",
            "key": key,
            "reason": control["reason"],
            "at": control["frozen_at"],
            "patch_id": patch_id,
        })
        state["events"] = state["events"][-200:]
        action["before"] = before
        action["after"] = copy.deepcopy(control)

    def _apply_clear_damping_control(self, action: Dict[str, Any]) -> None:
        key = action["key"]
        state = self.state.setdefault(
            "damping_state",
            {"controls": {}, "events": []},
        )
        controls = state.setdefault("controls", {})
        before = copy.deepcopy(controls.get(key))
        controls.pop(key, None)
        state.setdefault("events", []).append({
            "action": "clear",
            "key": key,
            "at": utc_now(),
        })
        state["events"] = state["events"][-200:]
        action["before"] = before
        action["after"] = None

    def _apply_set_hibernation_state(
        self,
        action: Dict[str, Any],
        patch_id: str,
    ) -> None:
        domain = action["domain"]
        collection = self._HIBERNATION_DOMAINS.get(domain)
        if collection is None:
            raise GuardrailViolation(
                f"unsupported hibernation domain: {domain}"
            )
        key = action["key"]
        record = self.state[collection].get(key)
        if record is None:
            raise NotFound(f"hibernation record not found: {domain}:{key}")
        action["before"] = copy.deepcopy(record)
        status = action["status"]
        if status not in {"active", "observing", "hibernating"}:
            raise GuardrailViolation(f"unsupported hibernation status: {status}")
        evaluated_at = action.get("evaluated_at") or utc_now()
        hibernation = copy.deepcopy(
            record.get("hibernation") or _default_hibernation(
                record.get("created_at")
            )
        )
        history = list(hibernation.get("history") or [])
        history.append({
            "at": evaluated_at,
            "status": status,
            "reason": action.get("reason"),
            "patch_id": patch_id,
        })
        hibernation.update({
            "status": status,
            "reason": action.get("reason"),
            "entered_at": evaluated_at,
            "last_transition_at": evaluated_at,
            "patch_id": patch_id,
            "history": history[-20:],
        })
        if status == "observing":
            hibernation["observation_started_at"] = evaluated_at
        elif status == "hibernating":
            hibernation["hibernated_at"] = evaluated_at
        elif status == "active":
            hibernation["last_wake_at"] = evaluated_at
            hibernation["observation_started_at"] = None
            hibernation["hibernated_at"] = None
        record["hibernation"] = hibernation
        if domain == "rule":
            previous = action.get("before") or {}
            previous_lifecycle = (
                (previous.get("hibernation") or {}).get("status")
            )
            if status == "hibernating":
                enabled_before_hibernation = bool(previous.get("enabled", False))
                hibernation["enabled_before_hibernation"] = (
                    enabled_before_hibernation
                )
                hibernation["disabled_by_hibernation"] = (
                    enabled_before_hibernation
                )
                record["enabled"] = False
            elif status == "active" and previous_lifecycle == "hibernating":
                if hibernation.get("disabled_by_hibernation"):
                    record["enabled"] = True
        action["after"] = copy.deepcopy(record)

    def rollback(self, patch_id: str) -> Dict[str, Any]:
        meta = self.state["patch_meta"].get(patch_id)
        if not meta or not self._patch_path(patch_id).exists():
            raise NotFound(f"patch not found: {patch_id}")
        if meta["status"] != "active":
            raise NotFound(f"patch is not active: {patch_id}")
        latest = self.latest_active_patch()
        if patch_id != latest:
            raise RollbackOrderError(
                f"cannot rollback {patch_id} while {latest} is still active; "
                "rollback newest patch first"
            )
        patch = json.loads(self._patch_path(patch_id).read_text(encoding="utf-8"))
        for action in reversed(patch["actions"]):
            self._undo(action)
        patch["status"] = "rolled_back"
        meta["status"] = "rolled_back"
        self._write_json(self._patch_path(patch_id), patch)
        self._save()
        self._log("patch_rolled_back", {
            "patch_id": patch_id,
            "summary": patch.get("summary", ""),
        })
        return copy.deepcopy(patch)

    def _undo(self, action: Dict[str, Any]) -> None:
        op = action["op"]
        if op == "add_alias":
            canonical = action["canonical"]
            rule = self.state["alias_rules"].get(canonical)
            if not rule:
                return
            removed = set(action.get("added") or action.get("aliases") or [])
            rule["aliases"] = [a for a in rule["aliases"] if a not in removed]
            if action.get("created") and not rule["aliases"]:
                self.state["alias_rules"].pop(canonical, None)
        elif op == "upsert_rule":
            rule_id = action["rule"]["id"]
            before = action.get("before")
            if before is None:
                self.state["operational_rules"].pop(rule_id, None)
            else:
                self.state["operational_rules"][rule_id] = copy.deepcopy(before)
        elif op == "delete_rule":
            rule_id = action["rule_id"]
            before = action.get("before")
            if before:
                self.state["operational_rules"][rule_id] = copy.deepcopy(before)
        elif op == "add_term":
            term = action["term"]["term"]
            before = action.get("before")
            if before is None:
                self.state["terms"].pop(term, None)
            else:
                self.state["terms"][term] = copy.deepcopy(before)
        elif op == "remove_term":
            term = action["term"]
            before = action.get("before")
            if before:
                self.state["terms"][term] = copy.deepcopy(before)
        elif op == "set_process_settings":
            before = action.get("before") or {}
            for key, value in before.items():
                self.state["process_settings"][key] = copy.deepcopy(value)
        elif op == "distill_alias":
            canonical = action["canonical"]
            before = action.get("before")
            if before is None:
                self.state["alias_rules"].pop(canonical, None)
            else:
                self.state["alias_rules"][canonical] = copy.deepcopy(before)
        elif op == "distill_rule":
            rule_id = action["rule_id"]
            before = action.get("before")
            if before is None:
                self.state["operational_rules"].pop(rule_id, None)
            else:
                self.state["operational_rules"][rule_id] = copy.deepcopy(before)
        elif op == "distill_term":
            term = action["term"]
            before = action.get("before")
            if before is None:
                self.state["terms"].pop(term, None)
            else:
                self.state["terms"][term] = copy.deepcopy(before)
        elif op == "delete_alias":
            canonical = action["canonical"]
            before = action.get("before")
            if before is not None:
                self.state["alias_rules"][canonical] = copy.deepcopy(before)
        elif op == "resolve_induction":
            finding_id = action["finding_id"]
            before = action.get("before")
            if before is None:
                self.state["induction_findings"].pop(finding_id, None)
            else:
                self.state["induction_findings"][finding_id] = copy.deepcopy(before)
        elif op == "set_damping_control":
            key = action["key"]
            before = action.get("before")
            controls = self.state.setdefault("damping_state", {}).setdefault(
                "controls",
                {},
            )
            if before is None:
                controls.pop(key, None)
            else:
                controls[key] = copy.deepcopy(before)
        elif op == "clear_damping_control":
            key = action["key"]
            before = action.get("before")
            controls = self.state.setdefault("damping_state", {}).setdefault(
                "controls",
                {},
            )
            if before is not None:
                controls[key] = copy.deepcopy(before)
        elif op == "set_hibernation_state":
            domain = action["domain"]
            collection = self._HIBERNATION_DOMAINS.get(domain)
            if collection is None:
                raise GuardrailViolation(
                    f"unsupported hibernation domain: {domain}"
                )
            key = action["key"]
            before = action.get("before")
            if before is None:
                self.state[collection].pop(key, None)
            else:
                self.state[collection][key] = copy.deepcopy(before)
        else:
            raise GuardrailViolation(f"cannot undo op: {op}")

    def rollback_proposal_link(self, proposal_id: str, patch_id: str) -> None:
        proposal = self.state["prescriptions"].get(proposal_id)
        if proposal and proposal.get("patch_id") == patch_id:
            proposal["status"] = "rolled_back"
            proposal["resolved_at"] = utc_now()
            proposal["patch_id"] = patch_id
            self._save()
            self._log("proposal_rolled_back", {
                "proposal_id": proposal_id,
                "patch_id": patch_id,
            })

    def latest_active_patch(self) -> Optional[str]:
        active = [
            (int(pid[1:]), pid)
            for pid, meta in self.state["patch_meta"].items()
            if meta.get("status") == "active"
        ]
        if not active:
            return None
        _, patch_id = max(active)
        return patch_id
