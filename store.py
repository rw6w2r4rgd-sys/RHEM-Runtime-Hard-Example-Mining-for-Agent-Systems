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

DEFAULT_GUARDRAILS: Dict[str, Any] = {
    "schema": GUARDRAIL_SCHEMA,
    "frozen": True,
    "editable_domains": [
        "alias_rules",
        "operational_rules",
        "terms",
        "process_settings",
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
            "prescriptions": {},
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
            data.setdefault("prescriptions", {})
            data.setdefault("patch_meta", {})
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

    def list_prescriptions(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.state["prescriptions"].values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return copy.deepcopy(rows)

    # ----------------------------------------------------------
    # 难例记录与门控
    # ----------------------------------------------------------
    def record_incident(self, incident: Incident) -> Dict[str, Any]:
        row = incident.to_dict()
        self.state["incidents"].append(row)
        group = self.state["hard_examples"].setdefault(
            incident.family,
            {
                "family": incident.family,
                "category": incident.category.value,
                "occurrences": 0,
                "sources": [],
                "incident_ids": [],
                "first_seen": incident.occurred_at,
                "last_seen": incident.occurred_at,
                "status": "pending",
                "patch_id": None,
                "proposal_id": None,
            },
        )
        if group.get("category") != incident.category.value:
            raise ValueError(
                f"family already belongs to another category: {group['category']}"
            )
        group["occurrences"] += 1
        group["last_seen"] = incident.occurred_at
        if incident.source not in group["sources"]:
            group["sources"].append(incident.source)
        if incident.id not in group["incident_ids"]:
            group["incident_ids"].append(incident.id)
        self._save()
        self._log("incident_recorded", {
            "incident_id": incident.id,
            "family": incident.family,
            "category": incident.category.value,
            "source": incident.source,
            "occurrences": group["occurrences"],
        })
        return self.get_group(incident.family)  # type: ignore[return-value]

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

    # ----------------------------------------------------------
    # 决策器药方（尚未生效，等待人工）
    # ----------------------------------------------------------
    def create_proposal(
        self,
        *,
        kind: str,
        family: str,
        title: str,
        detail: str,
        incident_ids: Iterable[str],
        suggested: Optional[Dict[str, Any]] = None,
        required_attribution: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        now = utc_now()
        proposal_id = new_id("proposal")
        proposal = {
            "id": proposal_id,
            "kind": kind,  # rule_gap | knowledge_gap
            "family": family,
            "title": title,
            "detail": detail,
            "status": "proposed",
            "incident_ids": list(incident_ids),
            "suggested": suggested,
            "required_attribution": required_attribution or [],
            "created_at": now,
            "resolved_at": None,
            "approver": None,
            "reason": None,
            "patch_id": None,
        }
        self.state["prescriptions"][proposal_id] = proposal
        self.mark_group(family, "proposed", proposal_id=proposal_id)
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
    }

    def commit(
        self,
        *,
        actions: List[Dict[str, Any]],
        summary: str,
        actor: str = "system",
        incident_ids: Optional[List[str]] = None,
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
        else:
            raise GuardrailViolation(f"unsupported op: {op}")

    def _apply_add_alias(self, action: Dict[str, Any], patch_id: str) -> None:
        canonical = action["canonical"]
        rule = self.state["alias_rules"].get(canonical)
        if rule is None:
            rule = {
                "canonical": canonical,
                "aliases": [],
                "sources": [],
                "created_at": utc_now(),
                "patch_id": None,
            }
            self.state["alias_rules"][canonical] = rule
            action["created"] = True
        else:
            action["created"] = False
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
        action["before"] = copy.deepcopy(
            self.state["terms"].get(record["term"])
        )
        action["after"] = record
        record["patch_id"] = patch_id
        self.state["terms"][record["term"]] = record

    def _apply_remove_term(self, action: Dict[str, Any]) -> None:
        term = action["term"]
        action["before"] = copy.deepcopy(self.state["terms"].get(term))
        self.state["terms"].pop(term, None)

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