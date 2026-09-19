"""仅追加的审计日志。

每次匹配、联络、替补、里程碑与身份解封都留下带依据的记录，
联调结束时通过 export() 逐条核对。审计中只出现别名，绝不出现真实身份。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    case_id: str | None
    subject: str | None  # 只记录别名
    basis: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "case_id": self.case_id,
            "subject": self.subject,
            "basis": _jsonable(self.basis),
        }


class AuditLog:
    def __init__(self, clock):
        self._clock = clock
        self._events: list[AuditEvent] = []

    def record(
        self,
        action: str,
        *,
        actor: str,
        case_id: str | None = None,
        subject: str | None = None,
        basis: Mapping[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            seq=len(self._events) + 1,
            at=self._clock.now(),
            actor=actor,
            action=action,
            case_id=case_id,
            subject=subject,
            basis=dict(basis or {}),
        )
        self._events.append(event)
        return event

    def find(self, action: str | None = None, case_id: str | None = None) -> list[AuditEvent]:
        return [
            event
            for event in self._events
            if (action is None or event.action == action)
            and (case_id is None or event.case_id == case_id)
        ]

    def export(self) -> list[dict]:
        """按发生顺序导出，供联调核对与外部审计归档。"""
        return [event.as_dict() for event in self._events]
