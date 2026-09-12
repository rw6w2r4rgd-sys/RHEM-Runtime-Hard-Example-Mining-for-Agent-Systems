# -*- coding: utf-8 -*-
"""RHEM 参考实现：领域模型与异常。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class ErrorCategory(str, Enum):
    RECOGNITION = "recognition"
    PROCESS = "process"
    RULE_GAP = "rule_gap"
    KNOWLEDGE_GAP = "knowledge_gap"


CATEGORY_LABELS: Dict[ErrorCategory, str] = {
    ErrorCategory.RECOGNITION: "识别错误",
    ErrorCategory.PROCESS: "流程错误",
    ErrorCategory.RULE_GAP: "规则缺失",
    ErrorCategory.KNOWLEDGE_GAP: "知识缺口",
}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class RhemError(Exception):
    """RHEM 基础异常。"""


class GuardrailViolation(RhemError):
    """尝试改写护栏域。"""


class NotFound(RhemError):
    """记录不存在，或当前状态不允许该操作。"""


class RollbackOrderError(RhemError):
    """只允许按补丁顺序倒序回滚。"""


@dataclass
class Incident:
    """一次现场错误或用户纠错。"""

    category: ErrorCategory
    family: str
    message: str
    source: str
    evidence: Dict[str, Any]
    expected: Optional[str] = None
    actual: Optional[str] = None
    active: bool = True
    cluster_key: Optional[str] = None
    # 保留在末尾，避免破坏旧版按位置传参的 Incident(...) 调用。
    id: str = field(default_factory=lambda: new_id("inc"))
    occurred_at: str = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category.value,
            "family": self.family,
            "message": self.message,
            "source": self.source,
            "evidence": self.evidence,
            "occurred_at": self.occurred_at,
            "expected": self.expected,
            "actual": self.actual,
            "active": self.active,
            "cluster_key": self.cluster_key,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        return cls(
            category=ErrorCategory(data["category"]),
            family=data["family"],
            message=data["message"],
            source=data["source"],
            evidence=data.get("evidence") or {},
            id=data["id"],
            occurred_at=data.get("occurred_at") or utc_now(),
            expected=data.get("expected"),
            actual=data.get("actual"),
            active=bool(data.get("active", True)),
            cluster_key=data.get("cluster_key"),
        )


@dataclass
class HardExampleGroup:
    """同一难例族；累计次数到达门控阈值前不并库。"""

    family: str
    category: ErrorCategory
    occurrences: int = 0
    sources: List[str] = field(default_factory=list)
    incident_ids: List[str] = field(default_factory=list)
    first_seen: str = field(default_factory=utc_now)
    last_seen: str = field(default_factory=utc_now)
    status: str = "pending"  # pending | applied | proposed | rolled_back
    patch_id: Optional[str] = None
    proposal_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "category": self.category.value,
            "occurrences": self.occurrences,
            "sources": list(self.sources),
            "incident_ids": list(self.incident_ids),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "status": self.status,
            "patch_id": self.patch_id,
            "proposal_id": self.proposal_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HardExampleGroup":
        return cls(
            family=data["family"],
            category=ErrorCategory(data["category"]),
            occurrences=int(data.get("occurrences", 0)),
            sources=list(data.get("sources") or []),
            incident_ids=list(data.get("incident_ids") or []),
            first_seen=data.get("first_seen") or utc_now(),
            last_seen=data.get("last_seen") or utc_now(),
            status=data.get("status", "pending"),
            patch_id=data.get("patch_id"),
            proposal_id=data.get("proposal_id"),
        )
